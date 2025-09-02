#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
UPS Hub REST API
Author: MarekWo
Description: Enhanced Flask-based REST API with bidirectional communication
             for UPS monitor clients.
"""

import configparser
import os
import subprocess
import json
from datetime import datetime
from flask import Flask, jsonify, request, abort

# --- Configuration ---
# The single, hardcoded token for all clients.
# IMPORTANT: Change this to a long, random string in your actual deployment.
API_TOKEN = "ggJVLx8MtcZvs84DVrSxzsiJPb5VoR4EMGUu"
UPSC_CMD = "/usr/bin/upsc"
POWER_MANAGER_CONFIG = "/etc/nut/power_manager.conf"
UPS_CONF_FILE = "/etc/nut/ups.conf"
CLIENT_STATUS_FILE = "/var/run/nut/client_status.json"

app = Flask(__name__)

# --- Helper Functions ---

def parse_upsc_value(value_str):
    """
    Tries to convert a string value to a more specific type (int, float).
    """
    value_str = value_str.strip()
    # Try integer first
    try:
        return int(value_str)
    except ValueError:
        pass
    # Try float
    try:
        return float(value_str)
    except ValueError:
        pass
    # Fallback to the original string
    return value_str

def build_nested_dict(flat_dict):
    """
    Converts a dictionary with dot-separated keys into a nested dictionary.
    Handles key conflicts gracefully (e.g., 'driver.version' and
    'driver.version.internal').
    """
    nested_dict = {}
    # By sorting the keys, we ensure that parent keys (like 'driver.version')
    # are processed before potential child keys ('driver.version.internal'),
    # leading to a consistent and predictable structure.
    for key, value in sorted(flat_dict.items()):
        parts = key.split('.')
        d = nested_dict
        for i, part in enumerate(parts[:-1]):
            # If the current path holds a value, we can't nest further.
            # So, we create a key from the remaining parts and break.
            if part in d and not isinstance(d[part], dict):
                remaining_key = '.'.join(parts[i:])
                d[remaining_key] = value
                break
            d = d.setdefault(part, {})
        else:  # This 'else' belongs to the 'for' loop
            # This block runs if the loop completed without a 'break'
            # Check if the final key would overwrite a dictionary
            final_key = parts[-1]
            if final_key in d and isinstance(d[final_key], dict):
                # A conflict where a parent node is also a value.
                # e.g. 'driver.version' exists, and we have 'driver.version.internal'
                # We can store the value with a special key, e.g., '_value'
                d[final_key]['_value'] = value
            else:
                d[final_key] = value
    return nested_dict

def read_power_manager_config():
    """
    Read and parse power_manager.conf file to get both main config and wake hosts.
    Returns tuple (main_config_dict, wake_hosts_dict).
    """
    config = {}
    wake_hosts = {}
    
    if not os.path.exists(POWER_MANAGER_CONFIG):
        return config, wake_hosts
    
    current_section = None
    with open(POWER_MANAGER_CONFIG, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            
            # Check for section headers [WAKE_HOST_X]
            if line.startswith('[WAKE_HOST_') and line.endswith(']'):
                current_section = line[1:-1]  # Remove brackets
                wake_hosts[current_section] = {}
                continue
            
            # Parse key=value pairs
            if '=' in line:
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip()
                
                # Remove quotes from values if present
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                elif value.startswith("'") and value.endswith("'"):
                    value = value[1:-1]
                elif value.startswith('"'):
                    value = value[1:]
                elif value.endswith('"'):
                    value = value[:-1]
                elif value.startswith("'"):
                    value = value[1:]
                elif value.endswith("'"):
                    value = value[:-1]
                
                if current_section:
                    # This is a wake host parameter
                    wake_hosts[current_section][key] = value
                else:
                    # This is a main config parameter
                    config[key] = value
    
    return config, wake_hosts

def get_client_ip():
    """
    Get the client's real IP address, considering proxies.
    """
    if request.headers.getlist("X-Forwarded-For"):
        return request.headers.getlist("X-Forwarded-For")[0]
    else:
        return request.remote_addr

def get_server_ip():
    """
    Determines the IP address for the UPS server from the environment.

    This function relies on the 'UPS_SERVER_HOST_IP' environment variable.
    This variable is mandatory for the API to report the correct IP address
    of the Docker host to the clients.
    """
    host_ip = os.environ.get('UPS_SERVER_HOST_IP')
    if not host_ip:
        # This is a critical configuration error. The application cannot
        # function correctly without it.
        error_msg = (
            "CRITICAL: The 'UPS_SERVER_HOST_IP' environment variable is not set. "
            "This variable must be defined in your .env file with the IP address "
            "of the Docker host. The API cannot continue without it."
        )
        app.logger.error(error_msg)
        abort(500, description=error_msg)
    
    app.logger.info(f"Using server IP from UPS_SERVER_HOST_IP environment variable: {host_ip}")
    return host_ip

def get_ups_name():
    """
    Parses ups.conf to find the name of the UPS, which is the first section
    defined in the file (e.g., [ups]). This method avoids using configparser
    to handle files with global settings outside of sections.
    """
    try:
        with open(UPS_CONF_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                # Ignore comments and empty lines
                if not line or line.startswith('#'):
                    continue

                # Check for a section header
                if line.startswith('[') and line.endswith(']'):
                    # Extract the name between the brackets
                    ups_name = line[1:-1].strip()
                    if ups_name:
                        return ups_name
        # If the loop completes without finding a valid section header
        raise ValueError(f"No UPS sections found in {UPS_CONF_FILE}")

    except (FileNotFoundError, ValueError) as e:
        app.logger.error(f"Could not read UPS name: {e}")
        abort(500, description=str(e))

# --- API Endpoints ---

@app.route('/upsc', methods=['GET'])
def get_upsc_data():
    """
    Endpoint to retrieve the live status of the UPS. It runs the 'upsc'
    command on the server and returns the data as a nested JSON object.
    """
    # 1. --- Security Check: Validate API Token ---
    auth_header = request.headers.get('Authorization')
    if not auth_header or auth_header != f"Bearer {API_TOKEN}":
        abort(401, description="Unauthorized: Missing or invalid API token.")

    app.logger.info("UPS status request received from a client.")

    # 2. --- Get UPS Name and run the upsc command ---
    try:
        ups_name = get_ups_name()
        command = [UPSC_CMD, f"{ups_name}@localhost"]
        app.logger.info(f"Executing command: {' '.join(command)}")

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True,  # Raises CalledProcessError on non-zero exit codes
            encoding='utf-8'
        )
    except FileNotFoundError:
        msg = f"Server error: The command '{UPSC_CMD}' was not found."
        app.logger.error(msg)
        abort(500, description=msg)
    except subprocess.CalledProcessError as e:
        msg = f"Error executing upsc command: {e.stderr.strip()}"
        app.logger.error(msg)
        abort(500, description=msg)

    # 3. --- Parse the output into a flat dictionary ---
    flat_data = {}
    for line in result.stdout.strip().split('\n'):
        key, value = line.split(':', 1)
        flat_data[key.strip()] = parse_upsc_value(value.strip())

    # 4. --- Convert to nested dictionary and return as JSON ---
    nested_data = build_nested_dict(flat_data)
    app.logger.info(f"Successfully retrieved and parsed UPS status.")
    return jsonify(nested_data)

@app.route('/config', methods=['GET'])
def get_config():
    """
    Endpoint to retrieve UPS configuration for a client.
    The client must provide its IP address as a query parameter
    and a valid API token in the 'Authorization' header.
    """
    # 1. --- Security Check: Validate API Token ---
    auth_header = request.headers.get('Authorization')
    if not auth_header or auth_header != f"Bearer {API_TOKEN}":
        abort(401, description="Unauthorized: Missing or invalid API token.")

    # 2. --- Get Client IP from the request ---
    client_ip = request.args.get('ip')
    if not client_ip:
        client_ip = get_client_ip()
        
    app.logger.info(f"Configuration request received for IP: {client_ip}")

    # 3. --- Read power_manager.conf and find client configuration ---
    try:
        main_config, wake_hosts = read_power_manager_config()
        
        # Look for the client IP in wake hosts sections
        client_config = None
        for section, params in wake_hosts.items():
            if params.get('IP') == client_ip:
                client_config = params.copy()
                break
        
        if not client_config:
            app.logger.warning(f"No configuration section found for IP: {client_ip}")
            abort(404, description=f"No configuration found for IP address {client_ip}")
        
        # Extract only the needed parameters for the client
        response_config = {}
        
        # Get shutdown delay (required)
        shutdown_delay = client_config.get('SHUTDOWN_DELAY_MINUTES')
        if shutdown_delay:
            response_config['SHUTDOWN_DELAY_MINUTES'] = shutdown_delay
        else:
            # Default to 5 minutes if not specified
            response_config['SHUTDOWN_DELAY_MINUTES'] = '5'
            app.logger.warning(f"No SHUTDOWN_DELAY_MINUTES found for {client_ip}, using default: 5")
        
        # Generate UPS_NAME
        ups_name = get_ups_name()
        server_ip = get_server_ip()
        response_config['UPS_NAME'] = f"{ups_name}@{server_ip}"
        
        app.logger.info(f"Generated configuration for {client_ip}: {response_config}")
        return jsonify(response_config)
        
    except Exception as e:
        app.logger.error(f"Error reading configuration: {e}")
        abort(500, description=f"Server configuration error: {e}")

@app.route('/status', methods=['POST'])
def update_client_status():
    """
    Endpoint for clients to post their status.
    """
    auth_header = request.headers.get('Authorization')
    if not auth_header or auth_header != f"Bearer {API_TOKEN}":
        abort(401, description="Unauthorized: Missing or invalid API token.")

    data = request.get_json()
    if not data or 'ip' not in data or 'status' not in data:
        abort(400, description="Bad Request: Missing 'ip' or 'status' in JSON payload.")

    client_ip = data['ip']
    client_status = {
        'status': data['status'],
        'remaining_seconds': data.get('remaining_seconds', None),
        'shutdown_delay': data.get('shutdown_delay', None),
        'timestamp': datetime.utcnow().isoformat()
    }

    # Read existing statuses
    try:
        if os.path.exists(CLIENT_STATUS_FILE):
            with open(CLIENT_STATUS_FILE, 'r') as f:
                statuses = json.load(f)
        else:
            statuses = {}
    except (IOError, json.JSONDecodeError):
        statuses = {}

    # Update status for the specific client
    statuses[client_ip] = client_status
    
    # Write back to the file
    try:
        with open(CLIENT_STATUS_FILE, 'w') as f:
            json.dump(statuses, f)
    except IOError as e:
        app.logger.error(f"Could not write client status file: {e}")
        abort(500, description="Server error: Could not write status file.")

    return jsonify({"message": "Status updated successfully"}), 200

if __name__ == '__main__':
    # For production, use a proper WSGI server like Gunicorn or uWSGI.
    # For debugging, you can run this script directly.
    # Host '0.0.0.0' makes it accessible from other machines on the network.
    app.run(host='0.0.0.0', port=5000, debug=True)