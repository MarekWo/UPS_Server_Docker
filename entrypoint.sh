#!/bin/bash

# Exit immediately if a command exits with a non-zero status.
set -e

echo "--- Starting UPS Server Container ---"

# --- 1. Initialize NUT ---
echo "Initializing NUT state file..."
touch /var/run/nut/virtual.device
chown nut:nut /var/run/nut/virtual.device

echo "Starting NUT drivers..."
upsdrvctl -u root start

echo "Starting NUT server daemon (upsd)..."
upsd -u root

# --- 2. Start Rsyslog Service ---
echo "Starting rsyslog daemon..."
rsyslogd

# --- 3. Start Cron Service ---
echo "Starting cron daemon..."
cron

# --- 4. Start the UPS Hub API ---
echo "Starting Gunicorn for UPS Hub API..."
exec gunicorn --workers 3 --bind 0.0.0.0:5000 --chdir /app api:app
