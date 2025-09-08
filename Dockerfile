# ===================================================================
# Dockerfile for UPS Server (Single-Stage Build with Logrotate & Rsyslog)
# ===================================================================

# Start from a Debian base image that includes Python
FROM python:3.11-slim-bookworm

# Set environment variables to prevent interactive prompts during installation
ENV DEBIAN_FRONTEND=noninteractive

# Install all necessary system packages in a single layer
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    nut-server \
    iputils-ping \
    wakeonlan \
    cron \
    curl \
    jq \
    logrotate \
    rsyslog \
    tzdata && \
    # Clean up the apt cache to reduce image size
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies directly using the system's pip
RUN pip install --no-cache-dir gunicorn flask

# Create the application directory
WORKDIR /app

# Copy the application scripts into the container
COPY app/ .

# Create templates directory for Web GUI
RUN mkdir -p /app/templates

# Make the power manager script executable
RUN chmod +x /app/power_manager.sh

# --- Logrotate Setup ---
COPY logrotate/power-manager-logrotate /etc/logrotate.d/

# --- Rsyslog Setup ---
# Note: The actual rsyslog config is mounted via docker-compose.yml
# This Dockerfile only ensures the service is installed.

# --- NUT Configuration ---
RUN mkdir -p /var/run/nut && \
    chown -R nut:nut /var/run/nut

# --- Expose Ports ---
EXPOSE 3493
EXPOSE 5000
EXPOSE 80

# Copy the entrypoint script and make it executable
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Set the entrypoint script as the command to run when the container starts
ENTRYPOINT ["/entrypoint.sh"]