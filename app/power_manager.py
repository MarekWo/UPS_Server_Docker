#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import subprocess
import json
import logging
import logging.handlers
import fcntl
import time
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
LOCK_FILE = "/var/run/nut/power_manager.lock"

# Sub-minute polling: 4 iterations x 15 seconds = 60 seconds per cron cycle
CHECK_ITERATIONS = 4
CHECK_INTERVAL_SECONDS = 15

# Commands
PING_CMD = "/bin/ping"
WAKEONLAN_CMD = "/usr/bin/wakeonlan"

# --- Logger Setup ---
def setup_logging(debug_mode=False):
    """Configures logging to file and syslog with optional debug level."""
    logger = logging.getLogger(APP_NAME)
    logger.setLevel(logging.DEBUG if debug_mode else logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', '%Y-%m-%d %H:%M:%S')

    # File handler for detailed logs
    try:
        file_handler = logging.FileHandler(LOG_FILE)
        file_handler.setLevel(logging.DEBUG if debug_mode else logging.INFO)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except IOError as e:
        print(f"Warning: Cannot write to log file {LOG_FILE}: {e}", file=sys.stderr)

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

# Initial logger setup (will be reconfigured after reading config)
log = setup_logging()

# --- Core Classes ---

def read_power_manager_config():
    """
    Read and parse power_manager.conf file - EXACT REPLICA of web_gui.py function
    to ensure 100% compatibility with existing Web GUI.
    """
    config = {}
    wake_hosts = {}
    schedules = {}
    current_section = None
    
    if not os.path.exists(CONFIG_FILE):
        return config, wake_hosts, schedules
    
    try:
        with open(CONFIG_FILE, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                
                # Check for section headers
                if line.startswith('[') and line.endswith(']'):
                    current_section = line[1:-1]
                    if current_section.startswith('WAKE_HOST_'):
                        wake_hosts[current_section] = {}
                    elif current_section.startswith('SCHEDULE_'):
                        schedules[current_section] = {}
                    else:
                        # Reset if it's not a known section type, allowing for future sections
                        current_section = None 
                    continue
                
                # Parse key=value pairs
                if '=' in line:
                    try:
                        key, value = line.split('=', 1)
                        key = key.strip()
                        # Remove quotes from values if present, strip whitespace again
                        value = value.strip().strip('"\'').strip()

                        if current_section:
                            if current_section.startswith('WAKE_HOST_'):
                                wake_hosts[current_section][key] = value
                            elif current_section.startswith('SCHEDULE_'):
                                schedules[current_section][key] = value
                        else:
                            # This is a main config parameter
                            config[key] = value
                    except ValueError as e:
                        log.warning(f"Invalid config line: {line} - {e}")
    except IOError as e:
        log.error(f"Cannot read config file: {e}")
        raise
    
    return config, wake_hosts, schedules

def save_setting_to_config(key, value, section=None):
    """Safely saves a single setting back to the config file with file locking."""
    section = section or None  # Main config section
    
    try:
        # Use file locking to prevent race conditions
        with open(CONFIG_FILE, 'r+') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            
            lines = f.readlines()
            f.seek(0)
            f.truncate()
            
            in_correct_section = section is None  # True for main config
            key_found = False
            
            for line in lines:
                stripped = line.strip()
                
                # Check for section headers
                if stripped.startswith('[') and stripped.endswith(']'):
                    current_section = stripped[1:-1]
                    in_correct_section = current_section == section
                    f.write(line)
                    continue
                
                # Check for our key in the correct section
                if in_correct_section and '=' in stripped and not stripped.startswith('#'):
                    line_key = stripped.split('=')[0].strip()
                    if line_key == key:
                        # Preserve original formatting but update value
                        indent = len(line) - len(line.lstrip())
                        f.write(' ' * indent + f'{key}="{value}"\n')
                        key_found = True
                        continue
                
                f.write(line)
            
            # If key wasn't found, add it to the end of the correct section
            if not key_found:
                if section is not None:
                    f.write(f'\n[{section}]\n')
                f.write(f'{key}="{value}"\n')
                
    except IOError as e:
        log.error(f"Failed to save setting {key}={value}: {e}")
        raise

class Notifier:
    """Handles sending email notifications."""
    def __init__(self, config):
        self.config = config
        self.debounce_file = NOTIFICATION_STATE_FILE
        if not os.path.exists(self.debounce_file):
            open(self.debounce_file, 'a').close()

    def send(self, n_type, subject, body):
        """Sends a notification if enabled and not debounced."""
        enabled_var = f"NOTIFY_{n_type.upper()}"
        if self.config.get(enabled_var, 'false').lower() != 'true':
            log.info(f"Notification for {n_type} is disabled. Skipping.")
            return

        if n_type == "APP_ERROR":
            debounce_seconds = 3600
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
                        timestamp_str = line.strip().split('=')[1]
                        return datetime.fromtimestamp(int(timestamp_str))
        except (IOError, ValueError, IndexError):
            pass
        return None

    def _set_debounce_timestamp(self, n_type):
        try:
            # Use file locking for safe concurrent access
            with open(self.debounce_file, 'r+') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                lines = [l for l in f if not l.startswith(f"{n_type}_LAST_SENT=")]
                lines.append(f"{n_type}_LAST_SENT={int(datetime.now().timestamp())}\n")
                f.seek(0)
                f.truncate()
                f.writelines(lines)
        except IOError as e:
            log.error(f"Could not update debounce timestamp file: {e}")

    def _send_email(self, subject, body):
        smtp_server = self.config.get('SMTP_SERVER')
        smtp_port = int(self.config.get('SMTP_PORT', 587))
        smtp_user = self.config.get('SMTP_USER')
        smtp_password = self.config.get('SMTP_PASSWORD')
        sender_name = self.config.get('SMTP_SENDER_NAME', 'UPS Server')
        sender_email = self.config.get('SMTP_SENDER_EMAIL')
        recipients = [e.strip() for e in self.config.get('SMTP_RECIPIENTS', '').split(',') if e.strip()]
        smtp_use_tls = self.config.get('SMTP_USE_TLS', 'auto').lower()  # New option

        if not all([smtp_server, sender_email, recipients]):
            raise ValueError("SMTP server, sender email, and recipients must be configured.")

        msg = MIMEText(body, 'plain', 'utf-8')
        msg['Subject'] = subject
        msg['From'] = formataddr((sender_name, sender_email))
        msg['To'] = ', '.join(recipients)

        server = None
        try:
            if smtp_port == 465:
                # Port 465 always uses SSL/TLS
                server = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=10)
            else:
                # For other ports, determine STARTTLS usage based on configuration
                server = smtplib.SMTP(smtp_server, smtp_port, timeout=10)
                
                # Determine whether to use STARTTLS
                should_use_starttls = False
                if smtp_use_tls == 'true':
                    should_use_starttls = True
                elif smtp_use_tls == 'false':
                    should_use_starttls = False
                elif smtp_use_tls == 'auto':
                    # Auto mode: use legacy logic (don't use STARTTLS on port 26)
                    should_use_starttls = smtp_port != 26
                
                if should_use_starttls:
                    server.starttls()
            
            if smtp_user and smtp_password:
                server.login(smtp_user, smtp_password)
            
            server.sendmail(sender_email, recipients, msg.as_string())
            
        finally:
            if server:
                try:
                    server.quit()
                except:
                    pass

class PowerManager:
    """Main application logic with improved error handling and file locking."""
    def __init__(self):
        global log
        try:
            self.config, self.wake_hosts, self.schedules = read_power_manager_config()
        except (FileNotFoundError, IOError) as e:
            log.error(f"CRITICAL ERROR: {e}. Exiting.")
            sys.exit(1)

        # Reconfigure logger based on DEBUG_MODE setting
        debug_mode = self.config.get('DEBUG_MODE', 'false').lower() == 'true'
        if debug_mode:
            # Clear existing handlers and reconfigure with debug mode
            logger = logging.getLogger(APP_NAME)
            logger.handlers.clear()
            log = setup_logging(debug_mode=True)
            log.info("Debug mode enabled via configuration")

        self.notifier = Notifier(self.config)
        self.power_state = None
        self.power_state_timestamp = None
        self.power_state_was_simulation = False
        self.simulation_interrupted = False
        self.interrupted_schedule_info = None
        self.client_notification_states = {}

    def _load_state(self):
        """Safely load state from files with comprehensive error handling."""
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if '=' in line:
                            try:
                                key, value = line.split('=', 1)
                                if key == 'STATE':
                                    self.power_state = value
                                elif key == 'TIMESTAMP':
                                    self.power_state_timestamp = int(value)
                                elif key == 'SIMULATION':
                                    self.power_state_was_simulation = value.lower() == 'true'
                                elif key == 'SIM_INTERRUPTED':
                                    self.simulation_interrupted = value.lower() == 'true'
                                    if self.simulation_interrupted:
                                        log.debug(f"Loaded simulation_interrupted flag: {self.simulation_interrupted}")
                                elif key == 'INTERRUPTED_SCHEDULE':
                                    try:
                                        self.interrupted_schedule_info = json.loads(value) if value and value != 'null' else None
                                        if self.interrupted_schedule_info:
                                            log.debug(f"Loaded interrupted_schedule_info: {self.interrupted_schedule_info}")
                                    except json.JSONDecodeError as e:
                                        log.error(f"Failed to parse INTERRUPTED_SCHEDULE JSON: {value} - {e}")
                                        self.interrupted_schedule_info = None
                            except (ValueError, TypeError) as e:
                                log.warning(f"Invalid state file line: {line} - {e}")
            except IOError as e:
                log.error(f"Cannot read state file: {e}")
        
        if os.path.exists(CLIENT_NOTIFICATION_STATE_FILE):
            try:
                with open(CLIENT_NOTIFICATION_STATE_FILE, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if '=' in line:
                            try:
                                key, value = line.split('=', 1)
                                self.client_notification_states[key] = value.lower() == 'true'
                            except ValueError as e:
                                log.warning(f"Invalid client notification state line: {line} - {e}")
            except IOError as e:
                log.error(f"Cannot read client notification state file: {e}")

    def _save_power_state(self, state):
        """Safely save power state with file locking."""
        try:
            with open(STATE_FILE, 'w') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                f.write(f"STATE={state}\n")
                f.write(f"TIMESTAMP={int(datetime.now().timestamp())}\n")
                # Save simulation mode status for restoration logic
                is_simulation = self.config.get('POWER_SIMULATION_MODE', 'false').lower() == 'true'
                f.write(f"SIMULATION={str(is_simulation).lower()}\n")
                f.write(f"SIM_INTERRUPTED={str(self.simulation_interrupted).lower()}\n")
                schedule_json = json.dumps(self.interrupted_schedule_info) if self.interrupted_schedule_info else 'null'
                f.write(f"INTERRUPTED_SCHEDULE={schedule_json}\n")
        except IOError as e:
            log.error(f"Cannot save power state: {e}")

    def _clear_file(self, filepath):
        """Safely clear and recreate a file."""
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
            open(filepath, 'a').close()
        except IOError as e:
            log.error(f"Cannot clear file {filepath}: {e}")

    def _update_ups_status_file(self, status_line):
        """Update UPS status file with error handling."""
        ups_file = self.config.get('UPS_STATE_FILE', UPS_STATE_FILE_DEFAULT)
        try:
            with open(ups_file, 'w') as f:
                f.write(status_line + '\n')
        except IOError as e:
            log.error(f"Cannot update UPS status file: {e}")

    def _should_simulation_be_active_now(self):
        """Check if any schedule indicates simulation should be active at current time.
        NOTE: This function checks time windows regardless of ENABLED flag,
        because one-time schedules are auto-disabled after execution.
        """
        now = datetime.now()

        for section, params in self.schedules.items():
            # Note: We don't check ENABLED here because one-time schedules
            # are automatically disabled after execution, but their time window
            # is still active until the corresponding stop schedule.

            schedule_type = params.get('TYPE', '').lower()
            schedule_time = params.get('TIME', '')
            action = params.get('ACTION', '').lower()

            if action != 'start':  # Only check start schedules
                continue

            # For one-time schedules, check if today matches and time has passed
            if schedule_type == 'one-time':
                schedule_date = params.get('DATE', '')
                if schedule_date == now.strftime('%Y-%m-%d'):
                    if schedule_time <= now.strftime('%H:%M'):
                        # Check if there's a corresponding stop schedule
                        stop_time = self._find_corresponding_stop_schedule(section, schedule_date)
                        if stop_time and now.strftime('%H:%M') < stop_time:
                            return {'active': True, 'schedule': section, 'params': params, 'end_time': stop_time}

            # For recurring schedules
            elif schedule_type == 'recurring':
                dow = params.get('DAY_OF_WEEK', '').lower()
                if dow == 'everyday' or dow == now.strftime('%A').lower():
                    if schedule_time <= now.strftime('%H:%M'):
                        # For recurring, assume it runs until end of day unless stopped
                        stop_time = self._find_corresponding_stop_schedule(section)
                        end_time = stop_time if stop_time and stop_time > now.strftime('%H:%M') else '23:59'
                        if now.strftime('%H:%M') < end_time:
                            return {'active': True, 'schedule': section, 'params': params, 'end_time': end_time}

        return {'active': False}

    def _find_corresponding_stop_schedule(self, start_section, date=None):
        """Find corresponding stop schedule for a start schedule.
        NOTE: Does not check ENABLED flag for same reason as _should_simulation_be_active_now.
        """
        for section, params in self.schedules.items():
            if params.get('ACTION', '').lower() == 'stop':

                if date:  # One-time schedule
                    if params.get('DATE') == date:
                        return params.get('TIME')
                else:  # Recurring schedule
                    # Simple heuristic: find stop on same day type
                    start_params = self.schedules.get(start_section, {})
                    if (params.get('TYPE') == start_params.get('TYPE') and
                        params.get('DAY_OF_WEEK') == start_params.get('DAY_OF_WEEK')):
                        return params.get('TIME')
        return None

    def _check_schedules(self):
        """Check and execute scheduled actions."""
        now = datetime.now()
        for section, params in self.schedules.items():
            if params.get('ENABLED', 'false').lower() != 'true': 
                continue

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
                
                try:
                    if action == 'start':
                        save_setting_to_config('POWER_SIMULATION_MODE', 'true')
                        self.notifier.send("SIMULATION_MODE", "[UPS] INFO: Power Outage Simulation Started", "Scheduled start of power outage simulation.")
                    elif action == 'stop':
                        save_setting_to_config('POWER_SIMULATION_MODE', 'false')
                        self.notifier.send("SIMULATION_MODE", "[UPS] INFO: Power Outage Simulation Stopped", "Scheduled stop of power outage simulation.")
                    
                    if params.get('TYPE') == 'one-time':
                        save_setting_to_config('ENABLED', 'false', section=section)
                    
                    # Reload config after changes
                    self.config, self.wake_hosts, self.schedules = read_power_manager_config()
                    
                except Exception as e:
                    log.error(f"Failed to execute scheduled action: {e}")
                
                break

    def _determine_power_status(self):
        """Determine current power status with improved error handling and simulation interruption detection."""
        is_simulation_mode = self.config.get('POWER_SIMULATION_MODE', 'false').lower() == 'true'

        # Always check sentinel hosts to detect real power failures
        sentinel_hosts = self.config.get('SENTINEL_HOSTS', '').split()
        if not sentinel_hosts:
            log.warning("No sentinel hosts configured, assuming power is ONLINE")
            return "ONLINE"

        log.info(f"Pinging sentinel hosts: {' '.join(sentinel_hosts)}")
        online_hosts_count = 0

        # Check ALL sentinel hosts (matching original Bash behavior)
        for ip in sentinel_hosts:
            try:
                result = subprocess.run([PING_CMD, "-c", "1", "-W", "1", ip],
                                      capture_output=True, timeout=3)
                if result.returncode == 0:
                    log.info(f"  -> Sentinel host {ip} is online.")
                    online_hosts_count += 1
                else:
                    log.info(f"  -> Sentinel host {ip} is offline.")
            except (subprocess.TimeoutExpired, OSError) as e:
                log.warning(f"  -> Failed to ping sentinel host {ip}: {e}")

        log.info(f"Found {online_hosts_count} online sentinel hosts.")

        real_power_offline = online_hosts_count == 0

        # Handle simulation mode interruption by real power failure
        if is_simulation_mode and real_power_offline:
            log.critical("REAL POWER FAILURE detected during simulation! Interrupting simulation mode.")

            # Save information about interrupted simulation
            sim_info = self._should_simulation_be_active_now()
            log.debug(f"Simulation schedule check result: {sim_info}")
            if sim_info['active']:
                self.simulation_interrupted = True
                self.interrupted_schedule_info = {
                    'schedule': sim_info['schedule'],
                    'end_time': sim_info['end_time'],
                    'interrupted_at': datetime.now().strftime('%Y-%m-%d %H:%M')
                }
                log.debug(f"Set simulation_interrupted=True, interrupted_schedule_info={self.interrupted_schedule_info}")
            else:
                log.warning("Simulation schedule check returned 'not active' - interruption flags NOT set!")

            # Turn off simulation mode immediately
            try:
                save_setting_to_config('POWER_SIMULATION_MODE', 'false')
                self.config['POWER_SIMULATION_MODE'] = 'false'  # Update local config
                log.info("Simulation mode disabled due to real power failure.")
            except Exception as e:
                log.error(f"Failed to disable simulation mode: {e}")

            # Note: State will be saved in _handle_power_offline() with interruption flags preserved

        # Return status based on real power conditions or simulation
        if is_simulation_mode and not real_power_offline:
            log.warning("Power Outage Simulation is active. Forcing OFFLINE.")
            return "OFFLINE"
        elif real_power_offline:
            log.warning("All sentinel hosts are offline. Power is OFF.")
            return "OFFLINE"
        else:
            log.info("At least one sentinel host is online. Power is ON.")
            return "ONLINE"

    def _handle_power_offline(self):
        """Handle power offline state."""
        state_changed = self.power_state != "POWER_FAIL"

        log.debug(f"_handle_power_offline: state_changed={state_changed}, simulation_interrupted={self.simulation_interrupted}, power_state={self.power_state}")

        if state_changed:
            log.warning("STATE CHANGE: Power failure detected!")
            self._clear_file(CLIENT_NOTIFICATION_STATE_FILE)
            self.client_notification_states = {}

            # Check if this is a real power failure or simulation
            is_simulation = self.config.get('POWER_SIMULATION_MODE', 'false').lower() == 'true'

            if is_simulation:
                # In simulation mode, send simulation notification instead of power fail
                self.notifier.send("SIMULATION_MODE", "[UPS] INFO: Power Outage Simulation Active",
                                 "Power outage simulation is active. UPS status set to 'On Battery, Low Battery' for testing.")
            else:
                # Real power failure - send regular power fail notification
                self.notifier.send("POWER_FAIL", "[UPS] ALERT: Power Outage Detected",
                                 "All sentinel hosts are offline. System is on UPS power.")

        # Always save state to persist interruption flags (even if state hasn't changed)
        should_save = state_changed or self.simulation_interrupted
        log.debug(f"Should save state: {should_save} (state_changed={state_changed}, simulation_interrupted={self.simulation_interrupted})")

        if should_save:
            self._save_power_state("POWER_FAIL")
            if self.simulation_interrupted and not state_changed:
                log.debug("Saving state to persist simulation interruption flags.")
            if self.simulation_interrupted:
                log.debug(f"State saved with interruption flags: interrupted={self.simulation_interrupted}, schedule_info={self.interrupted_schedule_info}")

        self._update_ups_status_file("ups.status: OB LB")

    def _handle_power_online(self):
        """Handle power online state."""
        self._update_ups_status_file("ups.status: OL")
        if not self.power_state: 
            return

        now_ts = int(datetime.now().timestamp())
        wol_delay = int(self.config.get('WOL_DELAY_MINUTES', 5))
        
        if self.power_state == "POWER_FAIL":
            duration = (now_ts - self.power_state_timestamp) // 60 if self.power_state_timestamp else 0
            log.info("STATE CHANGE: Power restoration detected.")

            # Handle simulation interruption restoration
            log.debug(f"Checking interruption status: simulation_interrupted={self.simulation_interrupted}, power_state_was_simulation={self.power_state_was_simulation}")

            if self.simulation_interrupted:
                log.debug(f"Handling restoration after interrupted simulation. Interrupted flag: {self.simulation_interrupted}, Schedule info: {self.interrupted_schedule_info}")

                # Check if we should restore simulation mode
                if self.interrupted_schedule_info:
                    current_time = datetime.now().strftime('%H:%M')
                    end_time = self.interrupted_schedule_info.get('end_time', '23:59')

                    if current_time < end_time:
                        log.info(f"Restoring simulation mode until {end_time}")
                        try:
                            save_setting_to_config('POWER_SIMULATION_MODE', 'true')
                            self.config['POWER_SIMULATION_MODE'] = 'true'  # Update local config
                            self.notifier.send("SIMULATION_MODE", "[UPS] INFO: Simulation Restored After Power Failure",
                                             f"Power restored during scheduled simulation window. Resuming simulation until {end_time}.")

                            # Initiate WoL immediately after restoring simulation (for IGNORE_SIMULATION hosts)
                            log.info(f"Waiting {wol_delay} mins before WoL after simulation restoration.")
                        except Exception as e:
                            log.error(f"Failed to restore simulation mode: {e}")
                    else:
                        log.info("Simulation window has ended, not restoring simulation mode.")
                        self.notifier.send("POWER_RESTORED", "[UPS] INFO: Power Restored (Simulation Window Ended)",
                                         f"Power restored after ~{duration} mins. Scheduled simulation window has ended.")

                # Clear interruption flags - but keep them if we restored simulation
                # (they will be cleared after WoL completes)
                if self.config.get('POWER_SIMULATION_MODE', 'false').lower() != 'true':
                    self.simulation_interrupted = False
                    self.interrupted_schedule_info = None

            elif self.power_state_was_simulation:
                # Previous state was regular simulation - send simulation stop notification
                self.notifier.send("SIMULATION_MODE", "[UPS] INFO: Power Outage Simulation Stopped",
                                 f"Power outage simulation ended after ~{duration} mins.")
            else:
                # Previous state was real power failure - send power restored notification
                self.notifier.send("POWER_RESTORED", "[UPS] INFO: Power Restored",
                                 f"Power restored after ~{duration} mins. Waiting {wol_delay} mins for WoL.")

            # Save state - use special state if we restored simulation mode
            if self.config.get('POWER_SIMULATION_MODE', 'false').lower() == 'true' and self.simulation_interrupted:
                self._save_power_state("POWER_RESTORED_SIM")
                log.debug("Saved state as POWER_RESTORED_SIM (simulation restored after interruption)")
            else:
                self._save_power_state("POWER_RESTORED")

        elif self.power_state == "POWER_RESTORED":
            if self.power_state_timestamp and (now_ts - self.power_state_timestamp) >= (wol_delay * 60):
                log.info("WoL delay passed. Initiating wake-up sequence.")
                self._initiate_wol()

                # Clear all state files and reset interruption tracking
                self._clear_file(STATE_FILE)
                self._clear_file(CLIENT_NOTIFICATION_STATE_FILE)
                self.simulation_interrupted = False
                self.interrupted_schedule_info = None

        elif self.power_state == "POWER_RESTORED_SIM":
            # Special state: power was restored and simulation was re-activated
            # We need to wait for WoL delay even though we're currently in simulation mode
            if self.power_state_timestamp and (now_ts - self.power_state_timestamp) >= (wol_delay * 60):
                log.info("WoL delay passed after simulation restoration. Initiating wake-up sequence.")
                self._initiate_wol()

                # Clear interruption flags now that WoL is done
                self.simulation_interrupted = False
                self.interrupted_schedule_info = None
                self._clear_file(STATE_FILE)
                self._clear_file(CLIENT_NOTIFICATION_STATE_FILE)

    def _initiate_wol(self):
        """Initiate Wake-on-LAN sequence with comprehensive error handling and status tracking."""
        default_broadcast = self.config.get('DEFAULT_BROADCAST_IP')
        woken_hosts = []

        # Check if we're currently in simulation mode
        is_simulation_active = self.config.get('POWER_SIMULATION_MODE', 'false').lower() == 'true'
        if is_simulation_active:
            log.info("Simulation mode is active - will only wake hosts with IGNORE_SIMULATION=true")

        for section, params in self.wake_hosts.items():
            if params.get('AUTO_WOL', 'true').lower() == 'false':
                continue

            # If simulation is active, only wake hosts that ignore simulation
            if is_simulation_active:
                ignore_simulation = params.get('IGNORE_SIMULATION', 'false').lower() == 'true'
                if not ignore_simulation:
                    log.info(f"Skipping WoL for {params.get('NAME', 'unknown')} ({params.get('IP')}) - simulation mode active and host does not ignore simulation")
                    continue

            ip, mac = params.get('IP'), params.get('MAC')
            if not ip or not mac:
                log.warning(f"Skipping WoL for {params.get('NAME', 'unknown')} - missing IP or MAC")
                continue

            try:
                # Check if host is already online
                ping_result = subprocess.run([PING_CMD, "-c", "1", "-W", "1", ip], 
                                           capture_output=True, timeout=3)
                
                if ping_result.returncode != 0:
                    broadcast = params.get('BROADCAST_IP', default_broadcast)
                    log.info(f"Sending WoL to {params.get('NAME')} ({ip}) via {broadcast}.")
                    
                    # Send WoL packet and check result (improved from original)
                    wol_result = subprocess.run([WAKEONLAN_CMD, "-i", broadcast, mac], 
                                              capture_output=True, timeout=5)
                    
                    if wol_result.returncode == 0:
                        self._update_client_status_json(ip, "wol_sent")
                        woken_hosts.append(f"- {params.get('NAME')} ({ip})")
                        log.info(f"WoL packet sent successfully to {params.get('NAME')} ({ip})")
                    else:
                        log.error(f"Failed to send WoL packet to {params.get('NAME')} ({ip}): {wol_result.stderr.decode()}")
                        self._update_client_status_json(ip, "wol_failed")
                else:
                    log.info(f"Host {params.get('NAME')} ({ip}) is already online.")
                    
            except (subprocess.TimeoutExpired, OSError) as e:
                log.error(f"Error during WoL process for {params.get('NAME')} ({ip}): {e}")
                self._update_client_status_json(ip, "wol_error")

        if woken_hosts:
            self.notifier.send("POWER_RESTORED", "[UPS] INFO: WoL Sequence Initiated",
                               "Sent WoL signals to:\n\n" + "\n".join(woken_hosts))

    def _update_client_status_json(self, ip, status):
        """Update client status JSON with atomic writes and compatible format."""
        statuses = {}
        
        try:
            if os.path.exists(CLIENT_STATUS_FILE):
                with open(CLIENT_STATUS_FILE, 'r') as f: 
                    statuses = json.load(f)
        except (IOError, json.JSONDecodeError) as e:
            log.warning(f"Failed to read client status file: {e}")

        try:
            # Use timestamp format compatible with Web GUI expectations
            timestamp = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
            statuses[ip] = {
                "status": status, 
                "timestamp": timestamp,
                "remaining_seconds": None,
                "shutdown_delay": None
            }
            
            # Use atomic write operation
            temp_file = CLIENT_STATUS_FILE + ".tmp"
            with open(temp_file, 'w') as f: 
                json.dump(statuses, f, indent=2)
            os.rename(temp_file, CLIENT_STATUS_FILE)
                
        except (IOError, json.JSONDecodeError) as e:
            log.error(f"Failed to update client status file: {e}")

    def _check_client_statuses(self):
        """Check client statuses and send notifications with improved error handling."""
        if not os.path.exists(CLIENT_STATUS_FILE): 
            return
            
        try:
            with open(CLIENT_STATUS_FILE, 'r') as f: 
                client_statuses = json.load(f)
        except (IOError, json.JSONDecodeError) as e:
            log.error(f"Failed to parse client status file: {e}")
            return

        now = datetime.utcnow()
        stale_minutes = int(self.config.get('CLIENT_STALE_TIMEOUT_MINUTES', 5))
        
        for section, params in self.wake_hosts.items():
            if 'SHUTDOWN_DELAY_MINUTES' not in params: 
                continue
                
            ip, name = params.get('IP'), params.get('NAME', 'N/A')
            if not ip: 
                continue
                
            status_data = client_statuses.get(ip)
            if not status_data: 
                continue

            # Check for shutdown notification
            shutdown_flag = f"SHUTDOWN_NOTIFIED_{ip.replace('.', '_')}"
            if (status_data.get('status') == 'shutdown_pending' and 
                not self.client_notification_states.get(shutdown_flag)):
                
                self.notifier.send("CLIENT_SHUTDOWN", "[UPS] ALERT: Client Shutdown", 
                                 f"Client '{name}' ({ip}) is shutting down.")
                self.client_notification_states[shutdown_flag] = True

            # Check for stale status with robust timestamp parsing
            stale_flag = f"STALE_NOTIFIED_{ip.replace('.', '_')}"
            try:
                timestamp_str = status_data.get('timestamp', '')
                if timestamp_str:
                    # Handle both ISO format and RFC3339 format
                    if timestamp_str.endswith('Z'):
                        ts = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                    else:
                        ts = datetime.fromisoformat(timestamp_str)
                    
                    # Convert to UTC if necessary
                    if ts.tzinfo is not None:
                        ts = ts.replace(tzinfo=None)
                    
                    time_diff = (now - ts).total_seconds()
                    
                    if time_diff > (stale_minutes * 60):
                        if not self.client_notification_states.get(stale_flag):
                            self.notifier.send("CLIENT_STALE", "[UPS] WARNING: Client Stale", 
                                             f"Client '{name}' ({ip}) has not reported for {stale_minutes}+ minutes.")
                            self.client_notification_states[stale_flag] = True
                    elif stale_flag in self.client_notification_states:
                        # Status is fresh again, clear the stale flag
                        log.info(f"Client '{name}' ({ip}) has recovered from stale status.")
                        del self.client_notification_states[stale_flag]
                        
            except (ValueError, TypeError) as e:
                log.warning(f"Invalid timestamp format for client {ip}: {timestamp_str} - {e}")

    def run(self, iteration=0):
        """Main execution method with comprehensive error handling.

        Args:
            iteration: Current iteration number (0-3). Schedule checking
                      only runs on iteration 0 to avoid duplicate triggers.
        """
        log.info(f"--- Power check initiated (iteration {iteration + 1}/{CHECK_ITERATIONS}) ---")
        try:
            self._load_state()

            # Log current state for debugging
            if self.simulation_interrupted:
                log.debug(f"Simulation interruption active: {self.interrupted_schedule_info}")

            # Only check schedules on the first iteration to avoid duplicate triggers
            if iteration == 0:
                self._check_schedules()
            power_status = self._determine_power_status()

            # Special handling for POWER_RESTORED_SIM state:
            # Even if power_status is OFFLINE (due to simulation), we need to handle WoL
            if self.power_state == "POWER_RESTORED_SIM":
                log.debug("Current state is POWER_RESTORED_SIM - handling WoL logic despite power_status")
                self._handle_power_online()  # This handles the POWER_RESTORED_SIM state
            elif power_status == "OFFLINE":
                self._handle_power_offline()
            else:
                self._handle_power_online()

            self._check_client_statuses()
            
            # Save client notification states with file locking
            try:
                with open(CLIENT_NOTIFICATION_STATE_FILE, 'w') as f:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    for k, v in self.client_notification_states.items():
                        f.write(f"{k}={str(v).lower()}\n")
            except IOError as e:
                log.error(f"Cannot save client notification states: {e}")

        except Exception as e:
            log.error(f"Unhandled exception: {e}", exc_info=True)
            try:
                self.notifier.send("APP_ERROR", "[UPS] CRITICAL: Script Failed", 
                                 f"The main power manager script failed. Error: {e}")
            except:
                pass  # Don't fail on notification failure
        finally:
            log.info("--- Power check finished ---")

if __name__ == "__main__":
    # Ensure required files exist with proper error handling
    for f in [STATE_FILE, NOTIFICATION_STATE_FILE, CLIENT_NOTIFICATION_STATE_FILE, CLIENT_STATUS_FILE]:
        try:
            if not os.path.exists(f):
                open(f, 'a').close()
                if f.endswith('.json'):
                    with open(f, 'w') as jf:
                        jf.write('{}')
        except IOError as e:
            print(f"Warning: Cannot create {f}: {e}", file=sys.stderr)

    # Acquire lock file to prevent concurrent execution.
    # Cron fires every minute, but we run for ~60 seconds (4 x 15s iterations).
    lock_fd = None
    try:
        lock_fd = open(LOCK_FILE, 'w')
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        # Another instance is already running - exit silently
        if lock_fd:
            lock_fd.close()
        sys.exit(0)

    try:
        for iteration in range(CHECK_ITERATIONS):
            # Create a fresh PowerManager for each iteration to pick up
            # any config changes made via the Web GUI between checks
            PowerManager().run(iteration=iteration)

            # Sleep between iterations (but not after the last one)
            if iteration < CHECK_ITERATIONS - 1:
                time.sleep(CHECK_INTERVAL_SECONDS)
    finally:
        # Release lock file
        if lock_fd:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
            lock_fd.close()
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass