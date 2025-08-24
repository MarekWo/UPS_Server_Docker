# Web GUI for UPS Power Management Server

## Overview

The Web GUI for UPS Server provides easy management of system configuration through a web interface accessible from any browser. The interface is fully responsive and optimized for both desktop and mobile devices.

## Web GUI Features

### 🏠 Dashboard
- **System Overview**: Displays numerical statistics (number of hosts, sentinels, clients)
- **Sentinel Host Status**: Real-time monitoring of hosts used to detect power failures
- **UPS Client Status**: Shows online/offline status of all UPS clients with shutdown delays
- **Managed Host Status**: Displays status of all hosts with Wake-on-LAN capability
- **Automatic Refresh**: Host status is automatically refreshed every 30 seconds
- **One-Click WoL**: Buttons to instantly send Wake-on-LAN signals to selected hosts

![Dashboard](/images/web-ui-dashboard.png)

### ⚙️ Configuration
- **Main Configuration**: Edit system parameters (sentinel hosts, WoL delay, broadcast address)
- **Managed Host Management**: Add, edit, and remove hosts with Wake-on-LAN and UPS client functionality
- **Data Validation**: Automatic validation of IP addresses and MAC addresses with error feedback
- **Auto-formatting**: Intelligent MAC address formatting during input
- **Unified Configuration**: Single interface for all host parameters including optional UPS client settings

![Configuration](/images/web-ui-config.png)

## Installation and Configuration

### 1. Update Application Files

Copy the new files to your application directory:

```bash
# Navigate to the application directory
cd /opt/ups-server-docker/app

# Create templates directory
mkdir -p templates

# Copy new files (web_gui.py, templates/base.html, templates/dashboard.html, templates/config.html)
```

### 2. Docker Updates

Replace existing files:
- `entrypoint.sh` - adds Web GUI startup on port 80
- `Dockerfile` - adds port 80 and templates directory
- `docker-compose.yml.example` - documents port 80
- `api.py` - updated to use consolidated configuration

### 3. Container Rebuild

```bash
# Stop existing container
docker compose down

# Rebuild and start with new files
docker compose up --build -d
```

## Accessing the Web GUI

After starting the container, the Web GUI will be available at:

```
http://<UPS_SERVER_IP>
```

For example, if your UPS server has IP `192.168.1.10`, open:

```
http://192.168.1.10
```

## Ports and Services

After the update, the container will provide the following services:

- **Port 80**: Web GUI (new)
- **Port 5000**: REST API (existing)
- **Port 3493**: NUT Server (existing)

## Configuration Management

### Unified Configuration File

All system configuration is now managed through a single file: `power_manager.conf`. This file contains:

- **Main Settings**: Sentinel hosts, WoL delay, broadcast addresses
- **Host Definitions**: Each `[WAKE_HOST_X]` section can include:
  - `NAME`: Descriptive name
  - `IP`: Host IP address
  - `MAC`: MAC address for Wake-on-LAN
  - `BROADCAST_IP`: Optional specific broadcast IP
  - `SHUTDOWN_DELAY_MINUTES`: Optional - makes host a UPS client

### Example Configuration

```bash
# === CONFIGURATION FILE FOR POWER_MANAGER.SH ===

SENTINEL_HOSTS=192.168.1.11 192.168.1.12 192.168.1.13
WOL_DELAY_MINUTES=5
DEFAULT_BROADCAST_IP=192.168.1.255
UPS_STATE_FILE=/var/run/nut/virtual.device

# === WAKE-ON-LAN HOST DEFINITIONS ===

# UPS Client (with shutdown delay)
[WAKE_HOST_1]
NAME=Synology NAS
IP=192.168.1.12
MAC=00:11:32:f8:af:9f
SHUTDOWN_DELAY_MINUTES=10

# WoL-only host (no UPS functionality)
[WAKE_HOST_2]
NAME=File Server
IP=192.168.1.15
MAC=00:11:32:aa:bb:cc
```

## Responsive Design and Mobile Optimization

The Web GUI is designed for accessibility across all devices:

### 📱 Mobile Features:
- **Responsive design**: Automatic adaptation to screen size
- **Touch-friendly**: Large buttons and elements easy to tap
- **Scrollable tables**: Tables with large amounts of data are horizontally scrollable
- **Optimized forms**: Modal dialogs adapted to mobile screens
- **Clear icons**: Font Awesome icons with clear status indicators

### 💻 Desktop Features:
- **Hover effects**: Animations on mouse hover
- **Advanced tables**: Full tables with more information
- **Wide layouts**: Full utilization of screen width
- **Keyboard navigation**: Full keyboard accessibility

## Security

### ⚠️ Important Security Notes:

1. **No Authentication**: The Web GUI does not have a login system. Access is open to anyone who knows the server's IP address.

2. **Internal Use**: The interface is designed for use within a secure internal network.

3. **No HTTPS**: Communication occurs over unencrypted HTTP.

### 🔒 Security Recommendations:

- Use the Web GUI only on trusted local networks
- Consider restricting access through firewall rules
- For production environments, consider adding authentication

## Troubleshooting

### Web GUI doesn't load
1. Check if container is running: `docker ps`
2. Check logs: `docker compose logs`
3. Check port 80 availability: `curl http://localhost`

### Cannot save configuration
1. Check permissions on configuration files
2. Check application logs: `docker compose logs ups-server`
3. Verify configuration files exist in `./config/`

### Host status doesn't refresh
1. Check network connectivity from container
2. Check browser logs (F12 -> Console)
3. Verify `/status` endpoint responds: `curl http://<SERVER_IP>/status`

### Wake-on-LAN problems
1. Ensure you're using `network_mode: host` in docker-compose.yml
2. Check if `wakeonlan` is installed in the container
3. Verify MAC addresses and broadcast IP addresses are correct

## Advanced Features

### Automatic Status Refresh
- Status of all hosts is automatically refreshed every 30 seconds
- You can manually refresh status by clicking the "Refresh" button

### Form Validation
- IP addresses are automatically validated
- MAC addresses are formatted during input (XX:XX:XX:XX:XX:XX)
- Invalid data is highlighted with error messages

### Notifications
- All actions (saving, adding, deleting) show notifications
- Notifications automatically disappear after 3-5 seconds
- Errors are displayed in red, success messages in green

## API Integration

The Web GUI uses the same REST API that serves UPS clients:

- `GET /config?ip=<client_ip>` - Returns UPS client configuration
- `GET /upsc` - Returns live UPS status
- `GET /status` - Returns current host status (used by Web GUI)

Example API response for a UPS client:
```bash
curl -H "Authorization: Bearer <token>" "http://server:5000/config?ip=192.168.1.12"
```
```json
{
  "SHUTDOWN_DELAY_MINUTES": "10",
  "UPS_NAME": "ups@192.168.1.10"
}
```

## Migration from Previous Versions

If upgrading from a version that used separate `upshub.conf`:

1. **Backup existing configuration**:
   ```bash
   cp config/power_manager.conf config/power_manager.conf.backup
   ```

2. **Add UPS client settings**: For each host that should be a UPS client, add `SHUTDOWN_DELAY_MINUTES` to its `[WAKE_HOST_X]` section.

3. **Remove old file**: The `upshub.conf` file is no longer needed.

4. **Update Web GUI**: Replace `web_gui.py` and templates with the unified versions.

## Web GUI Endpoints

The Web GUI provides these endpoints for management:

- `GET /` - Main dashboard
- `GET /config` - Configuration page
- `POST /save_main_config` - Save main configuration
- `POST /add_wake_host` - Add new managed host
- `POST /edit_wake_host/<section>` - Edit managed host
- `POST /delete_wake_host/<section>` - Delete managed host
- `GET /wol/<section>` - Send Wake-on-LAN signal
- `GET /status` - Get current host status (JSON)

All Web GUI endpoints are independent of the existing REST API on port 5000, which continues to work without changes for UPS clients.