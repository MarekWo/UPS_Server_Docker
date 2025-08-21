#!/bin/bash

################################################################################
#
# Power Manager for Dummy NUT Server (v1.0.2)
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

# === SCRIPT START ===
log "info" "--- Power check initiated ---"

# Load configuration
if [ ! -f "$CONFIG_FILE" ]; then
    log "err" "CRITICAL ERROR: Configuration file not found at $CONFIG_FILE. Exiting."
    exit 1
fi
source "$CONFIG_FILE"

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

                for HOST_INFO in $WAKE_HOSTS; do
                    IP=$(echo "$HOST_INFO" | cut -d';' -f1)
                    MAC=$(echo "$HOST_INFO" | cut -d';' -f2)

                    # Check if the target server is offline before sending WoL packet
                    if ! $PING_CMD -c 1 -W 1 "$IP" &> /dev/null; then
                        log "info" "Server $IP is offline. Sending WoL packet to $MAC."
                        $WAKEONLAN_CMD -i "${BROADCAST_IP:-192.168.1.255}" "$MAC"
                    else
                        log "info" "Server $IP is already online."
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
