#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
UPS Hub REST API
Author: Gemini
Description: A lightweight Flask-based REST API to serve configuration
             to UPS monitor clients from a central INI file.
"""

import configparser
from flask import Flask, jsonify, request, abort

# --- Configuration ---
# The single, hardcoded token for all clients.
# IMPORTANT: Change this to a long, random string in your actual deployment.
API_TOKEN = "ggJVLx8MtcZvs84DVrSxzsiJPb5VoR4EMGUu"
CONFIG_FILE = "/etc/nut/upshub.conf"

app = Flask(__name__)

def get_client_ip():
    """
    Get the client's real IP address, considering proxies.
    """
    if request.headers.getlist("X-Forwarded-For"):
        return request.headers.getlist("X-Forwarded-For")[0]
    else:
        return request.remote_addr

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
        if not config.read(CONFIG_FILE):
            raise FileNotFoundError(f"Configuration file not found at {CONFIG_FILE}")
            
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
        client_config = dict(config[client_ip])
        app.logger.info(f"Found configuration for {client_ip}: {client_config}")
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
