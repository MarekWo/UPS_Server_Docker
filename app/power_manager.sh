#!/bin/bash

################################################################################
#
# Power Manager for Dummy NUT Server (v1.4.1 - Stale Client Timeout from Config)
#
# Author: Marek Wojtaszek (Enhancements by Gemini)
# GitHub: https://github.com/MarekWo/
#
# This script monitors sentinel hosts or a schedule to determine power status
# and updates a virtual (dummy) NUT server. It can also send email
# notifications for various system events.
#
# v1.0.2 Change: Switched to sequential pinging.
# v1.0.3 Change: Enhanced configuration for WAKE_HOSTS.
# v1.0.4 Change: Fixed config parsing for values with spaces.
# v1.0.5 Change: Added "WoL sent" status for Web UI.
# v1.0.6 Change: Added manual Power Outage Simulation mode.
# v1.1.0 Change: Added scheduler for Power Outage Simulation.
# v1.1.2 Change: Fixed regex in get_sections function to correctly find schedules.
# v1.2.0 Change: Added selective auto WoL option per host (AUTO_WOL="false").
# v1.3.0 Change: Added comprehensive email notification system.
# v1.3.1 Change: Improved error logging for email notifications.
# v1.3.3 Fixed missing power outage simulation module.
# v1.3.4 Fixed missing WoL signal when waking hosts.
# v1.4.0 Change: Added notification handling for CLIENT_SHUTDOWN and CLIENT_STALE events.
# v1.4.1 Change: Moved CLIENT_STALE_TIMEOUT_MINUTES to power_manager.conf
#
################################################################################

# === CONFIGURATION ===
LOG_FILE="/var/log/power_manager.log"
CONFIG_FILE="/etc/nut/power_manager.conf"
STATE_FILE="/var/run/nut/power_manager.state" # Stores the power state across runs
NOTIFICATION_STATE_FILE="/var/run/nut/notification.state" # Stores notification timestamps
### MODIFIED ###
CLIENT_STATUS_FILE="/var/run/nut/client_status.json" # Input for client status
CLIENT_NOTIFICATION_STATE_FILE="/var/run/nut/client_notification.state" # Stores client notification states
UPS_STATE_FILE_DEFAULT="/var/run/nut/virtual.device"
EMAIL_SENDER_SCRIPT="/app/send_email.py"

# Absolute paths to commands for cron compatibility
PING_CMD="/bin/ping"
WAKEONLAN_CMD="/usr/bin/wakeonlan"
PYTHON_CMD="/usr/local/bin/python"
### NEW ###
JQ_CMD="/usr/bin/jq"

# Notification debounce time in seconds (1 hour)
NOTIFICATION_DEBOUNCE_SECONDS=3600
### NEW ###
# Timeout in minutes for a client to be considered stale
# CLIENT_STALE_TIMEOUT_MINUTES=5 # Moved to config file

# === LOGGING FUNCTION (DUAL LOGGING TO FILE AND SYSLOG) ===
log() {
    # Arguments: level message
    local level="${1:-info}"
    shift
    local msg="$*"
    local timestamp
    timestamp=$(date '+%Y-%m-%d %H:%M:%S')

    # 1. Log to the local file for detailed debugging
    echo "$timestamp - ${level^^} - $msg" >> "$LOG_FILE"

    # 2. Log to syslog for system-wide integration
    logger -p "user.$level" -t "PowerManager" -- "$msg"
}

# === FUNCTION TO PARSE THE ENTIRE CONFIG FILE ===
parse_config() {
    local config_file="$1"
    local current_section=""

    while IFS= read -r line || [[ -n "$line" ]]; do
        line=$(echo "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
        if [[ "$line" =~ ^# || -z "$line" ]]; then continue; fi
        if [[ "$line" =~ ^\[(.*)\]$ ]]; then
            current_section="${BASH_REMATCH[1]}"
            continue
        fi
        if [[ "$line" =~ ^([^=]+)=(.*)$ ]]; then
            key=$(echo "${BASH_REMATCH[1]}" | sed 's/[[:space:]]*$//')
            value=$(echo "${BASH_REMATCH[2]}" | sed 's/^[[:space:]]*//' | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")
            
            if [[ -n "$current_section" ]]; then
                declare -g "${current_section}_${key}=$value"
            else
                declare -g "$key=$value"
            fi
        fi
    done < "$config_file"
}

# === FUNCTION TO GET ALL SECTIONS OF A GIVEN TYPE ===
get_sections() {
    local type_prefix="$1"
    compgen -v | grep -E "^${type_prefix}_[0-9]+_NAME$" | sed 's/_NAME$//' | sort -V
}

# === FUNCTION TO UPDATE CLIENT STATUS JSON ===
update_client_status() {
    local ip_address="$1"
    local new_status="$2"
    local status_file="/var/run/nut/client_status.json"
    local temp_file
    temp_file=$(mktemp)

    if ! command -v jq &> /dev/null; then
        # jq is installed in the Dockerfile, but this is a safeguard
        log "warn" "jq command not found, cannot update client status file."
        return
    fi

    # Create the status file if it doesn't exist
    if [ ! -f "$status_file" ]; then
        echo "{}" > "$status_file"
    fi

    # Use jq to safely update the JSON file
    jq --arg ip "$ip_address" --arg status "$new_status" \
       '.[$ip] = {
           "status": $status,
           "remaining_seconds": null,
           "shutdown_delay": null,
           "timestamp": (now | todate)
       }' "$status_file" > "$temp_file" && mv "$temp_file" "$status_file"
}

# === NOTIFICATION FUNCTION ===
send_notification() {
    # Arguments: type subject body
    local type="$1"
    local subject="$2"
    local body="$3"
    
    local enabled_var_name="NOTIFY_${type^^}"
    
    if [[ "${!enabled_var_name}" != "true" ]]; then
        log "info" "Notification for ${type} is disabled. Skipping."
        return
    fi
    
    # Anti-spam for APP_ERROR
    if [[ "$type" == "APP_ERROR" ]]; then
        local last_sent
        last_sent=$(grep "^${type}_LAST_SENT=" "$NOTIFICATION_STATE_FILE" | cut -d'=' -f2)
        local now
        now=$(date +%s)
        if [[ -n "$last_sent" && $((now - last_sent)) -lt $NOTIFICATION_DEBOUNCE_SECONDS ]]; then
            log "warn" "Error notification for ${type} is debounced to prevent spam. Skipping."
            return
        fi
        # Update timestamp
        sed -i "/^${type}_LAST_SENT=/d" "$NOTIFICATION_STATE_FILE"
        echo "${type}_LAST_SENT=$now" >> "$NOTIFICATION_STATE_FILE"
    fi
    
    log "info" "Sending notification: $subject"
    
    # *** THIS IS THE CORRECTED PART ***
    # Capture stderr from the python script to a variable for detailed logging
    error_output=$("$PYTHON_CMD" "$EMAIL_SENDER_SCRIPT" "$subject" "$body" 2>&1 >/dev/null)
    
    if [ $? -ne 0 ]; then
        log "err" "Failed to send email notification. Reason: ${error_output}"
        if [[ "$type" != "APP_ERROR" ]]; then # Avoid recursion
            send_notification "APP_ERROR" "[UPS] CRITICAL: Email Sending Failed" "The UPS server failed to send an email notification. Please check the SMTP configuration and the application logs. Error: ${error_output}"
        fi
    fi
}

### NEW ###
# === FUNCTION TO CHECK CLIENT STATUSES AND SEND NOTIFICATIONS ===
check_client_statuses() {
    log "info" "Checking UPS client statuses for notifications..."

    if [ ! -f "$CLIENT_STATUS_FILE" ]; then
        log "info" "Client status file not found. Skipping client status check."
        return
    fi

    if ! command -v $JQ_CMD &> /dev/null; then
        log "warn" "jq command not found, cannot parse client statuses."
        return
    fi

    # Create the client notification state file if it doesn't exist
    touch "$CLIENT_NOTIFICATION_STATE_FILE"

    local now_seconds
    now_seconds=$(date +%s)    
    local stale_threshold_seconds=$((CLIENT_STALE_TIMEOUT_MINUTES * 60))

    # Iterate over all configured hosts that are UPS clients
    for section in $(get_sections "WAKE_HOST"); do
        local is_ups_client_var="${section}_SHUTDOWN_DELAY_MINUTES"
        local ip_var="${section}_IP"
        local name_var="${section}_NAME"

        # Process only if the host is a defined UPS client (has a shutdown delay)
        if [[ -n "${!is_ups_client_var}" ]]; then
            local client_ip="${!ip_var}"
            local client_name="${!name_var:-$client_ip}"

            if [[ -z "$client_ip" ]]; then
                continue
            fi

            # Extract status and timestamp for the current client IP using jq
            local client_data
            client_data=$($JQ_CMD -r --arg ip "$client_ip" '.[$ip] | if . then "\(.status) \(.timestamp)" else "not_found" end' "$CLIENT_STATUS_FILE")

            if [[ "$client_data" == "not_found" ]]; then
                log "debug" "No status entry found for client '$client_name' ($client_ip)."
                continue
            fi

            local client_status
            local client_timestamp_str
            read -r client_status client_timestamp_str <<< "$client_data"

            local client_timestamp_seconds
            client_timestamp_seconds=$(date -d "${client_timestamp_str}" +%s 2>/dev/null)

            # --- 1. Check for Shutdown Initiation ---
            local shutdown_notified_flag="SHUTDOWN_NOTIFIED_${client_ip//./_}"
            local was_shutdown_notified
            was_shutdown_notified=$(grep "^${shutdown_notified_flag}=true" "$CLIENT_NOTIFICATION_STATE_FILE")

            if [[ "$client_status" == "shutdown_pending" && -z "$was_shutdown_notified" ]]; then
                log "warn" "Client '$client_name' ($client_ip) has initiated shutdown. Sending notification."
                send_notification "CLIENT_SHUTDOWN" "[UPS] ALERT: Client Initiating Shutdown" "The UPS client '${client_name}' (${client_ip}) has detected a power outage and is beginning its graceful shutdown procedure."
                # Set flag to prevent re-notification during this outage
                echo "${shutdown_notified_flag}=true" >> "$CLIENT_NOTIFICATION_STATE_FILE"
            fi

            # --- 2. Check for Stale Status ---
            local stale_notified_flag="STALE_NOTIFIED_${client_ip//./_}"
            local was_stale_notified
            was_stale_notified=$(grep "^${stale_notified_flag}=true" "$CLIENT_NOTIFICATION_STATE_FILE")
            local time_diff=$((now_seconds - client_timestamp_seconds))

            if [[ $time_diff -gt $stale_threshold_seconds ]]; then
                # Status is stale
                if [[ -z "$was_stale_notified" ]]; then
                    log "warn" "Client '$client_name' ($client_ip) status is stale (last update ${time_diff}s ago). Sending notification."                    
                    send_notification "CLIENT_STALE" "[UPS] WARNING: Client Status is Stale" "The UPS client '${client_name}' (${client_ip}) has not reported its status for over ${CLIENT_STALE_TIMEOUT_MINUTES} minutes. It may be unresponsive or offline."
                    echo "${stale_notified_flag}=true" >> "$CLIENT_NOTIFICATION_STATE_FILE"
                fi
            else
                # Status is fresh again, clear the stale flag if it was set
                if [[ -n "$was_stale_notified" ]]; then
                    log "info" "Client '$client_name' ($client_ip) has recovered from stale status."
                    sed -i "/^${stale_notified_flag}=true/d" "$CLIENT_NOTIFICATION_STATE_FILE"
                fi
            fi
        fi
    done
}


# === SCRIPT START ===
log "info" "--- Power check initiated ---"

# Create notification state file if it doesn't exist
touch "$NOTIFICATION_STATE_FILE"

# Load configuration
if [ ! -f "$CONFIG_FILE" ]; then
    log "err" "CRITICAL ERROR: Configuration file not found at $CONFIG_FILE. Exiting."
    send_notification "APP_ERROR" "[UPS] CRITICAL: Configuration File Missing" "The main configuration file at $CONFIG_FILE was not found. The application cannot continue."
    exit 1
fi
parse_config "$CONFIG_FILE"

# Set defaults if not defined in config
CLIENT_STALE_TIMEOUT_MINUTES="${CLIENT_STALE_TIMEOUT_MINUTES:-5}"

# Use UPS_STATE_FILE from config, or default if not set
UPS_STATE_FILE="${UPS_STATE_FILE:-$UPS_STATE_FILE_DEFAULT}"

# Load previous state
PREVIOUS_STATE=""
if [ -f "$STATE_FILE" ]; then
    source "$STATE_FILE" # This loads STATE as PREVIOUS_STATE for this run
    PREVIOUS_STATE="$STATE"
fi

# --- SCHEDULE CHECK LOGIC (PRZYWRÓCONA SEKCJA) ---
NOW_HHMM=$(date +%H:%M)
NOW_DOW_LC=$(date +%A | tr '[:upper:]' '[:lower:]')
NOW_DATE=$(date +%Y-%m-%d)
SCHEDULE_SIMULATION_ACTION=""

# DEBUG: Log current time variables
log "debug" "Current time check: HH:MM='$NOW_HHMM', DOW='$NOW_DOW_LC', DATE='$NOW_DATE'"

# DEBUG: Check if schedule sections are found
SCHEDULE_SECTIONS=$(get_sections "SCHEDULE")
if [ -z "$SCHEDULE_SECTIONS" ]; then
    log "debug" "No schedule sections found in config."
else
    log "debug" "Found schedule sections: $SCHEDULE_SECTIONS"
fi

for section in $SCHEDULE_SECTIONS; do
    enabled_var="${section}_ENABLED"
    type_var="${section}_TYPE"
    time_var="${section}_TIME"
    action_var="${section}_ACTION"
    date_var="${section}_DATE"
    dow_var="${section}_DAY_OF_WEEK"
    name_var="${section}_NAME"

    # DEBUG: Log the values for the current schedule section being processed
    log "debug" "Processing schedule [$section]: NAME='${!name_var}', ENABLED='${!enabled_var}', TYPE='${!type_var}', TIME='${!time_var}', ACTION='${!action_var}', DATE='${!date_var}', DOW='${!dow_var}'"

    # Skip if disabled or missing essential info
    [[ "${!enabled_var}" != "true" || -z "${!type_var}" || -z "${!time_var}" ]] && continue

    match=false
    if [[ "${!type_var}" == "one-time" && "${!date_var}" == "$NOW_DATE" && "${!time_var}" == "$NOW_HHMM" ]]; then
        match=true
        log "debug" "Match found for one-time schedule '$section'"
    elif [[ "${!type_var}" == "recurring" && ("${!dow_var}" == "$NOW_DOW_LC" || "${!dow_var}" == "everyday") && "${!time_var}" == "$NOW_HHMM" ]]; then
        match=true
        log "debug" "Match found for recurring schedule '$section'"
    fi

    if $match; then
        log "info" "Schedule match: [${!name_var}] triggers action [${!action_var}]."
        SCHEDULE_SIMULATION_ACTION="${!action_var}"
        # If a one-time schedule matches, disable it to prevent re-triggering
        if [[ "${!type_var}" == "one-time" ]]; then
            log "info" "Disabling one-time schedule [${!name_var}] after execution."
            sed -i "/^\[$section\]/,/^\s*\[/ s/^\(ENABLED\s*=\s*\).*/\1\"false\"/" "$CONFIG_FILE"
        fi
        break # Process first match only
    fi
done

# Apply scheduled action by modifying the config file
if [[ "$SCHEDULE_SIMULATION_ACTION" == "start" ]]; then
    log "warn" "Scheduled action: STARTING Power Outage Simulation."
    sed -i 's/^\(POWER_SIMULATION_MODE\s*=\s*\).*/\1\"true\"/' "$CONFIG_FILE"
    POWER_SIMULATION_MODE="true" # Update live variable for this run
    send_notification "SIMULATION_MODE" "[UPS] INFO: Power Outage Simulation Started" "The Power Outage Simulation has been started by schedule."
elif [[ "$SCHEDULE_SIMULATION_ACTION" == "stop" ]]; then
    log "info" "Scheduled action: STOPPING Power Outage Simulation."
    sed -i 's/^\(POWER_SIMULATION_MODE\s*=\s*\).*/\1\"false\"/' "$CONFIG_FILE"
    POWER_SIMULATION_MODE="false" # Update live variable for this run
    send_notification "SIMULATION_MODE" "[UPS] INFO: Power Outage Simulation Stopped" "The Power Outage Simulation has been stopped by schedule."
fi

# --- MAIN DECISION LOGIC ---
LIVE_HOSTS_COUNT=0
POWER_STATUS="ONLINE"

if [[ "${POWER_SIMULATION_MODE}" == "true" ]]; then
    log "warn" "Power Outage Simulation is active. Forcing power status to OFFLINE."
    POWER_STATUS="OFFLINE"
else
    log "info" "Pinging sentinel hosts: $SENTINEL_HOSTS"
    for IP in $SENTINEL_HOSTS; do
        if $PING_CMD -c 1 -W 1 "$IP" &> /dev/null; then
            log "info" "  -> Sentinel host $IP is online."
            LIVE_HOSTS_COUNT=$((LIVE_HOSTS_COUNT + 1))
        else
            log "info" "  -> Sentinel host $IP is offline."
        fi
    done
    log "info" "Found $LIVE_HOSTS_COUNT online sentinel hosts."
    if [ "$LIVE_HOSTS_COUNT" -eq 0 ]; then
        POWER_STATUS="OFFLINE"
    fi
fi

# --- STATE UPDATE, NOTIFICATION, and WoL LOGIC ---
NOW_SECONDS=$(date +%s)

if [ "$POWER_STATUS" == "OFFLINE" ]; then
    if [[ "$PREVIOUS_STATE" != "POWER_FAIL" ]]; then
        log "warn" "STATE CHANGE: Power failure detected! Setting UPS status to OB LB."
        ### NEW ###
        # Clear previous client notification states on a new power failure event
        log "info" "New power failure detected. Clearing previous client notification states."
        rm -f "$CLIENT_NOTIFICATION_STATE_FILE"
        touch "$CLIENT_NOTIFICATION_STATE_FILE"
        ### END NEW ###
        send_notification "POWER_FAIL" "[UPS] ALERT: Power Outage Detected" "All sentinel hosts are offline. The system is now running on UPS power. Client shutdown procedures will be initiated."
        echo "STATE=POWER_FAIL" > "$STATE_FILE"
        echo "TIMESTAMP=$NOW_SECONDS" >> "$STATE_FILE"
    fi
    echo "ups.status: OB LB" > "$UPS_STATE_FILE"

else # Power is ONLINE
    echo "ups.status: OL" > "$UPS_STATE_FILE"

    if [ -f "$STATE_FILE" ]; then
        source "$STATE_FILE" # Load STATE and TIMESTAMP

        if [ "$STATE" == "POWER_FAIL" ]; then
            log "info" "STATE CHANGE: Power restoration detected. Starting $WOL_DELAY_MINUTES minute delay for Wake-on-LAN."
            
            # Calculate outage duration
            OUTAGE_DURATION=$((NOW_SECONDS - TIMESTAMP))
            DURATION_MINUTES=$((OUTAGE_DURATION / 60))
            
            send_notification "POWER_RESTORED" "[UPS] INFO: Power Has Been Restored" "Power has been restored after approximately ${DURATION_MINUTES} minutes. Waiting ${WOL_DELAY_MINUTES} minutes before initiating Wake-on-LAN procedures."
            
            echo "STATE=POWER_RESTORED" > "$STATE_FILE"
            echo "TIMESTAMP=$NOW_SECONDS" >> "$STATE_FILE"

        elif [ "$STATE" == "POWER_RESTORED" ]; then
            DELAY_SECONDS=$((WOL_DELAY_MINUTES * 60))
            TIME_ELAPSED=$((NOW_SECONDS - TIMESTAMP))

            if [ "$TIME_ELAPSED" -ge "$DELAY_SECONDS" ]; then
                log "info" "WoL delay has passed. Initiating wake-up sequence for servers."
                WOL_HOSTS_LIST=""

                for section in $(get_sections "WAKE_HOST"); do
                    name_var="${section}_NAME"
                    ip_var="${section}_IP"
                    mac_var="${section}_MAC"
                    broadcast_var="${section}_BROADCAST_IP"
                    auto_wol_var="${section}_AUTO_WOL"
                    
                    name="${!name_var:-Unknown Host}"
                    ip="${!ip_var}"
                    mac="${!mac_var}"
                    broadcast_ip="${!broadcast_var:-$DEFAULT_BROADCAST_IP}"
                    auto_wol_value="${!auto_wol_var}"
                    
                    if [[ -z "$ip" || -z "$mac" ]]; then
                        log "warn" "Skipping $name - missing IP or MAC address in configuration."
                        continue
                    fi

                    # Check if auto WoL is enabled for this host.
                    # It's enabled if AUTO_WOL is "true" or not set at all (for backward compatibility).
                    if [[ "$auto_wol_value" != "false" ]]; then
                        if ! $PING_CMD -c 1 -W 1 "$ip" &> /dev/null; then
                            log "info" "Server '$name' ($ip) is offline. Sending WoL packet to $mac via $broadcast_ip."
                            $WAKEONLAN_CMD -i "$broadcast_ip" "$mac"
                            update_client_status "$ip" "wol_sent"
                            WOL_HOSTS_LIST+="- ${name} (${ip})
"
                        else
                            log "info" "Server '$name' ($ip) is already online."
                        fi
                    else
                        log "info" "Skipping WoL for '$name' ($ip) as automatic startup is disabled in the configuration."
                    fi
                done
                
                if [[ -n "$WOL_HOSTS_LIST" ]]; then
                    # Convert literal \n to actual newlines for proper email formatting
                    WOL_EMAIL_BODY="The following hosts have been sent a Wake-on-LAN signal:

$WOL_HOSTS_LIST"
                    send_notification "POWER_RESTORED" "[UPS] INFO: Wake-on-LAN Sequence Initiated" "$WOL_EMAIL_BODY"
                fi

                log "info" "Wake-on-LAN sequence complete. Clearing state file."
                rm -f "$STATE_FILE"
                ### NEW ###
                # Clear client notification states after a full recovery cycle
                log "info" "Power restored and WoL complete. Clearing client notification states."
                rm -f "$CLIENT_NOTIFICATION_STATE_FILE"
                touch "$CLIENT_NOTIFICATION_STATE_FILE"
                ### END NEW ###
            fi
        fi
    fi
fi

### NEW ###
# Check client statuses at the end of every run
check_client_statuses
### END NEW ###

log "info" "--- Power check finished ---"