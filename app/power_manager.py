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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from battery_monitor import BatteryMonitor
from power_source import (
    PowerSourceEvaluator,
    SOURCE_BATTERY,
    SOURCE_SENTINEL,
    STATUS_LOW_BATTERY,
)

# --- Constants ---
APP_NAME = "PowerManager"
LOG_FILE = "/var/log/power_manager.log"
CONFIG_FILE = "/etc/nut/power_manager.conf"
STATE_FILE = "/var/run/nut/power_manager.state"
NOTIFICATION_STATE_FILE = "/var/run/nut/notification.state"
CLIENT_STATUS_FILE = "/var/run/nut/client_status.json"
CLIENT_NOTIFICATION_STATE_FILE = "/var/run/nut/client_notification.state"
POWER_STATE_FILE = "/var/run/nut/power_state.json"
BATTERY_WOL_STATE_FILE = "/var/run/nut/battery_wol.json"
BATTERY_GAP_STATE_FILE = "/var/run/nut/battery_gap.json"
BATTERY_OUTAGE_STATE_FILE = "/var/run/nut/battery_outage.json"
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
        self.battery_monitor = BatteryMonitor(self.config)
        self.evaluator = PowerSourceEvaluator(self.config)
        self.battery_status = None
        # How many consecutive polls have come back empty. A single miss is
        # usually the monitor restarting, not the battery vanishing.
        self.battery_gap_cycles = 0
        self.sentinel_online_count = 0
        self.sentinel_total_count = 0
        self.power_state = None
        self.power_state_timestamp = None
        self.power_state_was_simulation = False
        self.simulation_interrupted = False
        self.interrupted_schedule_info = None
        # Sentinel reading with the simulation stripped out: whether mains is
        # actually gone right now, as opposed to power_status, which a
        # simulation window can hold at OFFLINE for hours.
        self.real_power_offline = False
        # Latch: a real outage has been mailed out and no restoration has
        # been mailed since. Without it an outage starting inside a
        # simulation window is silent, because the state machine is already
        # sitting in POWER_FAIL and nothing looks like a transition.
        self.real_fail_notified = False
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
                                elif key == 'REAL_FAIL_NOTIFIED':
                                    self.real_fail_notified = value.lower() == 'true'
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

    def _save_power_state(self, state, reset_timestamp=False):
        """Safely save power state with file locking.

        Args:
            state: the state name to persist.
            reset_timestamp: start the episode clock again even though the state
                name has not changed. Used when a genuine outage begins
                underneath a simulation that already parked us in POWER_FAIL.

        Re-saving an unchanged state keeps its original timestamp. An
        interrupted simulation writes POWER_FAIL on every 15s pass, and moving
        the timestamp each time made the outage duration reported on
        restoration measure from the last write rather than from the start.
        """
        if reset_timestamp or state != self.power_state or not self.power_state_timestamp:
            self.power_state_timestamp = int(datetime.now().timestamp())
        self.power_state = state
        try:
            with open(STATE_FILE, 'w') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                f.write(f"STATE={state}\n")
                f.write(f"TIMESTAMP={self.power_state_timestamp}\n")
                # Save simulation mode status for restoration logic
                is_simulation = self.config.get('POWER_SIMULATION_MODE', 'false').lower() == 'true'
                f.write(f"SIMULATION={str(is_simulation).lower()}\n")
                f.write(f"SIM_INTERRUPTED={str(self.simulation_interrupted).lower()}\n")
                f.write(f"REAL_FAIL_NOTIFIED={str(self.real_fail_notified).lower()}\n")
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
                now_hm = now.strftime('%H:%M')
                stop_time = self._find_corresponding_stop_schedule(section)
                # Without a stop, the window is assumed to run to end of day.
                end_time = stop_time or '23:59'

                if schedule_time < end_time:
                    # Ordinary window, both ends on the same clock day.
                    in_window = schedule_time <= now_hm < end_time
                    window_day = now
                else:
                    # The window crosses midnight (21:00 -> 06:00). Comparing
                    # HH:MM strings straight through would make it inactive on
                    # both halves: '00:13' is neither >= '21:00' nor caught by
                    # a '23:59' fallback end. It has to be treated as the union
                    # of [start, 24:00) and [00:00, end), and the day-of-week
                    # test has to look at the day the window *opened*, which
                    # for the after-midnight half is yesterday.
                    in_window = now_hm >= schedule_time or now_hm < end_time
                    window_day = now if now_hm >= schedule_time else now - timedelta(days=1)

                day_matches = dow == 'everyday' or dow == window_day.strftime('%A').lower()
                if day_matches and in_window:
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
        self.sentinel_total_count = len(sentinel_hosts)
        if not sentinel_hosts:
            log.warning("No sentinel hosts configured, assuming power is ONLINE")
            self.sentinel_online_count = 0
            self.real_power_offline = False
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
        self.sentinel_online_count = online_hosts_count

        real_power_offline = online_hosts_count == 0
        # Published because the return value below cannot carry it: a simulation
        # window pins that to OFFLINE, which makes it useless for telling a
        # genuine outage apart from a scheduled one.
        self.real_power_offline = real_power_offline

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

        # The outage alert deliberately hangs off the sentinel reading rather
        # than off state_changed. A simulation window parks the state machine in
        # POWER_FAIL for the whole night, so a genuine outage starting
        # underneath it is not a transition and used to go out unannounced -
        # 2026-08-17 22:58 was silent for nine minutes, and only got a mail at
        # all because the grid happened to blink. The latch keeps it to one
        # alert per outage; it is cleared when a restoration is announced.
        alert_sent = False
        if self.real_power_offline and not self.real_fail_notified:
            self.notifier.send("POWER_FAIL", "[UPS] ALERT: Power Outage Detected",
                             "All sentinel hosts are offline. System is on UPS power."
                             + self._battery_context() + self._shutdown_plan())
            self.real_fail_notified = True
            alert_sent = True
        elif state_changed and not self.real_power_offline:
            # Nothing is actually wrong - the simulation is forcing OFFLINE.
            self.notifier.send("SIMULATION_MODE", "[UPS] INFO: Power Outage Simulation Active",
                             "Power outage simulation is active. UPS status set to 'On Battery, Low Battery' for testing.")

        # Always save state to persist interruption flags (even if state hasn't changed)
        should_save = state_changed or self.simulation_interrupted or alert_sent
        log.debug(f"Should save state: {should_save} (state_changed={state_changed}, simulation_interrupted={self.simulation_interrupted}, alert_sent={alert_sent})")

        if should_save:
            self._save_power_state("POWER_FAIL", reset_timestamp=alert_sent)
            if self.simulation_interrupted and not state_changed:
                log.debug("Saving state to persist simulation interruption flags.")
            if self.simulation_interrupted:
                log.debug(f"State saved with interruption flags: interrupted={self.simulation_interrupted}, schedule_info={self.interrupted_schedule_info}")

    def _handle_power_online(self):
        """Handle power online state."""
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

                # Check if we should restore simulation mode. Asking the
                # schedule again rather than comparing HH:MM strings against a
                # stored end_time: that comparison could not survive a window
                # crossing midnight, which is why the 21:00-06:00 simulation was
                # not restored after the 2026-08-18 00:13 restoration and PVE2
                # then ran all night.
                if self.interrupted_schedule_info:
                    sim_now = self._should_simulation_be_active_now()
                    still_active = (
                        sim_now.get('active')
                        and sim_now.get('schedule') == self.interrupted_schedule_info.get('schedule')
                    )
                    end_time = sim_now.get('end_time') or self.interrupted_schedule_info.get('end_time', '23:59')

                    if still_active:
                        log.info(f"Restoring simulation mode until {end_time}")
                        try:
                            save_setting_to_config('POWER_SIMULATION_MODE', 'true')
                            self.config['POWER_SIMULATION_MODE'] = 'true'  # Update local config
                            self.notifier.send("SIMULATION_MODE", "[UPS] INFO: Simulation Restored After Power Failure",
                                             f"Power restored during scheduled simulation window. Resuming simulation until {end_time}.")

                            # Anyone who was told about the outage has to be told
                            # it ended, even though resuming the simulation hides
                            # the transition behind a notification class that is
                            # usually switched off.
                            if self.real_fail_notified:
                                self.notifier.send("POWER_RESTORED", "[UPS] INFO: Power Restored",
                                                 f"Power restored after ~{duration} mins. "
                                                 f"Scheduled simulation resumes until {end_time}."
                                                 + self._battery_context())
                                self.real_fail_notified = False

                            # Initiate WoL immediately after restoring simulation (for IGNORE_SIMULATION hosts)
                            log.info(f"Waiting {wol_delay} mins before WoL after simulation restoration.")
                        except Exception as e:
                            log.error(f"Failed to restore simulation mode: {e}")
                    else:
                        log.info("Simulation window has ended, not restoring simulation mode.")
                        self.notifier.send("POWER_RESTORED", "[UPS] INFO: Power Restored (Simulation Window Ended)",
                                         f"Power restored after ~{duration} mins. Scheduled simulation window has ended."
                                         + self._battery_context())
                        self.real_fail_notified = False

                # Clear interruption flags - but keep them if we restored simulation
                # (they will be cleared after WoL completes)
                if self.config.get('POWER_SIMULATION_MODE', 'false').lower() != 'true':
                    self.simulation_interrupted = False
                    self.interrupted_schedule_info = None

            elif self.power_state_was_simulation and not self.real_fail_notified:
                # Previous state was regular simulation - send simulation stop notification
                self.notifier.send("SIMULATION_MODE", "[UPS] INFO: Power Outage Simulation Stopped",
                                 f"Power outage simulation ended after ~{duration} mins.")
            else:
                # Previous state was real power failure - send power restored notification
                self.notifier.send("POWER_RESTORED", "[UPS] INFO: Power Restored",
                                 f"Power restored after ~{duration} mins. Waiting {wol_delay} mins for WoL."
                                 + self._battery_context())
                self.real_fail_notified = False

            # Save state - use special state if we restored simulation mode
            if self.config.get('POWER_SIMULATION_MODE', 'false').lower() == 'true' and self.simulation_interrupted:
                self._save_power_state("POWER_RESTORED_SIM")
                log.debug("Saved state as POWER_RESTORED_SIM (simulation restored after interruption)")
            else:
                self._save_power_state("POWER_RESTORED")

        elif self.power_state == "POWER_RESTORED":
            if self.power_state_timestamp and (now_ts - self.power_state_timestamp) >= (wol_delay * 60):
                log.info("WoL delay passed. Initiating wake-up sequence.")
                self._complete_wol_cycle(now_ts, wol_delay)

        elif self.power_state == "POWER_RESTORED_SIM":
            # Special state: power was restored and simulation was re-activated
            # We need to wait for WoL delay even though we're currently in simulation mode
            if self.power_state_timestamp and (now_ts - self.power_state_timestamp) >= (wol_delay * 60):
                log.info("WoL delay passed after simulation restoration. Initiating wake-up sequence.")
                self._complete_wol_cycle(now_ts, wol_delay)

    def _initiate_wol(self, force=False):
        """Initiate Wake-on-LAN sequence with error handling and status tracking.

        Args:
            force: Skip the battery charge gate. Used once WOL_MAX_WAIT_MINUTES
                   has elapsed, so a stuck battery monitor cannot keep hosts
                   asleep indefinitely.

        Returns:
            List of host names whose wake-up was deferred waiting for charge.
        """
        woken_hosts = []
        deferred_hosts = []

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

            # Waking servers onto a battery that is still nearly empty just
            # means shutting them down again minutes later.
            if not force:
                allowed, wol_reason = self.evaluator.should_wol(params, self.battery_status)
                if not allowed:
                    log.info(
                        f"Deferring WoL for {params.get('NAME')} ({ip}): {wol_reason}"
                    )
                    self._update_client_status_json(ip, "wol_deferred")
                    deferred_hosts.append(params.get('NAME', ip))
                    continue

            if self._wake_host(params) == 'sent':
                woken_hosts.append(f"- {params.get('NAME')} ({ip})")

        if woken_hosts:
            body = "Sent WoL signals to:\n\n" + "\n".join(woken_hosts)
            if deferred_hosts:
                body += ("\n\nStill waiting for the battery to charge:\n\n"
                         + "\n".join(f"- {h}" for h in deferred_hosts))
            body += self._battery_context()
            self.notifier.send("POWER_RESTORED", "[UPS] INFO: WoL Sequence Initiated", body)

        return deferred_hosts

    def _wake_host(self, params):
        """Send a WoL packet to one host, unless it is already up.

        Shared by both wake-up paths: the sentinel-driven cycle in
        _initiate_wol() and the per-host battery cycle in _handle_battery_wol().

        Returns:
            'online' if the host answered a ping and no packet was needed,
            'sent', 'failed' or 'error' otherwise.
        """
        ip, mac = params.get('IP'), params.get('MAC')
        name = params.get('NAME', ip)
        broadcast = params.get('BROADCAST_IP', self.config.get('DEFAULT_BROADCAST_IP'))

        try:
            ping_result = subprocess.run([PING_CMD, "-c", "1", "-W", "1", ip],
                                         capture_output=True, timeout=3)
            if ping_result.returncode == 0:
                log.info(f"Host {name} ({ip}) is already online.")
                return 'online'

            log.info(f"Sending WoL to {name} ({ip}) via {broadcast}.")
            wol_result = subprocess.run([WAKEONLAN_CMD, "-i", broadcast, mac],
                                        capture_output=True, timeout=5)

            if wol_result.returncode == 0:
                self._update_client_status_json(ip, "wol_sent")
                log.info(f"WoL packet sent successfully to {name} ({ip})")
                return 'sent'

            log.error(f"Failed to send WoL packet to {name} ({ip}): {wol_result.stderr.decode()}")
            self._update_client_status_json(ip, "wol_failed")
            return 'failed'

        except (subprocess.TimeoutExpired, OSError) as e:
            log.error(f"Error during WoL process for {name} ({ip}): {e}")
            self._update_client_status_json(ip, "wol_error")
            return 'error'

    def _battery_context(self):
        """Battery summary to append to notification bodies, or '' if unavailable."""
        if not self.battery_monitor.enabled:
            return ""
        if not self.battery_status:
            return ("\n\nBattery monitor: unavailable ("
                    f"{self.battery_monitor.last_error or 'unknown reason'}) - "
                    "decisions fell back to sentinel hosts.")
        return f"\n\nBattery: {self.battery_status.describe()}"

    def _shutdown_plan(self):
        """Per-host shutdown thresholds, for the power failure notification.

        During an outage the useful question is not "is the power out" but
        "which machine goes down next, and at what point" - so spell it out.
        """
        if not self.battery_monitor.enabled or not self.battery_status:
            return ""

        lines = []
        for section, params in self.wake_hosts.items():
            if 'SHUTDOWN_DELAY_MINUTES' not in params:
                continue
            name = params.get('NAME', section)
            source = self.evaluator.host_source(params)
            if source == 'sentinel':
                lines.append(
                    f"- {name}: after {params.get('SHUTDOWN_DELAY_MINUTES')} min (timer)"
                )
            else:
                soc = self.evaluator.threshold(params, 'SHUTDOWN_SOC')
                voltage = self.evaluator.threshold(params, 'SHUTDOWN_VOLTAGE')
                lines.append(
                    f"- {name}: at SoC {soc:.0f}% or {voltage:.2f}V ({source})"
                )

        if not lines:
            return ""
        return "\n\nShutdown plan:\n\n" + "\n".join(lines)

    def _complete_wol_cycle(self, now_ts, wol_delay):
        """Run the wake-up sequence and decide whether the cycle is finished.

        Hosts gated on battery charge keep the POWER_RESTORED state alive so
        they get another chance on the next 15s pass, up to WOL_MAX_WAIT_MINUTES.
        """
        max_wait = int(self.config.get('WOL_MAX_WAIT_MINUTES', 240))
        waited = (now_ts - self.power_state_timestamp) // 60 if self.power_state_timestamp else 0
        force = max_wait > 0 and waited >= max_wait

        if force:
            log.warning(
                f"Waited {waited} min for the battery to charge (limit {max_wait} min) - "
                "waking the remaining hosts anyway."
            )

        deferred = self._initiate_wol(force=force)

        if deferred and not force:
            log.info(
                "WoL cycle still pending for: %s - will retry next check.",
                ", ".join(deferred),
            )
            return False

        self._clear_file(STATE_FILE)
        self._clear_file(CLIENT_NOTIFICATION_STATE_FILE)
        self.simulation_interrupted = False
        self.interrupted_schedule_info = None
        self.real_fail_notified = False
        return True

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

    def _poll_battery(self):
        """Read the battery monitor, if the integration is enabled.

        Never raises: battery data is an enhancement, and a failure here must
        leave the sentinel logic completely untouched.
        """
        self.battery_status = None
        if not self.battery_monitor.enabled:
            return

        self.battery_gap_cycles = self._read_battery_gap()

        try:
            self.battery_status = self.battery_monitor.get_status()
        except Exception as e:
            log.error(f"Battery monitor failed unexpectedly: {e}", exc_info=True)
            self.battery_status = None

        grace = self._fallback_grace_cycles()

        if self.battery_status:
            log.info(f"Battery: {self.battery_status.describe()}")
            if self.battery_gap_cycles:
                log.info(
                    "Battery data is back after %d missed poll(s).",
                    self.battery_gap_cycles,
                )
            self.battery_gap_cycles = 0
        else:
            self.battery_gap_cycles += 1
            reason = self.battery_monitor.last_error or "unknown reason"
            if self.battery_gap_cycles <= grace:
                log.warning(
                    "Battery data unavailable (%s) - missed poll %d of %d, "
                    "holding the previous per-host verdicts",
                    reason, self.battery_gap_cycles, grace,
                )
            else:
                log.warning(
                    "Battery data unavailable (%s) for %d consecutive polls "
                    "(limit %d) - falling back to sentinel logic",
                    reason, self.battery_gap_cycles, grace,
                )

        self._write_battery_gap(self.battery_gap_cycles)

    def _fallback_grace_cycles(self):
        """How many empty battery polls to ride out before falling back.

        The fallback is deliberately fail-safe - it sends hosts to the sentinel
        verdict, which during an outage means OB LB - so a momentary gap in the
        data is enough to shut a host down. On 2026-08-18 the UPS server itself
        was suspended for 56 seconds; the battery API came back a few seconds
        after the manager did, and in that window Synology Chomik was served
        OB LB at 69.9% SoC and powered off. Waiting a few cycles costs nothing:
        if mains really is gone, the sentinels have not moved and the fallback
        still arrives, just a minute later.
        """
        raw = self.config.get('BATTERY_FALLBACK_GRACE_CYCLES', 4)
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            log.warning(
                "Invalid BATTERY_FALLBACK_GRACE_CYCLES '%s' - using 4", raw
            )
            return 4

    def _read_battery_gap(self):
        """Load the consecutive-missed-poll counter, or 0 if unusable."""
        try:
            with open(BATTERY_GAP_STATE_FILE, 'r') as f:
                data = json.load(f)
            return max(0, int(data.get('consecutive_misses', 0)))
        except FileNotFoundError:
            return 0
        except (IOError, ValueError, AttributeError, json.JSONDecodeError) as e:
            log.warning(f"Cannot read battery gap state, starting fresh: {e}")
            return 0

    def _write_battery_gap(self, misses):
        """Persist the counter atomically; each iteration is a fresh process."""
        try:
            temp_file = BATTERY_GAP_STATE_FILE + ".tmp"
            with open(temp_file, 'w') as f:
                json.dump({'consecutive_misses': misses}, f)
            os.replace(temp_file, BATTERY_GAP_STATE_FILE)
        except (IOError, OSError, TypeError) as e:
            log.error(f"Cannot write battery gap state: {e}")

    def _battery_only_outage_cycles(self):
        """How long the disagreement must persist before it counts as real."""
        try:
            value = int(self.config.get('BATTERY_ONLY_OUTAGE_CYCLES', 8))
        except (TypeError, ValueError):
            log.warning(
                "Invalid BATTERY_ONLY_OUTAGE_CYCLES '%s' - using 8",
                self.config.get('BATTERY_ONLY_OUTAGE_CYCLES'),
            )
            return 8
        return max(1, value)

    def _read_battery_outage(self):
        """Load the battery-only outage counter and latch."""
        try:
            with open(BATTERY_OUTAGE_STATE_FILE, 'r') as f:
                data = json.load(f)
            return (max(0, int(data.get('consecutive', 0))),
                    bool(data.get('notified', False)))
        except FileNotFoundError:
            return 0, False
        except (IOError, ValueError, AttributeError, json.JSONDecodeError) as e:
            log.warning(f"Cannot read battery outage state, starting fresh: {e}")
            return 0, False

    def _write_battery_outage(self, consecutive, notified):
        """Persist it atomically; each iteration is a fresh process."""
        try:
            temp_file = BATTERY_OUTAGE_STATE_FILE + ".tmp"
            with open(temp_file, 'w') as f:
                json.dump({'consecutive': consecutive, 'notified': notified}, f)
            os.replace(temp_file, BATTERY_OUTAGE_STATE_FILE)
        except (IOError, OSError, TypeError) as e:
            log.error(f"Cannot write battery outage state: {e}")

    def _check_battery_only_outage(self):
        """Alert on an outage the sentinel hosts are structurally unable to see.

        The sentinels answer "is there grid power in the building". That is a
        proxy for the question that actually matters - "is the UPS being fed" -
        and the two come apart whenever the fault is confined to the UPS's own
        circuit. A tripped breaker or RCD is enough: the grid is fine, every
        sentinel answers, power_status never leaves ONLINE, and the ordinary
        outage alert never fires. Meanwhile the bank drains and the hosts shut
        down one after another on their state of charge thresholds, unannounced.

        The battery monitor is the only source measuring the right thing, so
        this alert hangs off it alone, with its own latch, independent of the
        sentinel-driven state machine - the condition can begin and end without
        power_status ever changing.

        It is debounced rather than immediate, because the same disagreement
        appears harmlessly at the *end* of an ordinary outage: the sentinels
        boot the moment the grid returns while the battery monitor still needs
        a few seconds of charge current to call it. On 2026-08-24 that window
        was two cycles wide. Anything short-lived is therefore ignored.
        """
        if not self.battery_monitor.enabled:
            return

        consecutive, notified = self._read_battery_outage()
        on_battery = (self.battery_status is not None
                      and self.battery_status.ac_power is False)
        # Only the disagreement interests us here. Once the sentinels agree,
        # the ordinary outage alert owns the event.
        unseen = on_battery and not self.real_power_offline

        consecutive = consecutive + 1 if unseen else 0
        threshold = self._battery_only_outage_cycles()

        if unseen and consecutive >= threshold and not notified:
            log.warning(
                "BATTERY-ONLY OUTAGE: mains lost according to the battery for %d "
                "consecutive checks while %d of %d sentinel hosts are still "
                "reachable - the UPS feed itself looks dead.",
                consecutive, self.sentinel_online_count, self.sentinel_total_count,
            )
            self.notifier.send(
                "POWER_FAIL", "[UPS] ALERT: UPS Lost Mains - Sentinels Still Up",
                "The battery monitor reports the UPS is running on battery, but "
                f"{self.sentinel_online_count} of {self.sentinel_total_count} "
                "sentinel hosts are still reachable - so grid power in the "
                "building looks fine.\n\n"
                "That points at the supply to the UPS rather than at the grid; a "
                "tripped breaker or RCD on its circuit is the usual cause. "
                "Nothing will restore it on its own, and the hosts will start "
                "shutting down as the bank drains."
                + self._battery_context() + self._shutdown_plan()
            )
            notified = True

        elif notified and not on_battery:
            log.info("BATTERY-ONLY OUTAGE cleared: the battery is on mains again.")
            self.notifier.send(
                "POWER_RESTORED", "[UPS] INFO: UPS Mains Restored",
                "The UPS is being fed from mains again."
                + self._battery_context()
            )
            notified = False

        self._write_battery_outage(consecutive, notified)

    def _read_published_verdicts(self):
        """Last per-host verdicts this manager published, keyed by IP."""
        try:
            with open(POWER_STATE_FILE, 'r') as f:
                data = json.load(f)
            hosts = data.get('hosts')
            return hosts if isinstance(hosts, dict) else {}
        except FileNotFoundError:
            return {}
        except (IOError, ValueError, AttributeError, json.JSONDecodeError) as e:
            log.warning(f"Cannot read the last published verdicts: {e}")
            return {}

    def _evaluate_hosts(self, power_status):
        """Run the per-host power evaluation and log anything noteworthy.

        Returns:
            Dict keyed by host IP, ready to publish in power_state.json.
        """
        verdicts = {}
        simulation = self.config.get('POWER_SIMULATION_MODE', 'false').lower() == 'true'

        # Inside the grace window a battery-sourced host keeps whatever it was
        # last told, rather than being handed the sentinel verdict. Holding the
        # published verdict rather than forcing OL is what makes this safe in
        # both directions: a host already on its way down stays on its way down.
        holding = (
            self.battery_monitor.enabled
            and self.battery_status is None
            and 0 < self.battery_gap_cycles <= self._fallback_grace_cycles()
        )
        previous = self._read_published_verdicts() if holding else {}

        # The real sentinel verdict, without the simulation override that
        # _determine_power_status() folds in - the evaluator needs both facts
        # separately to explain its reasoning.
        sentinel_offline = (
            self.sentinel_total_count > 0 and self.sentinel_online_count == 0
        )

        for section, params in self.wake_hosts.items():
            ip = params.get('IP')
            if not ip or 'SHUTDOWN_DELAY_MINUTES' not in params:
                continue  # WoL-only host, not a UPS client

            name = params.get('NAME', ip)

            if (holding and ip in previous
                    and self.evaluator.host_source(params) != SOURCE_SENTINEL):
                entry = dict(previous[ip])
                entry['name'] = params.get('NAME', section)
                entry['section'] = section
                entry['reason'] = (
                    "battery data missing for %d of %d allowed polls - holding "
                    "the previous verdict" % (
                        self.battery_gap_cycles, self._fallback_grace_cycles()
                    )
                )
                # The stored flag was raised against battery data we no longer
                # have; re-reporting it would just be noise.
                entry['disagreement'] = False
                verdicts[ip] = entry
                log.warning(
                    "HOLDING %s (%s): serving %s - battery data missing "
                    "for %d of %d allowed polls",
                    name, ip, entry.get('status'),
                    self.battery_gap_cycles, self._fallback_grace_cycles(),
                )
                continue

            try:
                verdict = self.evaluator.evaluate_host(
                    params, self.battery_status, sentinel_offline, simulation
                )
            except Exception as e:
                log.error(
                    f"Failed to evaluate host {name}: {e}",
                    exc_info=True,
                )
                continue

            entry = verdict.to_dict()
            entry['name'] = params.get('NAME', section)
            entry['section'] = section
            verdicts[ip] = entry

            log.info(
                "VERDICT %s (%s): serving %s [detail %s, source %s] - %s",
                name, ip, verdict.status, verdict.detail, verdict.source, verdict.reason,
            )

            # The whole point of observe mode: surface where the battery would
            # have decided differently, loudly enough to be found in the log.
            if verdict.disagreement:
                log.warning(
                    "SOURCE DISAGREEMENT %s (%s): sentinels imply %s, battery implies %s "
                    "- %s (mode=%s)",
                    name, ip,
                    'OB LB' if sentinel_offline else 'OL',
                    verdict.would_be, verdict.reason, verdict.mode,
                )

        return verdicts

    def _read_battery_wol_state(self):
        """Load the per-host battery wake-up tracker, or {} if unusable."""
        try:
            with open(BATTERY_WOL_STATE_FILE, 'r') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (IOError, json.JSONDecodeError) as e:
            log.warning(f"Cannot read battery WoL state, starting fresh: {e}")
            return {}

    def _write_battery_wol_state(self, tracked):
        """Persist the per-host battery wake-up tracker atomically."""
        try:
            temp_file = BATTERY_WOL_STATE_FILE + ".tmp"
            with open(temp_file, 'w') as f:
                json.dump(tracked, f, indent=2)
            os.replace(temp_file, BATTERY_WOL_STATE_FILE)
        except (IOError, OSError, TypeError) as e:
            log.error(f"Cannot write battery WoL state: {e}")

    def _handle_battery_wol(self, power_status, host_verdicts):
        """Wake hosts that the battery rules shut down by themselves.

        The POWER_FAIL/POWER_RESTORED machine is driven entirely by sentinel
        pings, so it never sees an outage that only the battery monitor
        noticed. Without this, a host shut down by its own state-of-charge
        threshold - while the sentinels stayed reachable and every other host
        kept running - would stay off until somebody pressed Wake, and nothing
        would be logged to say so.

        This is the per-host counterpart of that machine, and it deliberately
        stands down whenever the sentinel-driven cycle has anything to do
        (`self.power_state` set, or the sentinels reporting an outage), so the
        two can never both own the same wake-up or send duplicate mail.
        """
        if power_status != "ONLINE" or self.power_state:
            return

        tracked = self._read_battery_wol_state()
        original = json.dumps(tracked, sort_keys=True)
        now_ts = int(datetime.now().timestamp())
        wol_delay = int(self.config.get('WOL_DELAY_MINUTES', 5))
        max_wait = int(self.config.get('WOL_MAX_WAIT_MINUTES', 240))

        known_ips = set()

        for section, params in self.wake_hosts.items():
            ip = params.get('IP')
            verdict = host_verdicts.get(ip)
            if not verdict:
                continue

            # Sentinel-sourced hosts are the existing machine's business, and a
            # host we cannot wake is not worth tracking.
            if self.evaluator.host_source(params) == SOURCE_SENTINEL:
                continue
            if not params.get('MAC') or params.get('AUTO_WOL', 'true').lower() == 'false':
                continue

            known_ips.add(ip)
            name = params.get('NAME', ip)
            entry = tracked.get(ip)

            # Still down as far as the battery is concerned.
            if (verdict.get('status') == STATUS_LOW_BATTERY
                    and verdict.get('source') == SOURCE_BATTERY):
                if entry is None:
                    tracked[ip] = {'down_since': now_ts, 'restored_at': None}
                    log.info(
                        "Battery rules shut down %s (%s) with the sentinels still up - "
                        "this host is now owned by the battery wake-up cycle.", name, ip,
                    )
                elif entry.get('restored_at'):
                    # Dropped back onto battery before we managed to wake it.
                    entry['restored_at'] = None
                    log.info("%s (%s) is back on battery - wake-up postponed.", name, ip)
                continue

            if entry is None:
                continue

            # The battery says this host may run again.
            if not entry.get('restored_at'):
                entry['restored_at'] = now_ts
                log.info(
                    "Mains back for %s (%s) - waking it in %d min.", name, ip, wol_delay,
                )
                continue

            waited = (now_ts - entry['restored_at']) // 60
            if waited < wol_delay:
                continue

            force = max_wait > 0 and waited >= max_wait
            if force:
                log.warning(
                    "Waited %d min for the battery to charge before waking %s (limit %d min) - "
                    "waking it anyway.", waited, name, max_wait,
                )
            else:
                allowed, reason = self.evaluator.should_wol(params, self.battery_status)
                if not allowed:
                    log.info(f"Deferring WoL for {name} ({ip}): {reason}")
                    self._update_client_status_json(ip, "wol_deferred")
                    continue

            result = self._wake_host(params)
            if result in ('sent', 'online'):
                del tracked[ip]
                if result == 'sent':
                    self.notifier.send(
                        "POWER_RESTORED", "[UPS] INFO: WoL Sequence Initiated",
                        f"Sent WoL signal to:\n\n- {name} ({ip})\n\n"
                        "This host was shut down by the battery rules rather than by a "
                        "sentinel outage, so it was woken on its own schedule."
                        + self._battery_context()
                    )

        # Drop hosts that were removed from the config or switched to sentinel.
        for ip in [ip for ip in tracked if ip not in known_ips]:
            log.info("Dropping stale battery wake-up entry for %s.", ip)
            del tracked[ip]

        if json.dumps(tracked, sort_keys=True) != original:
            self._write_battery_wol_state(tracked)

    def _runtime_section(self) -> dict:
        """Site-wide runtime, measured to the last host shutdown.

        The battery monitor's own time-to-go counts down to the gauge's
        discharge floor, which on this bank is well above the point where
        anything actually shuts down - so the dashboard would show minutes
        while an hour of margin remained, then show nothing at all. This
        publishes the honest figure alongside it, and says which threshold it
        was measured against so the UI can label it.
        """
        clients = [
            params for params in self.wake_hosts.values()
            if params.get('IP') and 'SHUTDOWN_DELAY_MINUTES' in params
        ]
        target = self.evaluator.last_shutdown_soc(clients)
        if target is None:
            return {}

        return {
            'runtime_shutdown_soc': target,
            'runtime_to_shutdown_mins': self.evaluator.runtime_to_soc(
                self.battery_status, target
            ),
        }

    def _write_power_state(self, power_status, host_verdicts=None):
        """Publish the current power picture for the API and Web GUI.

        power_manager.py runs from cron while api.py and web_gui.py run under
        gunicorn, so this file is how the decision-making process hands its
        conclusions to the processes that serve them. Written atomically.
        """
        battery_section = {'enabled': self.battery_monitor.enabled, 'available': False}
        if self.battery_monitor.enabled:
            battery_section['url'] = self.battery_monitor.base_url
            battery_section['simulation'] = self.battery_monitor.simulation
            if self.battery_status:
                battery_section['available'] = True
                battery_section.update(self.battery_status.to_dict())
                battery_section.update(self._runtime_section())
            else:
                battery_section['error'] = self.battery_monitor.last_error

        state = {
            'updated_at': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
            'battery': battery_section,
            'sentinel': {
                'online': self.sentinel_online_count,
                'total': self.sentinel_total_count,
                'power': power_status,
            },
            'simulation': self.config.get('POWER_SIMULATION_MODE', 'false').lower() == 'true',
            'decision_mode': self.evaluator.mode,
            'hosts': host_verdicts or {},
        }

        try:
            temp_file = POWER_STATE_FILE + ".tmp"
            with open(temp_file, 'w') as f:
                json.dump(state, f, indent=2)
            # os.replace rather than os.rename: same atomic swap on POSIX, but
            # it also overwrites an existing target instead of failing.
            os.replace(temp_file, POWER_STATE_FILE)
        except (IOError, OSError, TypeError) as e:
            log.error(f"Cannot write power state file: {e}")

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

            self._poll_battery()
            power_status = self._determine_power_status()
            host_verdicts = self._evaluate_hosts(power_status)
            self._write_power_state(power_status, host_verdicts)

            # Deliberately before the handlers below: it must see the state the
            # cycle started with, so that it stands down the moment the
            # sentinel-driven machine takes ownership of a wake-up.
            self._handle_battery_wol(power_status, host_verdicts)

            # The NUT virtual device carries the site-wide status, so it has to
            # follow power_status and nothing else. Leaving it to the handlers
            # meant POWER_RESTORED_SIM published "OL" from _handle_power_online()
            # while the site ran on battery.
            self._update_ups_status_file(
                "ups.status: OB LB" if power_status == "OFFLINE" else "ups.status: OL"
            )

            # POWER_RESTORED_SIM means mains came back inside a simulation
            # window: power_status stays OFFLINE because the simulation forces
            # it, but the WoL countdown for IGNORE_SIMULATION hosts still has to
            # run. That only holds while mains is genuinely present. On
            # 2026-08-17 the grid dropped again 30s after returning and this
            # branch kept winning, so no failure was ever declared and the
            # countdown woke PVE2 five minutes into a live outage.
            if self.power_state == "POWER_RESTORED_SIM" and not self.real_power_offline:
                log.debug("Current state is POWER_RESTORED_SIM - handling WoL logic despite power_status")
                self._handle_power_online()  # This handles the POWER_RESTORED_SIM state
            elif power_status == "OFFLINE":
                self._handle_power_offline()
            else:
                self._handle_power_online()

            # Independent of the handlers above: this condition can begin and
            # end without power_status ever leaving ONLINE.
            self._check_battery_only_outage()

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
    # POWER_STATE_FILE is deliberately absent: _write_power_state() creates it
    # on the first cycle, and an empty placeholder would only make the API log
    # a parse warning until then.
    for f in [STATE_FILE, NOTIFICATION_STATE_FILE, CLIENT_NOTIFICATION_STATE_FILE,
              CLIENT_STATUS_FILE]:
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
            started = time.monotonic()

            # Create a fresh PowerManager for each iteration to pick up
            # any config changes made via the Web GUI between checks
            PowerManager().run(iteration=iteration)

            # Sleep the remainder of the interval, not the whole of it. An
            # iteration is near-instant while the sentinels answer, but each
            # unreachable one costs a ping timeout, so during an outage the work
            # itself takes ~4.5s and a flat 15s sleep pushed the run past 60s.
            # Cron then fired into the flock below and the whole minute was
            # skipped: on 2026-08-17 the manager ran every second minute, with
            # 63s gaps, exactly when it needed to be quickest. Schedules are only
            # evaluated on iteration 0, so a skipped minute can also drop a
            # scheduled start or stop.
            if iteration < CHECK_ITERATIONS - 1:
                remaining = CHECK_INTERVAL_SECONDS - (time.monotonic() - started)
                if remaining > 0:
                    time.sleep(remaining)
    finally:
        # Release lock file
        if lock_fd:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
            lock_fd.close()
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass