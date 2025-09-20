#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import os
import smtplib
import re
from email.mime.text import MIMEText
from email.utils import formataddr

# --- Configuration ---
POWER_MANAGER_CONFIG = "/etc/nut/power_manager.conf"

def read_power_manager_config():
    """Read and parse power_manager.conf file to get main config."""
    config = {}
    if not os.path.exists(POWER_MANAGER_CONFIG):
        return config
    
    with open(POWER_MANAGER_CONFIG, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '[' in line:
                continue
            
            if '=' in line:
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip().strip('"\'').strip()
                config[key] = value
    return config

def send_email(subject, body, config):
    """Send an email using configured SMTP settings."""
    try:
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

        msg = MIMEText(body, 'plain', 'utf-8')
        msg['Subject'] = subject
        msg['From'] = formataddr((smtp_sender_name, smtp_sender_email))
        msg['To'] = ', '.join(smtp_recipients)

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
        print("Email sent successfully.")
    except Exception as e:
        print(f"Failed to send email: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: send_email.py <'Subject'> <'Body'>", file=sys.stderr)
        sys.exit(1)
    
    email_subject = sys.argv[1]
    email_body = sys.argv[2]
    
    config_data = read_power_manager_config()
    send_email(email_subject, email_body, config_data)