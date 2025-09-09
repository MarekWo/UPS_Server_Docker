#!/bin/bash

################################################################################
#
# Power Manager for Dummy NUT Server (v1.2.0 - Selective WoL Edition)
#
# Author: Marek Wojtaszek (Enhancements by Gemini)
# GitHub: https://github.com/MarekWo/
#
# This script monitors sentinel hosts or a schedule to determine power status
# and updates a virtual (dummy) NUT server.
#
# v1.0.2 Change: Switched to sequential pinging.
# v1.0.3 Change: Enhanced configuration for WAKE_HOSTS.
# v1.0.4 Change: Fixed config parsing for values with spaces.
# v1.0.5 Change: Added "WoL sent" status for Web UI.
# v1.0.6 Change: Added manual Power Outage Simulation mode.
# v1.1.0 Change: Added scheduler for Power Outage Simulation.
# v1.1.2 Change: Fixed regex in get_sections function to correctly find schedules.
# v1.2.0 Change: Added selective auto WoL option per host (AUTO_WOL="false").
#
################################################################################

# === CONFIGURATION ===
LOG_FILE="/var/log/power_manager.log"
CONFIG_FILE="/etc/nut/power_manager.conf"
STATE_FILE="/var/run/nut/power_manager.state" # Stores the power state across runs
UPS_STATE_FILE_DEFAULT="/var/run/nut/virtual.device"

# Absolute paths to commands for cron compatibility
PING_CMD="/bin/ping"
WAKEONLAN_CMD="/usr/bin/wakeonlan"

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
        # Trim leading/trailing whitespace
        line=$(echo "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')

        # Skip comments and empty lines
        if [[ "$line" =~ ^# || -z "$line" ]]; then
            continue
        fi

        # Check for section headers
        if [[ "$line" =~ ^\[(.*)\]$ ]]; then
            current_section="${BASH_REMATCH[1]}"
            continue
        fi

        # Parse key=value pairs
        if [[ "$line" =~ ^([^=]+)=(.*)$ ]]; then
            key=$(echo "${BASH_REMATCH[1]}" | sed 's/[[:space:]]*$//')
            value=$(echo "${BASH_REMATCH[2]}" | sed 's/^[[:space:]]*//')

            # Remove quotes from value
            value="${value#\"}"; value="${value%\"}"
            value="${value#\'}"; value="${value%\'}"
            
            if [[ -n "$current_section" ]]; then
                # It's a section variable (WAKE_HOST_X or SCHEDULE_X)
                declare -g "${current_section}_${key}=$value"
            else
                # It's a main config variable
                declare -g "$key=$value"
            fi
        fi
    done < "$config_file"
}

# === FUNCTION TO GET ALL SECTIONS OF A GIVEN TYPE ===
get_sections() {
    local type_prefix="$1"
    # Use grep with Extended Regular Expressions (-E) to correctly handle '+' for one or more digits.
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

# === SCRIPT START ===
log "info" "--- Power check initiated ---"

# Load configuration
if [ ! -f "$CONFIG_FILE" ]; then
    log "err" "CRITICAL ERROR: Configuration file not found at $CONFIG_FILE. Exiting."
    exit 1
fi
parse_config "$CONFIG_FILE"

# Use UPS_STATE_FILE from config, or default if not set
UPS_STATE_FILE="${UPS_STATE_FILE:-$UPS_STATE_FILE_DEFAULT}"

# --- SCHEDULE CHECK LOGIC ---
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
elif [[ "$SCHEDULE_SIMULATION_ACTION" == "stop" ]]; then
    log "info" "Scheduled action: STOPPING Power Outage Simulation."
    sed -i 's/^\(POWER_SIMULATION_MODE\s*=\s*\).*/\1\"false\"/' "$CONFIG_FILE"
    POWER_SIMULATION_MODE="false" # Update live variable for this run
fi

# --- MAIN DECISION LOGIC ---
LIVE_HOSTS_COUNT=0
POWER_STATUS="ONLINE" # Assume power is online by default

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

# --- STATE UPDATE AND WoL LOGIC ---
NOW_SECONDS=$(date +%s)

if [ "$POWER_STATUS" == "OFFLINE" ]; then
    log "warn" "Power failure detected! Setting UPS status to OB LB."
    echo "ups.status: OB LB" > "$UPS_STATE_FILE"
    echo "STATE=POWER_FAIL" > "$STATE_FILE"
    echo "TIMESTAMP=$NOW_SECONDS" >> "$STATE_FILE"
else
    log "info" "Power is ONLINE. Setting UPS status to OL."
    echo "ups.status: OL" > "$UPS_STATE_FILE"

    if [ -f "$STATE_FILE" ]; then
        source "$STATE_FILE" # Load STATE and TIMESTAMP

        if [ "$STATE" == "POWER_FAIL" ]; then
            log "info" "Power restoration detected. Starting $WOL_DELAY_MINUTES minute delay for Wake-on-LAN."
            echo "STATE=POWER_RESTORED" > "$STATE_FILE"
            echo "TIMESTAMP=$NOW_SECONDS" >> "$STATE_FILE"
        elif [ "$STATE" == "POWER_RESTORED" ]; then
            DELAY_SECONDS=$((WOL_DELAY_MINUTES * 60))
            TIME_ELAPSED=$((NOW_SECONDS - TIMESTAMP))

            if [ "$TIME_ELAPSED" -ge "$DELAY_SECONDS" ]; then
                log "info" "WoL delay has passed. Initiating wake-up sequence for servers."

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
                        else
                            log "info" "Server '$name' ($ip) is already online."
                        fi
                    else
                        log "info" "Skipping WoL for '$name' ($ip) as automatic startup is disabled in the configuration."
                    fi
                done

                log "info" "Wake-on-LAN sequence complete. Clearing state file."
                rm -f "$STATE_FILE"
            else
                REMAINING=$(( (DELAY_SECONDS - TIME_ELAPSED) / 60 ))
                log "info" "Power is restored. Waiting for WoL. Approx. $REMAINING minutes remaining."
            fi
        fi
    fi
fi

log "info" "--- Power check finished ---"