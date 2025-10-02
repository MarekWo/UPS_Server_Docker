# UPS Server Migration Guide

This guide provides a step-by-step plan for migrating the UPS Server application from one host (Server A) to another (Server B) while preserving all configuration settings.

## Overview

The migration process involves:
- Transferring configuration files from Server A to Server B
- Updating network-specific settings for the new environment
- Rebuilding and redeploying the Docker container
- Updating all UPS clients with the new server address

**Estimated migration time:** 15-30 minutes (depending on the number of clients to update)

---

## Configuration Files to Migrate

The following files contain your server configuration and must be transferred:

### Critical Configuration Files

1. **[config/power_manager.conf](config/power_manager.conf)** - Main configuration file containing:
   - Sentinel host IP addresses (SENTINEL_HOSTS)
   - API authentication token (API_TOKEN)
   - SMTP and email notification settings
   - Wake-on-LAN host definitions with shutdown delays
   - Power outage simulation schedules

2. **[.env](.env)** - Environment variables:
   - Timezone (TZ)
   - UPS Server host IP address (UPS_SERVER_HOST_IP) - **requires update for Server B**

3. **NUT Configuration Files:**
   - [config/nut.conf](config/nut.conf)
   - [config/ups.conf](config/ups.conf)
   - [config/upsd.conf](config/upsd.conf)
   - [config/upsd.users](config/upsd.users)

4. **[docker-compose.yml](docker-compose.yml)** - Docker Compose configuration

5. **[rsyslog/custom.conf](rsyslog/custom.conf)** - Syslog forwarding configuration (if used)

### Optional Files

- **logs/** directory - Historical logs (not required for migration, but can be archived)

---

## Migration Steps

### Phase 1: Prepare Server B

1. **Install required dependencies:**
   ```bash
   apt-get update && apt-get install -y docker docker-compose git curl
   ```

2. **Clone the repository:**
   ```bash
   git clone https://github.com/MarekWo/UPS_Server_Docker.git /opt/ups-server-docker
   cd /opt/ups-server-docker
   ```

### Phase 2: Transfer Configuration from Server A

3. **Copy configuration files from Server A to Server B:**

   Option A - Using SCP (recommended):
   ```bash
   # From Server B, copy files from Server A
   scp user@serverA:/opt/ups-server-docker/config/power_manager.conf /opt/ups-server-docker/config/
   scp user@serverA:/opt/ups-server-docker/.env /opt/ups-server-docker/
   scp user@serverA:/opt/ups-server-docker/config/nut.conf /opt/ups-server-docker/config/
   scp user@serverA:/opt/ups-server-docker/config/ups.conf /opt/ups-server-docker/config/
   scp user@serverA:/opt/ups-server-docker/config/upsd.conf /opt/ups-server-docker/config/
   scp user@serverA:/opt/ups-server-docker/config/upsd.users /opt/ups-server-docker/config/
   scp user@serverA:/opt/ups-server-docker/docker-compose.yml /opt/ups-server-docker/
   scp -r user@serverA:/opt/ups-server-docker/rsyslog/ /opt/ups-server-docker/
   ```

   Option B - Manual backup and restore:
   ```bash
   # On Server A: Create backup archive
   cd /opt/ups-server-docker
   tar -czf ups-server-config-backup.tar.gz config/ .env docker-compose.yml rsyslog/

   # Transfer the archive to Server B
   scp ups-server-config-backup.tar.gz user@serverB:/tmp/

   # On Server B: Extract the archive
   cd /opt/ups-server-docker
   tar -xzf /tmp/ups-server-config-backup.tar.gz
   ```

### Phase 3: Update Configuration on Server B

4. **🔴 CRITICAL: Update the server IP address in `.env` file:**
   ```bash
   nano /opt/ups-server-docker/.env
   ```
   Change `UPS_SERVER_HOST_IP` to the IP address of Server B.

   Example:
   ```bash
   TZ=Europe/Warsaw
   UPS_SERVER_HOST_IP=192.168.1.20  # Update this to Server B's IP
   ```

5. **Review and update network settings in `power_manager.conf` (if network subnet changed):**
   ```bash
   nano /opt/ups-server-docker/config/power_manager.conf
   ```
   Check and update if necessary:
   - `DEFAULT_BROADCAST_IP` - Default broadcast address for Wake-on-LAN
   - `BROADCAST_IP` in each `[WAKE_HOST_X]` section
   - `SENTINEL_HOSTS` - If sentinel device IPs changed

6. **Verify port availability on Server B:**
   ```bash
   # Check if required ports are free
   netstat -tuln | grep -E ':(80|3493|5000) '
   ```
   The following ports must be available:
   - Port 80 - Web GUI
   - Port 3493 - NUT server
   - Port 5000 - REST API

### Phase 4: Deploy on Server B

7. **Build and start the container:**
   ```bash
   cd /opt/ups-server-docker
   docker compose up --build -d
   ```

8. **Monitor the startup logs:**
   ```bash
   docker compose logs -f
   ```
   Press `Ctrl+C` to exit log viewing.

9. **Verify the container is running:**
   ```bash
   docker compose ps
   ```

### Phase 5: Update UPS Clients

10. **Update all UPS clients with the new server address:**

    If you're using the [UPS_monitor](https://github.com/MarekWo/UPS_monitor) client script, update the configuration on each client machine:

    ```bash
    nano /path/to/ups_monitor.conf
    ```
    Update the `UPS_NAME` parameter to use Server B's IP address:
    ```bash
    UPS_NAME=ups@192.168.1.20  # New Server B IP
    ```

### Phase 6: Verification and Testing

11. **Verify web interface access:**

    Open your web browser and navigate to:
    ```
    http://<Server_B_IP>
    ```
    You should see the UPS Server dashboard.

12. **Test API connectivity:**
    ```bash
    curl -H "Authorization: Bearer <your_api_token>" http://<Server_B_IP>:5000/upsc
    ```
    This should return JSON data with UPS status.

13. **Test Wake-on-LAN functionality:**
    - Access the web interface
    - Navigate to the Dashboard
    - Try sending a WoL packet to one of the configured hosts
    - Verify the status updates correctly

14. **Optional: Test power outage simulation:**
    - In the web interface, go to Configuration
    - Enable "Power Outage Simulation Mode"
    - Verify that clients receive the `OB LB` (On Battery, Low Battery) status
    - Disable simulation mode when testing is complete

15. **Monitor client status reporting:**
    - Check the Dashboard to ensure all clients are reporting their status
    - Verify that the "Last Update" timestamps are recent
    - Look for any clients showing "Status Stale" (which indicates communication issues)

### Phase 7: Decommission Server A

16. **Once Server B is verified to be working correctly:**
    ```bash
    # On Server A: Stop the container
    cd /opt/ups-server-docker
    docker compose down
    ```

17. **Keep Server A's configuration as a backup for at least 7 days** before removing it completely.

---

## Troubleshooting

### Issue: Web interface not accessible

**Solution:**
- Verify the container is running: `docker compose ps`
- Check port 80 is not blocked by firewall: `ufw status` (if using UFW)
- Check container logs: `docker compose logs web_gui`

### Issue: Clients cannot connect to NUT server

**Solution:**
- Verify port 3493 is accessible: `telnet <Server_B_IP> 3493`
- Check NUT server logs: `docker compose logs ups-server | grep upsd`
- Ensure clients have updated configuration with new server IP

### Issue: Wake-on-LAN not working

**Solution:**
- Verify `network_mode: host` is enabled in [docker-compose.yml](docker-compose.yml)
- Check broadcast IP addresses in [config/power_manager.conf](config/power_manager.conf)
- Verify target machines have WoL enabled in BIOS/UEFI
- Test WoL manually: `wakeonlan -i <broadcast_ip> <mac_address>`

### Issue: API authentication errors

**Solution:**
- Verify `API_TOKEN` in [config/power_manager.conf](config/power_manager.conf) matches the token used by clients
- Check client configuration files have the correct token
- Restart the container after token changes: `docker compose restart`

### Issue: Email notifications not working

**Solution:**
- Verify SMTP settings in [config/power_manager.conf](config/power_manager.conf)
- Check SMTP server connectivity: `telnet <smtp_server> <smtp_port>`
- Review container logs for SMTP errors: `docker compose logs | grep -i smtp`
- Ensure notification flags are set to "true" in configuration

---

## Post-Migration Checklist

- [ ] Server B container is running successfully
- [ ] Web interface is accessible at `http://<Server_B_IP>`
- [ ] API endpoint returns valid data
- [ ] All UPS clients are updated with new server IP
- [ ] All clients are reporting status (visible in Dashboard)
- [ ] Wake-on-LAN functionality tested and working
- [ ] Email notifications tested (if enabled)
- [ ] Power outage simulation tested (optional but recommended)
- [ ] Server A container stopped
- [ ] Server A configuration backed up

---

## Important Notes

- **Security Consideration:** After migration, consider rotating the `API_TOKEN` for enhanced security
- **Network Mode:** If using `network_mode: host` in [docker-compose.yml](docker-compose.yml), the `ports:` section is ignored
- **Log History:** The `logs/` directory is not required for migration but can be archived for historical reference
- **Timezone:** Ensure the `TZ` variable in `.env` is set correctly for your location to avoid issues with scheduled tasks
- **Backup Strategy:** Always create a full backup before starting the migration process

---

## Additional Resources

- [Main README](README.md) - Complete project documentation
- [Web GUI Documentation](WEB_GUI_README.md) - Web interface guide
- [UPS_monitor Client](https://github.com/MarekWo/UPS_monitor) - Companion client script
- [GitHub Issues](https://github.com/MarekWo/UPS_Server_Docker/issues) - Report problems or request features
- [GitHub Discussions](https://github.com/MarekWo/UPS_Server_Docker/discussions) - Community support

---

## Support

If you encounter any issues during migration:
1. Check the [Troubleshooting](#troubleshooting) section above
2. Review container logs: `docker compose logs -f`
3. Visit [GitHub Discussions](https://github.com/MarekWo/UPS_Server_Docker/discussions) for community support
4. Report bugs at [GitHub Issues](https://github.com/MarekWo/UPS_Server_Docker/issues)

---

**Last Updated:** 2025-10-02
