#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import subprocess
import configparser
import json
import logging
import logging.handlers
import re
from datetime import datetime, timedelta
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr

# --- Constants ---
APP_NAME = "PowerManager"
LOG_FILE = "/var/log/power_manager.log"
CONFIG_FILE = "/etc/nut/power_manager.conf"
STATE_FILE = "/var/run/nut/power_manager.state"
NOTIFICATION_STATE_FILE = "/var/run/nut/notification.state"
CLIENT_STATUS_FILE = "/var/run/nut/client_status.json"
CLIENT_NOTIFICATION_STATE_FILE = "/var/run/nut/client_notification.state"
UPS_STATE_FILE_DEFAULT = "/var/run/nut/virtual.device"

# Commands
PING_CMD = "/bin/ping"
WAKEONLAN_CMD = "/usr/bin/wakeonlan"

# --- Logger Setup ---
def setup_logging():
    """Configures logging to file and syslog."""
    logger = logging.getLogger(APP_NAME)
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', '%Y-%m-%d %H:%M:%S')

    # File handler for detailed logs
    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # Syslog handler for system-wide integration
    try:
        syslog_handler = logging.handlers.SysLogHandler(address='/dev/log')
        syslog_formatter = logging.Formatter(f'{APP_NAME}[%(process)d]: %(message)s')
        syslog_handler.setFormatter(syslog_formatter)
        syslog_handler.setLevel(logging.INFO)
        logger.addHandler(syslog_handler)
    except (IOError, OSError):
        logger.warning("Could not connect to syslog. Logging to file only.")

    return logger

log = setup_logging()

# --- Core Classes ---

class ConfigManager:
    """Handles reading and writing the power_manager.conf file."""
    def __init__(self, filepath):
        self.filepath = filepath
        self.config = None
        self.load()

    def load(self):
        """Loads the configuration from the file."""
        if not os.path.exists(self.filepath):
            raise FileNotFoundError(f"Configuration file not found at {self.filepath}")
        
        self.config = configparser.ConfigParser(interpolation=None, allow_no_value=True)
        self.config.optionxform = str # Preserve case

        with open(self.filepath, 'r') as f:
            content = f.read()
        
        # Prepend a [global] section to handle key-value pairs before the first section
        first_section_pos = content.find('[')
        if first_section_pos > 0 or (first_section_pos == -1 and content.strip()):
             content = "[global]\n" + content

        self.config.read_string(content)

    def get_main(self, key, fallback=None):
        """Gets a value from the main (global) configuration."""
        return self.config.get('global', key, fallback=fallback)

    def get_sections(self, prefix):
        """Gets all sections starting with a given prefix."""
        return [s for s in self.config.sections() if s.startswith(prefix)]

    def get_wake_hosts(self):
        """Returns a dictionary of all wake hosts."""
        return {s: dict(self.config.items(s)) for s in self.get_sections('WAKE_HOST_')}

    def get_schedules(self):
        """Returns a dictionary of all schedules."""
        return {s: dict(self.config.items(s)) for s in self.get_sections('SCHEDULE_')}

    def save_setting(self, key, value, section=None):
        """Saves a single setting back to the file, preserving comments and structure."""
        key_regex = re.compile(rf"^\s*{re.escape(key)}\s*=", re.IGNORECASE)
        
        with open(self.filepath, 'r') as f:
            lines = f.readlines()

        with open(self.filepath, 'w') as f:
            in_correct_section = section is None
            for line in lines:
                stripped = line.strip()
                if stripped.startswith('[') and stripped.endswith(']'):
                    current_section = stripped[1:-1]
                    in_correct_section = current_section == section

                if in_correct_section and key_regex.match(stripped):
                    original_key_part = line.split('=')[0]
                    f.write(f'{original_key_part}= "{value}"\n')
                else:
                    f.write(line)

class Notifier:
    """Handles sending email notifications."""
    def __init__(self, config_manager):
        self.config = config_manager
        self.debounce_file = NOTIFICATION_STATE_FILE
        if not os.path.exists(self.debounce_file):
            open(self.debounce_file, 'a').close()

    def send(self, n_type, subject, body):
        """Sends a notification if enabled and not debounced."""
        enabled_var = f"NOTIFY_{n_type.upper()}"
        if self.config.get_main(enabled_var, 'false').lower() != 'true':
            log.info(f"Notification for {n_type} is disabled. Skipping.")
            return

        if n_type == "APP_ERROR":
            debounce_seconds = 3600 # From original script
            last_sent = self._get_debounce_timestamp(n_type)
            if last_sent and (datetime.now() - last_sent).total_seconds() < debounce_seconds:
                log.warning(f"Error notification for {n_type} is debounced. Skipping.")
                return
            self._set_debounce_timestamp(n_type)

        log.info(f"Sending notification: {subject}")
        try:
            self._send_email(subject, body)
        except Exception as e:
            log.error(f"Failed to send email notification. Reason: {e}")
            if n_type != "APP_ERROR":
                self.send("APP_ERROR", "[UPS] CRITICAL: Email Sending Failed",
                          f"The UPS server failed to send an email notification. Error: {e}")

    def _get_debounce_timestamp(self, n_type):
        try:
            with open(self.debounce_file, 'r') as f:
                for line in f:
                    if line.startswith(f"{n_type}_LAST_SENT="):
                        return datetime.fromtimestamp(int(line.strip().split('=')[1]))
        except (IOError, ValueError, IndexError):
            return None
        return None

    def _set_debounce_timestamp(self, n_type):
        try:
            lines = []
            if os.path.exists(self.debounce_file):
                with open(self.debounce_file, 'r') as f:
                    lines = [l for l in f if not l.startswith(f"{n_type}_LAST_SENT=")]
            lines.append(f"{n_type}_LAST_SENT={int(datetime.now().timestamp())}\n")
            with open(self.debounce_file, 'w') as f:
                f.writelines(lines)
        except IOError as e:
            log.error(f"Could not update debounce timestamp file: {e}")

    def _send_email(self, subject, body):
        smtp_server = self.config.get_main('SMTP_SERVER')
        smtp_port = int(self.config.get_main('SMTP_PORT', 587))
        smtp_user = self.config.get_main('SMTP_USER')
        smtp_password = self.config.get_main('SMTP_PASSWORD')
        sender_name = self.config.get_main('SMTP_SENDER_NAME', 'UPS Server')
        sender_email = self.config.get_main('SMTP_SENDER_EMAIL')
        recipients = [e.strip() for e in self.config.get_main('SMTP_RECIPIENTS', '').split(',') if e.strip()]

        if not all([smtp_server, sender_email, recipients]):
            raise ValueError("SMTP server, sender email, and recipients must be configured.")

        msg = MIMEText(body, 'plain', 'utf-8')
        msg['Subject'] = subject
        msg['From'] = formataddr((sender_name, sender_email))
        msg['To'] = ', '.join(recipients)

        server = None
        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=10)
        else:
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=10)
            if smtp_port != 26:
                server.starttls()
        
        if smtp_user and smtp_password:
            server.login(smtp_user, smtp_password)
        
        server.sendmail(sender_email, recipients, msg.as_string())
        server.quit()

class PowerManager:
    """Main application logic."""
    def __init__(self):
        try:
            self.config = ConfigManager(CONFIG_FILE)
        except FileNotFoundError as e:
            log.error(f"CRITICAL ERROR: {e}. Exiting.")
            sys.exit(1)
        
        self.notifier = Notifier(self.config)
        self.power_state = None
        self.power_state_timestamp = None
        self.client_notification_states = {}

    def _load_state(self):
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r') as f:
                for line in f:
                    if '=' in line:
                        key, value = line.strip().split('=', 1)
                        if key == 'STATE': self.power_state = value
                        elif key == 'TIMESTAMP': self.power_state_timestamp = int(value)
        
        if os.path.exists(CLIENT_NOTIFICATION_STATE_FILE):
            with open(CLIENT_NOTIFICATION_STATE_FILE, 'r') as f:
                for line in f:
                    if '=' in line:
                        key, value = line.strip().split('=', 1)
                        self.client_notification_states[key] = value == 'true'

    def _save_power_state(self, state):
        with open(STATE_FILE, 'w') as f:
            f.write(f"STATE={state}\n")
            f.write(f"TIMESTAMP={int(datetime.now().timestamp())}\n")

    def _clear_file(self, filepath):
        if os.path.exists(filepath):
            os.remove(filepath)
        open(filepath, 'a').close()

    def _update_ups_status_file(self, status_line):
        ups_file = self.config.get_main('UPS_STATE_FILE', UPS_STATE_FILE_DEFAULT)
        with open(ups_file, 'w') as f:
            f.write(status_line + '\n')

    def _check_schedules(self):
        now = datetime.now()
        for section, params in self.config.get_schedules().items():
            if params.get('ENABLED', 'false').lower() != 'true': continue

            match = False
            if params.get('TYPE') == 'one-time' and params.get('DATE') == now.strftime('%Y-%m-%d') and params.get('TIME') == now.strftime('%H:%M'):
                match = True
            elif params.get('TYPE') == 'recurring' and params.get('TIME') == now.strftime('%H:%M'):
                dow = params.get('DAY_OF_WEEK', '').lower()
                if dow == 'everyday' or dow == now.strftime('%A').lower():
                    match = True
            
            if match:
                action, name = params.get('ACTION', '').lower(), params.get('NAME', section)
                log.info(f"Schedule match: [{name}] triggers action [{action}].")
                
                if action == 'start':
                    self.config.save_setting('POWER_SIMULATION_MODE', 'true')
                    self.notifier.send("SIMULATION_MODE", "[UPS] INFO: Power Outage Simulation Started", "Scheduled start of power outage simulation.")
                elif action == 'stop':
                    self.config.save_setting('POWER_SIMULATION_MODE', 'false')
                    self.notifier.send("SIMULATION_MODE", "[UPS] INFO: Power Outage Simulation Stopped", "Scheduled stop of power outage simulation.")
                
                if params.get('TYPE') == 'one-time':
                    self.config.save_setting('ENABLED', 'false', section=section)
                
                self.config.load() # Reload config after potential change
                break

    def _determine_power_status(self):
        if self.config.get_main('POWER_SIMULATION_MODE', 'false').lower() == 'true':
            log.warning("Power Outage Simulation is active. Forcing OFFLINE.")
            return "OFFLINE"
        
        sentinel_hosts = self.config.get_main('SENTINEL_HOSTS', '').split()
        if not sentinel_hosts: return "ONLINE"

        for ip in sentinel_hosts:
            try:
                if subprocess.run([PING_CMD, "-c", "1", "-W", "1", ip], capture_output=True, timeout=2).returncode == 0:
                    log.info(f"Sentinel host {ip} is online. Power is ON.")
                    return "ONLINE"
            except subprocess.TimeoutExpired:
                pass
        
        log.warning("All sentinel hosts are offline. Power is OFF.")
        return "OFFLINE"

    def _handle_power_offline(self):
        if self.power_state != "POWER_FAIL":
            log.warning("STATE CHANGE: Power failure detected!")
            self._clear_file(CLIENT_NOTIFICATION_STATE_FILE)
            self.client_notification_states = {}
            self.notifier.send("POWER_FAIL", "[UPS] ALERT: Power Outage Detected", "All sentinel hosts are offline. System is on UPS power.")
            self._save_power_state("POWER_FAIL")
        self._update_ups_status_file("ups.status: OB LB")

    def _handle_power_online(self):
        self._update_ups_status_file("ups.status: OL")
        if not self.power_state: return

        now_ts = int(datetime.now().timestamp())
        wol_delay = int(self.config.get_main('WOL_DELAY_MINUTES', 5))
        
        if self.power_state == "POWER_FAIL":
            duration = (now_ts - self.power_state_timestamp) // 60
            log.info("STATE CHANGE: Power restoration detected.")
            self.notifier.send("POWER_RESTORED", "[UPS] INFO: Power Restored",
                               f"Power restored after ~{duration} mins. Waiting {wol_delay} mins for WoL.")
            self._save_power_state("POWER_RESTORED")

        elif self.power_state == "POWER_RESTORED":
            if (now_ts - self.power_state_timestamp) >= (wol_delay * 60):
                log.info("WoL delay passed. Initiating wake-up sequence.")
                self._initiate_wol()
                self._clear_file(STATE_FILE)
                self._clear_file(CLIENT_NOTIFICATION_STATE_FILE)

    def _initiate_wol(self):
        wake_hosts = self.config.get_wake_hosts()
        default_broadcast = self.config.get_main('DEFAULT_BROADCAST_IP')
        woken_hosts = []

        for params in wake_hosts.values():
            if params.get('AUTO_WOL', 'true').lower() == 'false': continue
            ip, mac = params.get('IP'), params.get('MAC')
            if not ip or not mac: continue

            if subprocess.run([PING_CMD, "-c", "1", "-W", "1", ip], capture_output=True).returncode != 0:
                broadcast = params.get('BROADCAST_IP', default_broadcast)
                log.info(f"Sending WoL to {params.get('NAME')} ({ip}) via {broadcast}.")
                subprocess.run([WAKEONLAN_CMD, "-i", broadcast, mac])
                self._update_client_status_json(ip, "wol_sent")
                woken_hosts.append(f"- {params.get('NAME')} ({ip})")

        if woken_hosts:
            self.notifier.send("POWER_RESTORED", "[UPS] INFO: WoL Sequence Initiated",
                               "Sent WoL signals to:\n\n" + "\n".join(woken_hosts))

    def _update_client_status_json(self, ip, status):
        statuses = {}
        if os.path.exists(CLIENT_STATUS_FILE):
            try:
                with open(CLIENT_STATUS_FILE, 'r') as f: statuses = json.load(f)
            except (IOError, json.JSONDecodeError): pass

        statuses[ip] = {"status": status, "timestamp": datetime.utcnow().isoformat() + "Z"}
        with open(CLIENT_STATUS_FILE, 'w') as f: json.dump(statuses, f, indent=2)

    def _check_client_statuses(self):
        if not os.path.exists(CLIENT_STATUS_FILE): return
        try:
            with open(CLIENT_STATUS_FILE, 'r') as f: client_statuses = json.load(f)
        except (IOError, json.JSONDecodeError): return

        now = datetime.utcnow()
        stale_minutes = int(self.config.get_main('CLIENT_STALE_TIMEOUT_MINUTES', 5))
        
        for params in self.config.get_wake_hosts().values():
            if 'SHUTDOWN_DELAY_MINUTES' not in params: continue
            ip, name = params.get('IP'), params.get('NAME', 'N/A')
            if not ip or not (status_data := client_statuses.get(ip)): continue

            shutdown_flag = f"SHUTDOWN_NOTIFIED_{ip.replace('.', '_')}"
            if status_data.get('status') == 'shutdown_pending' and not self.client_notification_states.get(shutdown_flag):
                self.notifier.send("CLIENT_SHUTDOWN", "[UPS] ALERT: Client Shutdown", f"Client '{name}' ({ip}) is shutting down.")
                self.client_notification_states[shutdown_flag] = True

            stale_flag = f"STALE_NOTIFIED_{ip.replace('.', '_')}"
            try:
                ts = datetime.fromisoformat(status_data.get('timestamp', '').replace('Z', '+00:00'))
                if (now - ts) > timedelta(minutes=stale_minutes):
                    if not self.client_notification_states.get(stale_flag):
                        self.notifier.send("CLIENT_STALE", "[UPS] WARNING: Client Stale", f"Client '{name}' ({ip}) is stale.")
                        self.client_notification_states[stale_flag] = True
                elif stale_flag in self.client_notification_states:
                    del self.client_notification_states[stale_flag]
            except (ValueError, TypeError): pass

    def run(self):
        log.info("--- Power check initiated ---")
        try:
            self._load_state()
            self._check_schedules()
            power_status = self._determine_power_status()

            if power_status == "OFFLINE": self._handle_power_offline()
            else: self._handle_power_online()
            
            self._check_client_statuses()
            with open(CLIENT_NOTIFICATION_STATE_FILE, 'w') as f:
                for k, v in self.client_notification_states.items():
                    f.write(f"{k}={str(v).lower()}\n")

        except Exception as e:
            log.error(f"Unhandled exception: {e}", exc_info=True)
            self.notifier.send("APP_ERROR", "[UPS] CRITICAL: Script Failed", f"The main power manager script failed. Error: {e}")
        finally:
            log.info("--- Power check finished ---")

if __name__ == "__main__":
    for f in [STATE_FILE, NOTIFICATION_STATE_FILE, CLIENT_NOTIFICATION_STATE_FILE, CLIENT_STATUS_FILE]:
        if not os.path.exists(f):
            open(f, 'a').close()
            if f.endswith('.json'):
                with open(f, 'w') as jf: jf.write('{}')

    PowerManager().run()