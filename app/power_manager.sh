#!/bin/bash

################################################################################
#
# Power Manager for Dummy NUT Server (v1.0.5)
#
# Author: Marek Wojtaszek (Enhancements by Gemini)
# GitHub: https://github.com/MarekWo/
#
# This script monitors a set of sentinel hosts to infer the status of mains
# power. If all sentinel hosts become unreachable, it assumes a power failure
# and updates the status of a virtual (dummy) NUT server.
#
# v1.0.2 Change: Switched to sequential pinging to prevent race conditions
# and ensure consistent logging, especially in containerized environments.
# v1.0.3 Change: Enhanced configuration format for WAKE_HOSTS with sections
# and added support for per-host broadcast IPs and descriptive names.
# v1.0.4 Change: Fixed config parsing to handle values with spaces correctly,
# both with and without quotes.
# v1.0.5 Change: Setting "WoL sent" status for displaying on dashboard Web UI
#
#
################################################################################

# === CONFIGURATION ===
LOG_FILE="/var/log/power_manager.log"
CONFIG_FILE="/etc/nut/power_manager.conf"
STATE_FILE="/var/run/nut/power_manager.state" # Stores the power state across runs

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

# === FUNCTION TO PARSE MAIN CONFIG VARIABLES ===
parse_main_config() {
    local config_file="$1"
    
    while IFS= read -r line || [[ -n "$line" ]]; do
        # Skip comments and empty lines
        [[ "$line" =~ ^[[:space:]]*# || -z "$line" ]] && continue
        
        # Skip section headers
        [[ "$line" =~ ^\[[[:space:]]*WAKE_HOST_[0-9]+[[:space:]]*\]$ ]] && continue
        
        # Parse key=value pairs for main config only
        if [[ "$line" =~ ^[[:space:]]*([A-Z_]+)[[:space:]]*=[[:space:]]*(.*)$ ]]; then
            key="${BASH_REMATCH[1]}"
            value="${BASH_REMATCH[2]}"
            
            # Remove surrounding quotes if present
            if [[ "$value" =~ ^\"(.*)\"$ ]]; then
                value="${BASH_REMATCH[1]}"
            elif [[ "$value" =~ ^\'(.*)\'$ ]]; then
                value="${BASH_REMATCH[1]}"
            fi
            
            # Export the variable
            declare -g "$key=$value"
        fi
    done < "$config_file"
}

# === FUNCTION TO PARSE WAKE HOSTS FROM CONFIG ===
parse_wake_hosts() {
    local config_file="$1"
    local current_section=""
    local wake_hosts_info=()
    
    # Use a more robust method to handle files without trailing newlines
    while IFS= read -r line || [[ -n "$line" ]]; do
        # Skip comments and empty lines
        [[ "$line" =~ ^[[:space:]]*# || -z "$line" ]] && continue
        
        # Check for section headers [WAKE_HOST_X]
        if [[ "$line" =~ ^\[WAKE_HOST_[0-9]+\]$ ]]; then
            current_section=$(echo "$line" | tr -d '[]')
            continue
        fi
        
        # Only process lines within WAKE_HOST sections
        if [[ "$current_section" =~ ^WAKE_HOST_[0-9]+$ ]]; then
            # Parse key=value pairs
            if [[ "$line" =~ ^[[:space:]]*([A-Z_]+)[[:space:]]*=[[:space:]]*(.*)$ ]]; then
                key="${BASH_REMATCH[1]}"
                value="${BASH_REMATCH[2]}"
                
                # Remove surrounding quotes if present
                if [[ "$value" =~ ^\"(.*)\"$ ]]; then
                    value="${BASH_REMATCH[1]}"
                elif [[ "$value" =~ ^\'(.*)\'$ ]]; then
                    value="${BASH_REMATCH[1]}"
                fi
                
                # Store values with section prefix
                declare -g "${current_section}_${key}=$value"
            fi
        fi
    done < "$config_file"
}

# === FUNCTION TO GET ALL WAKE HOST SECTIONS ===
get_wake_host_sections() {
    # Find all WAKE_HOST section variables
    compgen -v | grep "^WAKE_HOST_[0-9]\+_NAME$" | sed 's/_NAME$//' | sort -V
}

# === FUNCTION TO UPDATE CLIENT STATUS JSON ===
update_client_status() {
    local ip_address="$1"
    local new_status="$2"
    local status_file="/var/run/nut/client_status.json"
    local temp_file=$(mktemp)

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

# Parse the main configuration variables (non-section)
parse_main_config "$CONFIG_FILE"

# Parse wake host sections
parse_wake_hosts "$CONFIG_FILE"

# Debug: Log the parsed SENTINEL_HOSTS value
log "info" "Parsed SENTINEL_HOSTS: '$SENTINEL_HOSTS'"

# === CHECK SENTINEL HOSTS AVAILABILITY ===
log "info" "Pinging sentinel hosts: $SENTINEL_HOSTS"
LIVE_HOSTS_COUNT=0

# --- MODIFIED LOOP ---
# Loop through each host sequentially and wait for the result.
for IP in $SENTINEL_HOSTS; do
    # The ping command now runs in the foreground. The script will pause
    # here for up to 1 second waiting for a response.
    if $PING_CMD -c 1 -W 1 "$IP" &> /dev/null; then
        log "info" "  -> Sentinel host $IP is online."
        LIVE_HOSTS_COUNT=$((LIVE_HOSTS_COUNT + 1))
    else
        log "info" "  -> Sentinel host $IP is offline."
    fi
done

log "info" "Found $LIVE_HOSTS_COUNT online sentinel hosts."

NOW_SECONDS=$(date +%s)

# === MAIN DECISION LOGIC ===
if [ "$LIVE_HOSTS_COUNT" -eq 0 ]; then
    # --- POWER FAILURE DETECTED ---
    log "warn" "Power failure detected! Setting UPS status to OB LB (On Battery, Low Battery)."
    echo "ups.status: OB LB" > "$UPS_STATE_FILE"

    # Create state file to signify a power failure event is in progress
    echo "STATE=POWER_FAIL" > "$STATE_FILE"
    echo "TIMESTAMP=$NOW_SECONDS" >> "$STATE_FILE"

else
    # --- POWER IS ONLINE ---
    log "info" "Power OK ($LIVE_HOSTS_COUNT hosts online). Setting UPS status to OL (Online)."
    echo "ups.status: OL" > "$UPS_STATE_FILE"

    # Check state file to handle Wake-on-LAN logic after power restoration
    if [ -f "$STATE_FILE" ]; then
        source "$STATE_FILE"

        if [ "$STATE" == "POWER_FAIL" ]; then
            # This is the first run after power has been restored
            log "info" "Power restoration detected. Starting $WOL_DELAY_MINUTES minute delay for Wake-on-LAN."
            echo "STATE=POWER_RESTORED" > "$STATE_FILE"
            echo "TIMESTAMP=$NOW_SECONDS" >> "$STATE_FILE"

        elif [ "$STATE" == "POWER_RESTORED" ]; then
            # Power is restored, check if the delay has passed
            DELAY_SECONDS=$((WOL_DELAY_MINUTES * 60))
            TIME_ELAPSED=$((NOW_SECONDS - TIMESTAMP))

            if [ "$TIME_ELAPSED" -ge "$DELAY_SECONDS" ]; then
                log "info" "WoL delay has passed. Initiating wake-up sequence for servers."

                # Process each WAKE_HOST section
                for section in $(get_wake_host_sections); do
                    # Get variables for this section
                    name_var="${section}_NAME"
                    ip_var="${section}_IP"
                    mac_var="${section}_MAC"
                    broadcast_var="${section}_BROADCAST_IP"
                    
                    # Get values (using indirect variable expansion)
                    name="${!name_var:-Unknown Host}"
                    ip="${!ip_var}"
                    mac="${!mac_var}"
                    broadcast_ip="${!broadcast_var:-$DEFAULT_BROADCAST_IP}"
                    
                    # Skip if essential info is missing
                    if [[ -z "$ip" || -z "$mac" ]]; then
                        log "warn" "Skipping $name - missing IP or MAC address in configuration."
                        continue
                    fi

                    # Check if the target server is offline before sending WoL packet
                    if ! $PING_CMD -c 1 -W 1 "$ip" &> /dev/null; then
                        log "info" "Server '$name' ($ip) is offline. Sending WoL packet to $mac via $broadcast_ip."
                        $WAKEONLAN_CMD -i "$broadcast_ip" "$mac"
                        # Set the status to 'wol_sent' after sending the packet
                        update_client_status "$ip" "wol_sent"
                    else
                        log "info" "Server '$name' ($ip) is already online."
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