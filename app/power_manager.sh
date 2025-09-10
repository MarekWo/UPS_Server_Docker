#!/bin/bash

################################################################################
#
# Power Manager for Dummy NUT Server (v1.3.1 - Enhanced Logging)
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
#
################################################################################

# === CONFIGURATION ===
LOG_FILE="/var/log/power_manager.log"
CONFIG_FILE="/etc/nut/power_manager.conf"
STATE_FILE="/var/run/nut/power_manager.state" # Stores the power state across runs
NOTIFICATION_STATE_FILE="/var/run/nut/notification.state" # Stores notification timestamps
UPS_STATE_FILE_DEFAULT="/var/run/nut/virtual.device"
EMAIL_SENDER_SCRIPT="/app/send_email.py"

# Absolute paths to commands for cron compatibility
PING_CMD="/bin/ping"
WAKEONLAN_CMD="/usr/bin/wakeonlan"
PYTHON_CMD="/usr/local/bin/python"

# Notification debounce time in seconds (1 hour)
NOTIFICATION_DEBOUNCE_SECONDS=3600

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

# Use UPS_STATE_FILE from config, or default if not set
UPS_STATE_FILE="${UPS_STATE_FILE:-$UPS_STATE_FILE_DEFAULT}"

# Load previous state
PREVIOUS_STATE=""
if [ -f "$STATE_FILE" ]; then
    source "$STATE_FILE" # This loads STATE as PREVIOUS_STATE for this run
    PREVIOUS_STATE="$STATE"
fi

# --- MAIN DECISION LOGIC ---
LIVE_HOSTS_COUNT=0
POWER_STATUS="ONLINE"

if [[ "${POWER_SIMULATION_MODE}" == "true" ]]; then
    log "warn" "Power Outage Simulation is active. Forcing power status to OFFLINE."
    POWER_STATUS="OFFLINE"
else
    log "info" "Pinging sentinel hosts: $SENTEL_HOSTS"
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
                    name="${!section_NAME:-Unknown Host}"
                    ip="${!section_IP}"
                    mac="${!section_MAC}"
                    broadcast_ip="${!section_BROADCAST_IP:-$DEFAULT_BROADCAST_IP}"
                    auto_wol="${!section_AUTO_WOL}"
                    
                    if [[ -z "$ip" || -z "$mac" ]]; then
                        log "warn" "Skipping $name - missing IP or MAC address in configuration."
                        continue
                    fi

                    if [[ "$auto_wol" != "false" ]]; then
                        if ! $PING_CMD -c 1 -W 1 "$ip" &> /dev/null; then
                            log "info" "Server '$name' ($ip) is offline. Sending WoL packet to $mac."
                            $WAKEONLAN_CMD -i "$broadcast_ip" "$mac"
                            WOL_HOSTS_LIST+="- ${name} (${ip})\n"
                        fi
                    fi
                done
                
                if [[ -n "$WOL_HOSTS_LIST" ]]; then
                    send_notification "POWER_RESTORED" "[UPS] INFO: Wake-on-LAN Sequence Initiated" "The following hosts have been sent a Wake-on-LAN signal:\n\n$WOL_HOSTS_LIST"
                fi

                log "info" "Wake-on-LAN sequence complete. Clearing state file."
                rm -f "$STATE_FILE"
            fi
        fi
    fi
fi

log "info" "--- Power check finished ---"