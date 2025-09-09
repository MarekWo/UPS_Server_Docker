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
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from werkzeug.exceptions import BadRequest


# Add the current directory to Python path to import api module
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api import API_TOKEN, get_ups_name, get_server_ip

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
                    current_section = None # Reset if it's not a known section type
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
        
        # Write main configuration
        for key in sorted(config.keys()):
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

def validate_ip(ip):
    """Validate IP address"""
    try:
        ipaddress.ip_address(ip)
        return True
    except:
        return False

def validate_mac(mac):
    """Validate MAC address"""
    import re
    mac_pattern = re.compile(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$')
    return bool(mac_pattern.match(mac))

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

        return render_template('dashboard.html',
                             pm_config=pm_config,
                             wake_hosts=wake_hosts,
                             ups_clients=ups_clients,
                             sentinel_hosts=sentinel_hosts,
                             sentinel_status=sentinel_status,
                             wake_host_status=wake_host_status,
                             client_statuses=client_statuses) 
    except Exception as e:
        flash(f'Error loading configuration: {str(e)}', 'error')
        return render_template('dashboard.html')

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
                             schedules=schedules)
    except Exception as e:
        app.logger.error(f"Error in config route: {str(e)}", exc_info=True)
        flash(f'Error loading configuration: {str(e)}', 'error')
        return f"Error: {str(e)}", 500


@app.route('/save_main_config', methods=['POST'])
def save_main_config():
    """Save main power manager configuration"""
    try:
        pm_config, wake_hosts, schedules = read_power_manager_config()
        
        # Update main config
        pm_config['SENTINEL_HOSTS'] = request.form.get('sentinel_hosts', '')
        pm_config['WOL_DELAY_MINUTES'] = request.form.get('wol_delay_minutes', '5')
        pm_config['DEFAULT_BROADCAST_IP'] = request.form.get('default_broadcast_ip', '192.168.1.255')
        pm_config['POWER_SIMULATION_MODE'] = 'true' if 'power_simulation_mode' in request.form else 'false'
        # UPS_STATE_FILE is read-only in the form, but we ensure it's preserved
        if 'UPS_STATE_FILE' not in pm_config:
            pm_config['UPS_STATE_FILE'] = request.form.get('ups_state_file', '/var/run/nut/virtual.device')


        # Validate IPs in sentinel hosts
        sentinel_ips = pm_config['SENTINEL_HOSTS'].split()
        for ip in sentinel_ips:
            if ip and not validate_ip(ip):
                flash(f'Invalid IP address: {ip}', 'error')
                return redirect(url_for('config'))
        
        # Validate broadcast IP
        if not validate_ip(pm_config['DEFAULT_BROADCAST_IP']):
            flash('Invalid default broadcast IP address', 'error')
            return redirect(url_for('config'))
        
        write_power_manager_config(pm_config, wake_hosts, schedules)
        flash('Main configuration saved successfully!', 'success')
        
    except Exception as e:
        flash(f'Error saving configuration: {str(e)}', 'error')
    
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
        wake_hosts[section_name] = {'NAME': name, 'IP': ip, 'MAC': mac, 'AUTO_WOL': auto_wol}
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