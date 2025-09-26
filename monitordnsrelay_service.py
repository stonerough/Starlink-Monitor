#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import subprocess
import smtplib
import requests
import socket
import json
import re
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from dotenv import load_dotenv, find_dotenv
from typing import List, Optional, Tuple, Dict, Any

# Attempt to import starlink-grpc-core, which is an optional dependency.
# If it fails, the Starlink-specific functions will be disabled.
try:
    from starlink_grpc import grpc_channel, ChannelDeadlineExceeded
    HAS_STARLINK_GRPC = True
except ImportError:
    # This dependency is not strictly required but allows for the enhanced firmware checks.
    HAS_STARLINK_GRPC = False
    print("Warning: 'starlink-grpc-core' not found. Firmware check functionality will be disabled.")
except Exception:
    # Catch any other initialization errors
    HAS_STARLINK_GRPC = False


# Load environment variables from .env file
load_dotenv(find_dotenv())

# ==============================================================================
# CONFIGURATION
# Hardcoded user-specific values have been replaced with environment lookups.
# ==============================================================================

# DNS servers to monitor.
# NOTE: The script will only declare an outage if ALL of these servers are down.
DNS_SERVERS = ["1.1.1.1", "8.8.8.8", "1.0.0.1", "8.8.4.4"]

# Normal ping interval in seconds (60s for standard production monitoring).
NORMAL_PING_INTERVAL = 60

# Reduced ping interval in seconds when an outage is detected (5s for faster recovery detection).
REDUCED_PING_INTERVAL = 5

# Ping timeout in seconds.
PING_TIMEOUT = 3

# Starlink gRPC endpoint (standard local IP).
STARLINK_STATUS_URL = os.getenv("STARLINK_STATUS_URL", "192.168.100.1:9200")

# Interval for checking Starlink firmware version (in hours).
STARLINK_CHECK_INTERVAL_HOURS = 6

# Email configuration loaded from environment variables (see .env.example)
FROM_ADDR = os.getenv("SENDER_EMAIL")
TO_ADDR = os.getenv("RECIPIENT_EMAIL")
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USERNAME = os.getenv("SENDER_EMAIL")
SMTP_PASSWORD = os.getenv("SENDER_PASSWORD")
EMAIL_SUBJECT_PREFIX = "Starlink DNS Monitor"

# File paths loaded from environment or set to relative defaults
BASE_DATA_DIR = "./data"
if not os.path.exists(BASE_DATA_DIR):
    os.makedirs(BASE_DATA_DIR, exist_ok=True)

log_file_path = os.getenv("LOG_FILE_PATH", os.path.join(BASE_DATA_DIR, "monitordnsrelay.log"))
version_file_path = os.getenv("STARLINK_VERSION_FILE", os.path.join(BASE_DATA_DIR, "starlink_version.json"))

# ==============================================================================
# STATE VARIABLES
# ==============================================================================
is_outage = False
last_outage_start: Optional[datetime] = None
last_version_check: Optional[datetime] = None
outage_log_message_sent = False
# Counter for status logging, only log current status every N checks
STATUS_LOG_INTERVAL = 25
check_count = 0


# ==============================================================================
# UTILITY FUNCTIONS
# ==============================================================================

def validate_configuration() -> List[str]:
    """Ensures critical environment variables are set."""
    issues = []
    if not FROM_ADDR:
        issues.append("SENDER_EMAIL is not set in the environment or .env file.")
    if not TO_ADDR:
        issues.append("RECIPIENT_EMAIL is not set in the environment or .env file.")
    if not SMTP_PASSWORD:
        issues.append("SENDER_PASSWORD is not set in the environment or .env file (must be Gmail App Password).")
    return issues


def log_event(message: str) -> None:
    """Logs the event message with a timestamp to the log file and stdout."""
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"{timestamp} - {message}"

        # Write to file
        with open(log_file_path, "a") as f:
            f.write(log_entry + "\n")

        # Print to console (only if running interactively)
        if sys.stdout.isatty():
            print(log_entry)

    except Exception as e:
        # Emergency log to stdout/stderr if file logging fails
        print(f"ERROR: Failed to write to log file {log_file_path}: {e}", file=sys.stderr)


def send_email(subject: str, body: str) -> bool:
    """Sends an email notification."""
    if not all([FROM_ADDR, TO_ADDR, SMTP_USERNAME, SMTP_PASSWORD]):
        log_event("Email not sent: Missing one or more SMTP configuration parameters.")
        return False

    msg = MIMEText(body)
    msg["Subject"] = f"[{EMAIL_SUBJECT_PREFIX}] {subject}"
    msg["From"] = FROM_ADDR
    msg["To"] = TO_ADDR

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()  # Secure the connection
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(FROM_ADDR, [TO_ADDR], msg.as_string())
        log_event(f"Email notification sent: {subject}")
        return True
    except smtplib.SMTPAuthenticationError:
        log_event("ERROR: SMTP Authentication failed. Check SENDER_EMAIL and SENDER_PASSWORD (App Password).")
        return False
    except Exception as e:
        log_event(f"ERROR: Failed to send email: {e}")
        return False


def check_dns_server(host: str, timeout: int) -> bool:
    """Performs a simple DNS query check for a single server using subprocess/ping."""
    try:
        # Use subprocess to run ping and check exit code
        command = ["ping", "-c", "1", "-W", str(timeout), host]
        result = subprocess.run(command, capture_output=True, timeout=timeout + 1)
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False
    except FileNotFoundError:
        # Fallback for systems without a standard 'ping' command
        return check_tcp_port(host, 53, timeout)
    except Exception:
        # Fallback to TCP check for generic errors
        return check_tcp_port(host, 53, timeout)


def check_tcp_port(host: str, port: int, timeout: int) -> bool:
    """Checks if a TCP port (e.g., 53) is open."""
    try:
        with socket.create_connection((host, port), timeout) as sock:
            return True
    except socket.error:
        return False


def save_starlink_version(version: str) -> None:
    """Saves the current Starlink version to a JSON file."""
    try:
        data: Dict[str, Any] = {"current_version": version, "last_updated": datetime.now().isoformat()}

        # Read existing data to maintain history, if desired, but here we just update 'current_version'
        # To maintain version history, the structure in reportdnsrelay.py would need to be reflected here.
        # For simplicity, we just store the current version needed for comparison.
        with open(version_file_path, "w") as f:
            json.dump(data, f)
    except Exception as e:
        log_event(f"ERROR: Failed to save Starlink version to {version_file_path}: {e}")


def load_starlink_version() -> Optional[str]:
    """Loads the last recorded Starlink version from the JSON file."""
    try:
        with open(version_file_path, "r") as f:
            data = json.load(f)
            return data.get("current_version")
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    except Exception as e:
        log_event(f"ERROR: Failed to load Starlink version from {version_file_path}: {e}")
        return None


def get_starlink_software_version() -> Optional[str]:
    """
    Retrieves the current software version from the Starlink dish using gRPC.
    This uses the starlink-grpc-core library.
    """
    if not HAS_STARLINK_GRPC:
        return None

    try:
        # This function relies on starlink-grpc-core
        with grpc_channel(STARLINK_STATUS_URL, timeout=10) as channel:
            # The channel context manager handles connection and deadline exceeded
            version = channel.get_status().device_info.software_version
            return version
    except ChannelDeadlineExceeded:
        log_event(f"Warning: Starlink gRPC timeout accessing {STARLINK_STATUS_URL}. Assuming no update for now.")
        return None
    except Exception as e:
        log_event(f"ERROR: Failed to get Starlink software version via gRPC: {e}. Check connectivity or port.")
        return None


def should_check_starlink_version(last_check: Optional[datetime]) -> bool:
    """Determines if enough time has passed to check the Starlink version again."""
    if not HAS_STARLINK_GRPC:
        return False
    if last_check is None:
        return True  # Always check on first run

    time_since_last_check = datetime.now() - last_check
    return time_since_last_check >= timedelta(hours=STARLINK_CHECK_INTERVAL_HOURS)


# ==============================================================================
# MAIN MONITORING LOOP
# ==============================================================================

def main_loop():
    """The core monitoring logic."""
    global is_outage, last_outage_start, last_version_check, outage_log_message_sent, check_count

    print("Starting Enhanced DNS monitoring service...")
    current_interval = NORMAL_PING_INTERVAL

    # Initial Starlink version check
    current_version = get_starlink_software_version()
    if current_version:
        log_event(f"Initial Starlink version recorded: {current_version}")
        save_starlink_version(current_version)
        last_version_check = datetime.now()
    else:
        log_event("Starlink version check failed on startup. Continuing without version tracking.")


    while True:
        try:
            up_hosts = []
            down_hosts = []

            # 1. Check all DNS servers
            for host in DNS_SERVERS:
                if check_dns_server(host, PING_TIMEOUT):
                    up_hosts.append(host)
                else:
                    down_hosts.append(host)

            # 2. Check Starlink Version (periodically and not during an existing outage)
            if not is_outage and should_check_starlink_version(last_version_check):
                new_version = get_starlink_software_version()
                last_version_check = datetime.now() # Update check time regardless of success

                if new_version:
                    recorded_version = load_starlink_version()
                    if recorded_version and new_version != recorded_version:
                        log_event(
                            f"Starlink Firmware Update Detected: {recorded_version} -> {new_version}"
                        )
                        save_starlink_version(new_version)
                    elif recorded_version is None:
                        # Case where file was deleted or corrupted
                        save_starlink_version(new_version)
                    # Log status periodically
                    check_count += 1
                    if check_count % STATUS_LOG_INTERVAL == 0:
                         log_event(f"Status check ({check_count}): System operational. Starlink version: {new_version}")


            # 3. Handle Outage State Transition
            if not up_hosts:
                # INTERNET OUTAGE DETECTED
                if not is_outage:
                    # New outage started
                    is_outage = True
                    last_outage_start = datetime.now()
                    message = f"Internet outage detected: ALL DNS servers failed ({', '.join(down_hosts)})"
                    log_event(message)
                    outage_log_message_sent = False # Reset flag for logging

                # During an ongoing outage, use reduced interval
                current_interval = REDUCED_PING_INTERVAL

                # Log periodic status update during outage (every 5 minutes)
                if last_outage_start and not outage_log_message_sent and (datetime.now() - last_outage_start) >= timedelta(minutes=5):
                    log_event("Outage duration exceeds 5 minutes. Reduced logging interval active.")
                    outage_log_message_sent = True


            else:
                # CONNECTION RESTORED
                if is_outage:
                    # Outage just ended
                    outage_end_time = datetime.now()
                    outage_duration = outage_end_time - last_outage_start if last_outage_start else timedelta(0)

                    # Prepare email body
                    email_subject = "Outage Ended"
                    email_body = f"Internet connectivity restored.\n\n"
                    if last_outage_start:
                        message = f"Outage ended. Duration: {outage_duration.total_seconds():.2f} seconds. Working DNS servers: {', '.join(up_hosts)}"
                        email_body += f"Outage Start: {last_outage_start.strftime('%Y-%m-%d %H:%M:%S')}\n"
                        email_body += f"Outage End: {outage_end_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                        email_body += f"Duration: {str(outage_duration).split('.')[0]} (H:MM:SS)\n"
                        email_body += f"\nWorking DNS servers: {', '.join(up_hosts)}"
                    else:
                        # Should not happen if outage detection is correct, but safe fallback
                        message = f"Outage ended (start time unknown). Working DNS servers: {', '.join(up_hosts)}"
                        email_body += (
                            f"\nWorking DNS servers: {', '.join(up_hosts)}"
                        )

                    log_event(message)
                    send_email(email_subject, email_body)
                    is_outage = False
                    last_outage_start = None # Reset outage state for next event

                # Return to normal interval
                current_interval = NORMAL_PING_INTERVAL

                # Log periodic operational status
                check_count += 1
                if check_count % STATUS_LOG_INTERVAL == 0:
                    log_event(f"Status check ({check_count}): System operational. Working DNS servers: {', '.join(up_hosts)}")


        except KeyboardInterrupt:
            log_event("Enhanced DNS monitoring service stopped by user")
            print("\nMonitoring stopped by user.")
            break
        except Exception as e:
            error_msg = f"An error occurred in the main loop: {type(e).__name__} - {e}"
            log_event(error_msg)
            if sys.stdout.isatty():
                print(error_msg)

        # Wait for the next check.
        time.sleep(current_interval)


if __name__ == "__main__":
    # Validate configuration before starting
    config_issues = validate_configuration()
    if config_issues:
        print("Configuration issues found:")
        for issue in config_issues:
            print(f"  - {issue}")
        print("\nPlease fix these issues before running the script.")
        sys.exit(1)

    try:
        main_loop()
    except Exception as e:
        log_event(f"Fatal error: {type(e).__name__} - {e}")
        print(f"Fatal error: {type(e).__name__} - {e}")
        sys.exit(1)
