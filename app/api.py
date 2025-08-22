#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
UPS Hub REST API
Author: Gemini
Description: A lightweight Flask-based REST API to serve configuration
             to UPS monitor clients from a central INI file.
"""

import configparser
import os
from flask import Flask, jsonify, request, abort

# --- Configuration ---
# The single, hardcoded token for all clients.
# IMPORTANT: Change this to a long, random string in your actual deployment.
API_TOKEN = "ggJVLx8MtcZvs84DVrSxzsiJPb5VoR4EMGUu"
UPSHUB_CONFIG_FILE = "/etc/nut/upshub.conf"
UPS_CONF_FILE = "/etc/nut/ups.conf"

app = Flask(__name__)

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
        # Using abort() is a clean way to send standard HTTP error responses
        abort(401, description="Unauthorized: Missing or invalid API token.")

    # 2. --- Get Client IP from the request ---
    # We use the IP from the query parameter as the primary identifier.
    # This allows for flexibility if the client's perceived IP is different.
    client_ip = request.args.get('ip')
    if not client_ip:
        # If the 'ip' parameter is missing, fall back to the requester's IP.
        client_ip = get_client_ip()
        
    app.logger.info(f"Configuration request received for IP: {client_ip}")

    # 3. --- Read and Parse the INI Configuration File ---
    config = configparser.ConfigParser()
    config.optionxform = str
    try:
        # Use read() which returns an empty list if the file doesn't exist.
        if not config.read(UPSHUB_CONFIG_FILE):
            raise FileNotFoundError(f"Client configuration file not found at {UPSHUB_CONFIG_FILE}")
            
    except FileNotFoundError as e:
        app.logger.error(e)
        # 500 Internal Server Error
        abort(500, description=str(e))
    except configparser.Error as e:
        app.logger.error(f"Error parsing configuration file: {e}")
        abort(500, description=f"Server configuration error: {e}")

    # 4. --- Find and Return the Client's Configuration ---
    if client_ip in config:
        # The section for the client exists. Convert it to a dictionary.
        # This will contain client-specific settings like SHUTDOWN_DELAY_MINUTES.
        client_config = dict(config[client_ip])
        app.logger.info(f"Found base configuration for {client_ip}: {client_config}")

        # 5. --- Dynamically Generate and Add the UPS_NAME ---
        ups_name = get_ups_name()
        server_ip = get_server_ip()
        client_config['UPS_NAME'] = f"{ups_name}@{server_ip}"

        app.logger.info(f"Generated full configuration for {client_ip}: {client_config}")
        return jsonify(client_config)
    else:
        # The client's IP was not found in the config file.
        app.logger.warning(f"No configuration section found for IP: {client_ip}")
        # 404 Not Found
        abort(404, description=f"No configuration found for IP address {client_ip}")

if __name__ == '__main__':
    # For production, use a proper WSGI server like Gunicorn or uWSGI.
    # For debugging, you can run this script directly.
    # Host '0.0.0.0' makes it accessible from other machines on the network.
    app.run(host='0.0.0.0', port=5000, debug=True)
