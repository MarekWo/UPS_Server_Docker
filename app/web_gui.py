#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
UPS Server Web GUI
Author: MarekWo, Claude
Description: A web interface for managing UPS Server configuration with unified power_manager.conf
"""

import os
import sys
import subprocess
import ipaddress
import json
import smtplib
import re
from email.mime.text import MIMEText
from email.utils import formataddr
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from werkzeug.exceptions import BadRequest


# Add the current directory to Python path to import modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api import API_TOKEN, get_ups_name, get_server_ip

# Import version information
try:
    from version_info import get_version_info, get_version_string
except ImportError:
    # Fallback if version_info module is not available
    def get_version_info():
        return {
            "version_string": "development",
            "commit_hash": "unknown",
            "source": "fallback"
        }
    def get_version_string():
        return "development"

app = Flask(__name__)
app.secret_key = 'ups_server_gui_secret_key_change_in_production'

# --- Configuration ---
POWER_MANAGER_CONFIG = "/etc/nut/power_manager.conf"
PING_CMD = "/bin/ping"
WAKEONLAN_CMD = "/usr/bin/wakeonlan"
CLIENT_STATUS_FILE = "/var/run/nut/client_status.json"

# --- Helper Functions ---

def get_client_statuses():
    """Reads the client status file."""
    if not os.path.exists(CLIENT_STATUS_FILE):
        return {}
    try:
        with open(CLIENT_STATUS_FILE, 'r') as f:
            return json.load(f)
    except (IOError, json.JSONDecodeError):
        return {}

def read_power_manager_config():
    """Read and parse power_manager.conf file to get main config, wake hosts, and schedules."""
    config = {}
    wake_hosts = {}
    schedules = {}
    current_section = None
    
    if not os.path.exists(POWER_MANAGER_CONFIG):
        return config, wake_hosts, schedules
    
    with open(POWER_MANAGER_CONFIG, 'r') as f:
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
    
    return config, wake_hosts, schedules


def write_power_manager_config(config, wake_hosts, schedules):
    """Write power_manager.conf file, preserving comments and structure is hard, so we rewrite."""
    with open(POWER_MANAGER_CONFIG, 'w') as f:
        f.write("# === CONFIGURATION FILE FOR POWER_MANAGER.SH ===\n\n")
        
        # Main config keys order
        main_keys = [
            'SENTINEL_HOSTS', 'WOL_DELAY_MINUTES', 'CLIENT_STALE_TIMEOUT_MINUTES', 
            'UPS_STATE_FILE', 'DEFAULT_BROADCAST_IP', 'API_TOKEN', 'POWER_SIMULATION_MODE'
        ]
        
        # Write main configuration
        for key in main_keys:
            if key in config:
                 f.write(f"{key}=\"{config[key]}\"\n")
        
        f.write("\n# === SMTP NOTIFICATIONS ===\n")
        smtp_keys = [
            'SMTP_SERVER', 'SMTP_PORT', 'SMTP_USE_TLS', 'SMTP_USER', 'SMTP_PASSWORD',
            'SMTP_SENDER_NAME', 'SMTP_SENDER_EMAIL', 'SMTP_RECIPIENTS'
        ]
        for key in smtp_keys:
            if key in config and config[key]:
                f.write(f"{key}=\"{config[key]}\"\n")

        f.write("\n# === NOTIFICATION SETTINGS ===\n")
        notify_keys = [
            'NOTIFY_POWER_FAIL', 'NOTIFY_POWER_RESTORED', 'NOTIFY_CLIENT_SHUTDOWN',
            'NOTIFY_CLIENT_STALE', 'NOTIFY_APP_ERROR', 'NOTIFY_SIMULATION_MODE'
        ]
        for key in notify_keys:
            if key in config:
                f.write(f"{key}=\"{config[key]}\"\n")


        f.write("\n# === WAKE-ON-LAN HOST DEFINITIONS ===\n")
        
        # Write wake hosts
        for section in sorted(wake_hosts.keys()):
            f.write(f"\n[{section}]\n")
            for key, value in wake_hosts[section].items():
                f.write(f"{key}=\"{value}\"\n")

        f.write("\n# === POWER OUTAGE SIMULATION SCHEDULES ===\n")
        
        # Write schedules
        for section in sorted(schedules.keys()):
            f.write(f"\n[{section}]\n")
            for key, value in schedules[section].items():
                f.write(f"{key}=\"{value}\"\n")

def ping_host(ip):
    """Check if host is online"""
    try:
        result = subprocess.run(
            [PING_CMD, "-c", "1", "-W", "1", ip],
            capture_output=True,
            timeout=2
        )
        return result.returncode == 0
    except:
        return False

def send_wol(mac, broadcast_ip):
    """Send Wake-on-LAN packet"""
    try:
        subprocess.run([WAKEONLAN_CMD, "-i", broadcast_ip, mac], 
                      capture_output=True, check=True)
        return True
    except:
        return False

def send_email(subject, body, config):
    """Send an email using configured SMTP settings."""
    try:
        # Extract SMTP configuration
        smtp_server = config.get('SMTP_SERVER')
        smtp_port_str = config.get('SMTP_PORT')
        smtp_user = config.get('SMTP_USER')
        smtp_password = config.get('SMTP_PASSWORD')
        smtp_sender_name = config.get('SMTP_SENDER_NAME')
        smtp_sender_email = config.get('SMTP_SENDER_EMAIL')
        smtp_recipients_str = config.get('SMTP_RECIPIENTS')
        smtp_use_tls = config.get('SMTP_USE_TLS', 'auto').lower()  # New option

        if not all([smtp_server, smtp_port_str, smtp_sender_email, smtp_recipients_str]):
            raise ValueError("SMTP server, port, sender email, and recipients must be configured.")

        smtp_port = int(smtp_port_str)
        smtp_recipients = [email.strip() for email in smtp_recipients_str.split(',') if email.strip()]

        # Create message
        msg = MIMEText(body, 'plain', 'utf-8')
        msg['Subject'] = subject
        msg['From'] = formataddr((smtp_sender_name, smtp_sender_email))
        msg['To'] = ', '.join(smtp_recipients)

        # Send email
        server = None
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
        
        server.sendmail(smtp_sender_email, smtp_recipients, msg.as_string())
        server.quit()
        return True, "Email sent successfully."
    except Exception as e:
        app.logger.error(f"Failed to send email: {str(e)}")
        return False, str(e)


def validate_ip(ip):
    """Validate IP address"""
    try:
        ipaddress.ip_address(ip)
        return True
    except:
        return False

def validate_mac(mac):
    """Validate MAC address"""
    mac_pattern = re.compile(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$')
    return bool(mac_pattern.match(mac))

def validate_email_list(emails_str):
    """Validate a comma-separated list of emails."""
    if not emails_str:
        return True # Empty is considered valid.
    
    email_pattern = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")
    emails = [email.strip() for email in emails_str.split(',')]
    
    for email in emails:
        if email and not email_pattern.match(email):
            return False
    return True

def get_ups_clients_from_wake_hosts(wake_hosts):
    """Extract UPS clients (hosts with SHUTDOWN_DELAY_MINUTES) from wake hosts"""
    ups_clients = {}
    for section, params in wake_hosts.items():
        if 'SHUTDOWN_DELAY_MINUTES' in params and 'IP' in params:
            ups_clients[section] = {
                'ip': params['IP'],
                'name': params.get('NAME', section),
                'shutdown_delay': params['SHUTDOWN_DELAY_MINUTES']
            }
    return ups_clients

# --- Routes ---

@app.route('/')
def index():
    """Main dashboard"""
    try:
        # Read configurations
        pm_config, wake_hosts, _ = read_power_manager_config()
        
        # Get UPS clients from wake hosts
        ups_clients = get_ups_clients_from_wake_hosts(wake_hosts)
        
        # Calculate non-UPS hosts (hosts without SHUTDOWN_DELAY_MINUTES)
        non_ups_hosts = {
            section: host
            for section, host in wake_hosts.items()
            if 'SHUTDOWN_DELAY_MINUTES' not in host
        }
        
        # Get status information
        sentinel_hosts_raw = pm_config.get('SENTINEL_HOSTS', '').split()
        sentinel_hosts = []
        sentinel_status = {}
        
        # Clean up sentinel hosts (remove quotes and empty entries)
        for host in sentinel_hosts_raw:
            if host:
                # Remove quotes from host IP
                clean_host = host.strip().strip('"').strip("'")
                if clean_host and validate_ip(clean_host):
                    sentinel_hosts.append(clean_host)
                    sentinel_status[clean_host] = ping_host(clean_host)
                elif clean_host:
                    # Invalid IP but not empty - still add for display but mark as offline
                    sentinel_hosts.append(clean_host)
                    sentinel_status[clean_host] = False
        
        # Get wake host status
        wake_host_status = {}
        for section, params in wake_hosts.items():
            ip = params.get('IP', '')
            if ip:
                wake_host_status[section] = ping_host(ip)
        
        # Get client statuses
        client_statuses = get_client_statuses()

        # Get version information
        version_info = get_version_info()

        return render_template('dashboard.html',
                             pm_config=pm_config,
                             wake_hosts=wake_hosts,
                             ups_clients=ups_clients,
                             non_ups_hosts=non_ups_hosts,
                             sentinel_hosts=sentinel_hosts,
                             sentinel_status=sentinel_status,
                             wake_host_status=wake_host_status,
                             client_statuses=client_statuses,
                             version_info=version_info) 
    except Exception as e:
        flash(f'Error loading configuration: {str(e)}', 'error')
        return render_template('dashboard.html', version_info=get_version_info())

@app.route('/config')
def config():
    """Configuration page"""
    try:
        pm_config, wake_hosts, schedules = read_power_manager_config()
        ups_clients = get_ups_clients_from_wake_hosts(wake_hosts)
        
        return render_template('config.html',
                             pm_config=pm_config,
                             wake_hosts=wake_hosts,
                             ups_clients=ups_clients,
                             schedules=schedules,
                             version_info=get_version_info())
    except Exception as e:
        app.logger.error(f"Error in config route: {str(e)}", exc_info=True)
        flash(f'Error loading configuration: {str(e)}', 'error')
        return f"Error: {str(e)}", 500

# --- New Version Endpoint ---
@app.route('/version')
def version_endpoint():
    """API endpoint to get version information"""
    return jsonify(get_version_info())

# [Rest of the routes remain the same as in the original file]
@app.route('/save_main_config', methods=['POST'])
def save_main_config():
    """Save main and SMTP power manager configuration"""
    try:
        pm_config, wake_hosts, schedules = read_power_manager_config()
        
        # --- Update main config ---
        pm_config['SENTINEL_HOSTS'] = request.form.get('sentinel_hosts', '')
        pm_config['WOL_DELAY_MINUTES'] = request.form.get('wol_delay_minutes', '5')
        pm_config['CLIENT_STALE_TIMEOUT_MINUTES'] = request.form.get('client_stale_timeout_minutes', '5')
        pm_config['DEFAULT_BROADCAST_IP'] = request.form.get('default_broadcast_ip', '192.168.1.255')
        pm_config['POWER_SIMULATION_MODE'] = 'true' if 'power_simulation_mode' in request.form else 'false'
        if 'UPS_STATE_FILE' not in pm_config:
            pm_config['UPS_STATE_FILE'] = request.form.get('ups_state_file', '/var/run/nut/virtual.device')

        # --- Update SMTP config ---
        pm_config['SMTP_SERVER'] = request.form.get('smtp_server', '')
        pm_config['SMTP_PORT'] = request.form.get('smtp_port', '')
        pm_config['SMTP_USER'] = request.form.get('smtp_user', '')
        pm_config['SMTP_PASSWORD'] = request.form.get('smtp_password', '')
        pm_config['SMTP_SENDER_NAME'] = request.form.get('smtp_sender_name', '')
        pm_config['SMTP_SENDER_EMAIL'] = request.form.get('smtp_sender_email', '')
        pm_config['SMTP_RECIPIENTS'] = request.form.get('smtp_recipients', '')
        pm_config['SMTP_USE_TLS'] = request.form.get('smtp_use_tls', 'auto')  # New field

        # --- Update Notification settings ---
        notify_keys = [
            'notify_power_fail', 'notify_power_restored', 'notify_client_shutdown',
            'notify_client_stale', 'notify_app_error', 'notify_simulation_mode'
        ]
        for key in notify_keys:
            # Convert to uppercase for the config file
            config_key = key.upper() 
            pm_config[config_key] = 'true' if key in request.form else 'false'

        # --- Validation ---
        sentinel_ips = pm_config['SENTINEL_HOSTS'].split()
        for ip in sentinel_ips:
            if ip and not validate_ip(ip):
                flash(f'Invalid IP address in Sentinel Hosts: {ip}', 'error')
                return redirect(url_for('config'))
        
        if not validate_ip(pm_config['DEFAULT_BROADCAST_IP']):
            flash('Invalid Default Broadcast IP address', 'error')
            return redirect(url_for('config'))

        if pm_config.get('SMTP_RECIPIENTS') and not validate_email_list(pm_config['SMTP_RECIPIENTS']):
             flash('Invalid email address format in Recipients field.', 'error')
             return redirect(url_for('config'))
        
        if pm_config.get('SMTP_SENDER_EMAIL') and not validate_email_list(pm_config['SMTP_SENDER_EMAIL']):
             flash('Invalid email address format in Sender Email field.', 'error')
             return redirect(url_for('config'))

        # --- Write the combined configuration ---
        write_power_manager_config(pm_config, wake_hosts, schedules)
        flash('Configuration saved successfully!', 'success')
        
    except Exception as e:
        flash(f'Error saving configuration: {str(e)}', 'error')
    
    return redirect(url_for('config'))


@app.route('/test_smtp', methods=['POST'])
def test_smtp():
    """Send a test email."""
    try:
        pm_config, _, _ = read_power_manager_config()
        
        subject = "[UPS] Test Email from UPS Power Management Server"
        body = "This is a test email to verify that your SMTP configuration is correct.\n\n"
        body += "If you received this, notifications are working."
        
        success, message = send_email(subject, body, pm_config)
        
        if success:
            flash(f'Test email sent successfully to {pm_config.get("SMTP_RECIPIENTS")}!', 'success')
        else:
            flash(f'Failed to send test email: {message}', 'error')
            
    except Exception as e:
        flash(f'An unexpected error occurred: {str(e)}', 'error')
        
    return redirect(url_for('config'))


@app.route('/add_wake_host', methods=['POST'])
def add_wake_host():
    """Add new wake host"""
    try:
        pm_config, wake_hosts, schedules = read_power_manager_config()

        # Find next available wake host number
        existing_numbers = [int(s.replace('WAKE_HOST_', '')) for s in wake_hosts.keys() if s.startswith('WAKE_HOST_')]
        next_num = max(existing_numbers) + 1 if existing_numbers else 1
        section_name = f"WAKE_HOST_{next_num}"

        # Get form data and validate
        name = request.form.get('name', '').strip()
        ip = request.form.get('ip', '').strip()
        mac = request.form.get('mac', '').strip()
        broadcast_ip = request.form.get('broadcast_ip', '').strip()
        shutdown_delay = request.form.get('shutdown_delay', '').strip()
        auto_wol = 'true' if 'auto_wol' in request.form else 'false'
        ignore_simulation = 'true' if 'ignore_simulation' in request.form else 'false'
        
        if not all([name, ip, mac]):
            flash('Name, IP, and MAC address are required', 'error')
            return redirect(url_for('config'))
        if not validate_ip(ip):
            flash(f'Invalid IP address: {ip}', 'error')
            return redirect(url_for('config'))
        if not validate_mac(mac):
            flash(f'Invalid MAC address: {mac}', 'error')
            return redirect(url_for('config'))
        if broadcast_ip and not validate_ip(broadcast_ip):
            flash(f'Invalid broadcast IP address: {broadcast_ip}', 'error')
            return redirect(url_for('config'))
        
        # Add new wake host
        wake_hosts[section_name] = {'NAME': name, 'IP': ip, 'MAC': mac, 'AUTO_WOL': auto_wol, 'IGNORE_SIMULATION': ignore_simulation}
        if broadcast_ip:
            wake_hosts[section_name]['BROADCAST_IP'] = broadcast_ip
        if shutdown_delay:
            wake_hosts[section_name]['SHUTDOWN_DELAY_MINUTES'] = shutdown_delay
        
        write_power_manager_config(pm_config, wake_hosts, schedules)
        flash(f'Host "{name}" added successfully!', 'success')
        
    except Exception as e:
        flash(f'Error adding host: {str(e)}', 'error')
    
    return redirect(url_for('config'))

@app.route('/edit_wake_host/<section>', methods=['POST'])
def edit_wake_host(section):
    """Edit existing wake host"""
    try:
        pm_config, wake_hosts, schedules = read_power_manager_config()
        
        if section not in wake_hosts:
            flash('Host not found', 'error')
            return redirect(url_for('config'))
        
        # Get form data and validate
        name = request.form.get('name', '').strip()
        ip = request.form.get('ip', '').strip()
        mac = request.form.get('mac', '').strip()
        broadcast_ip = request.form.get('broadcast_ip', '').strip()
        shutdown_delay = request.form.get('shutdown_delay', '').strip()
        auto_wol = 'true' if 'auto_wol' in request.form else 'false'
        ignore_simulation = 'true' if 'ignore_simulation' in request.form else 'false'

        if not all([name, ip, mac]):
            flash('Name, IP, and MAC address are required.', 'error')
            return redirect(url_for('config'))
        if not validate_ip(ip):
            flash(f'Invalid IP address: {ip}', 'error')
            return redirect(url_for('config'))
        if not validate_mac(mac):
            flash(f'Invalid MAC address: {mac}', 'error')
            return redirect(url_for('config'))
        if broadcast_ip and not validate_ip(broadcast_ip):
            flash(f'Invalid broadcast IP address: {broadcast_ip}', 'error')
            return redirect(url_for('config'))
        
        # Update wake host data
        wake_hosts[section]['NAME'] = name
        wake_hosts[section]['IP'] = ip
        wake_hosts[section]['MAC'] = mac
        wake_hosts[section]['AUTO_WOL'] = auto_wol
        wake_hosts[section]['IGNORE_SIMULATION'] = ignore_simulation
        
        if broadcast_ip:
            wake_hosts[section]['BROADCAST_IP'] = broadcast_ip
        elif 'BROADCAST_IP' in wake_hosts[section]:
            del wake_hosts[section]['BROADCAST_IP']

        if shutdown_delay:
            wake_hosts[section]['SHUTDOWN_DELAY_MINUTES'] = shutdown_delay
        elif 'SHUTDOWN_DELAY_MINUTES' in wake_hosts[section]:
            del wake_hosts[section]['SHUTDOWN_DELAY_MINUTES']
        
        write_power_manager_config(pm_config, wake_hosts, schedules)
        flash(f'Host "{name}" updated successfully!', 'success')
        
    except Exception as e:
        flash(f'Error updating host: {str(e)}', 'error')
    
    return redirect(url_for('config'))

@app.route('/delete_wake_host/<section>', methods=['POST'])
def delete_wake_host(section):
    """Delete wake host"""
    try:
        pm_config, wake_hosts, schedules = read_power_manager_config()
        
        if section in wake_hosts:
            name = wake_hosts[section].get('NAME', section)
            del wake_hosts[section]
            write_power_manager_config(pm_config, wake_hosts, schedules)
            flash(f'Host "{name}" deleted successfully!', 'success')
        else:
            flash('Host not found', 'error')
        
    except Exception as e:
        flash(f'Error deleting host: {str(e)}', 'error')
    
    return redirect(url_for('config'))

@app.route('/add_schedule', methods=['POST'])
def add_schedule():
    """Add a new schedule entry."""
    try:
        pm_config, wake_hosts, schedules = read_power_manager_config()
        
        existing_numbers = [int(s.replace('SCHEDULE_', '')) for s in schedules.keys() if s.startswith('SCHEDULE_')]
        next_num = max(existing_numbers) + 1 if existing_numbers else 1
        section_name = f"SCHEDULE_{next_num}"
        
        new_schedule = {
            'NAME': request.form.get('name'),
            'TYPE': request.form.get('type'),
            'TIME': request.form.get('time'),
            'ACTION': request.form.get('action'),
            'ENABLED': 'true' if 'enabled' in request.form else 'false'
        }
        
        if new_schedule['TYPE'] == 'one-time':
            new_schedule['DATE'] = request.form.get('date')
        else:
            new_schedule['DAY_OF_WEEK'] = request.form.get('day_of_week')
        
        schedules[section_name] = new_schedule
        write_power_manager_config(pm_config, wake_hosts, schedules)
        flash(f'Schedule "{new_schedule["NAME"]}" added successfully!', 'success')

    except Exception as e:
        flash(f'Error adding schedule: {str(e)}', 'error')
        
    return redirect(url_for('config'))

@app.route('/edit_schedule/<section>', methods=['POST'])
def edit_schedule(section):
    """Edit an existing schedule entry."""
    try:
        pm_config, wake_hosts, schedules = read_power_manager_config()
        if section not in schedules:
            flash('Schedule not found', 'error')
            return redirect(url_for('config'))
            
        updated_schedule = {
            'NAME': request.form.get('name'),
            'TYPE': request.form.get('type'),
            'TIME': request.form.get('time'),
            'ACTION': request.form.get('action'),
            'ENABLED': 'true' if 'enabled' in request.form else 'false'
        }

        # Clear old type-specific fields before updating
        schedules[section].pop('DATE', None)
        schedules[section].pop('DAY_OF_WEEK', None)

        if updated_schedule['TYPE'] == 'one-time':
            updated_schedule['DATE'] = request.form.get('date')
        else:
            updated_schedule['DAY_OF_WEEK'] = request.form.get('day_of_week')

        schedules[section].update(updated_schedule)
        write_power_manager_config(pm_config, wake_hosts, schedules)
        flash(f'Schedule "{updated_schedule["NAME"]}" updated successfully!', 'success')
        
    except Exception as e:
        flash(f'Error updating schedule: {str(e)}', 'error')

    return redirect(url_for('config'))

@app.route('/delete_schedule/<section>', methods=['POST'])
def delete_schedule(section):
    """Delete a schedule entry."""
    try:
        pm_config, wake_hosts, schedules = read_power_manager_config()
        if section in schedules:
            name = schedules[section].get('NAME', section)
            del schedules[section]
            write_power_manager_config(pm_config, wake_hosts, schedules)
            flash(f'Schedule "{name}" deleted successfully!', 'success')
        else:
            flash('Schedule not found', 'error')
    
    except Exception as e:
        flash(f'Error deleting schedule: {str(e)}', 'error')
        
    return redirect(url_for('config'))

@app.route('/wol/<section>')
def wake_host(section):
    """Send Wake-on-LAN to specific host"""
    try:
        pm_config, wake_hosts, _ = read_power_manager_config()
        
        if section not in wake_hosts:
            return jsonify({'success': False, 'message': 'Host not found'})
        
        host = wake_hosts[section]
        mac = host.get('MAC')
        broadcast_ip = host.get('BROADCAST_IP', pm_config.get('DEFAULT_BROADCAST_IP', '192.168.1.255'))
        name = host.get('NAME', section)
        
        if not mac:
            return jsonify({'success': False, 'message': 'MAC address not found'})
        
        if send_wol(mac, broadcast_ip):
            return jsonify({'success': True, 'message': f'Wake-on-LAN sent to {name}'})
        else:
            return jsonify({'success': False, 'message': f'Failed to send Wake-on-LAN to {name}'})
        
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/status')
def get_status():
    """Get current status of all hosts"""
    try:
        pm_config, wake_hosts, _ = read_power_manager_config()
        
        # Check sentinel hosts
        sentinel_hosts = pm_config.get('SENTINEL_HOSTS', '').split()
        sentinel_status = {}
        for host in sentinel_hosts:
            if host:
                clean_host = host.strip().strip('"').strip("'")
                if clean_host:
                    sentinel_status[clean_host] = ping_host(clean_host)
        
        # Check wake hosts
        wake_host_status = {}
        for section, params in wake_hosts.items():
            ip = params.get('IP', '')
            if ip:
                wake_host_status[section] = ping_host(ip)
        
        return jsonify({
            'sentinel_status': sentinel_status,
            'wake_host_status': wake_host_status
        })
        
    except Exception as e:
        return jsonify({'error': str(e)})
    
@app.route('/client_statuses')
def get_client_statuses_json():
    """Endpoint to get current client statuses as JSON."""
    return jsonify(get_client_statuses())

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=80, debug=True)