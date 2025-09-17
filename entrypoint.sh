#!/bin/bash

# Exit immediately if a command exits with a non-zero status.
set -e

echo "--- Starting UPS Server Container ---"

# --- 0. Setup Version Information ---
echo "Setting up version information..."

# Copy advanced CLI tool if available from scripts directory
if [ -f /scripts/ups-version ]; then
    echo "Installing advanced CLI tool..."
    cp /scripts/ups-version /usr/local/bin/ups-version
    chmod +x /usr/local/bin/ups-version
else
    echo "Installing basic CLI wrapper..."
    # Create simple bash wrapper for global access (fallback)
    cat > /usr/local/bin/ups-version << 'EOF'
#!/bin/bash
cd /app && python3 version_info.py "$@"
EOF
    chmod +x /usr/local/bin/ups-version
fi

# Ensure version info is available (freeze if not already done)
if [ ! -f /app/version_info.json ] && [ -f /app/version_info.py ]; then
    echo "Freezing version information..."
    cd /app && python3 version_info.py freeze || echo "Warning: Could not freeze version"
fi

# Display current version
if [ -f /app/version_info.py ]; then
    echo "📦 Current version:"
    cd /app && python3 version_info.py info
fi

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

# --- 3. Setup and Start Cron Service (Universal Timezone Solution) ---
echo "Setting up cron job with Timezone: ${TZ:-UTC}"

# Dynamically create the cron file using the TZ variable from docker-compose
# This ensures the cron job runs in the user-specified timezone
echo "TZ=${TZ:-UTC}" > /etc/cron.d/power-manager-cron
echo "* * * * * root /usr/local/bin/python /app/power_manager.py" >> /etc/cron.d/power-manager-cron

# Apply correct permissions to the cron file
chmod 0644 /etc/cron.d/power-manager-cron

echo "Starting cron daemon..."
cron

# --- 4. Start the Web GUI ---
echo "Starting Gunicorn for Web GUI on port 80..."
gunicorn --workers 2 --bind 0.0.0.0:80 --chdir /app web_gui:app --daemon

# --- 5. Start the UPS Hub API ---
echo "Starting Gunicorn for UPS Hub API on port 5000..."
exec gunicorn --workers 3 --bind 0.0.0.0:5000 --chdir /app api:app