#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import re
import datetime
import smtplib
from email.mime.text import MIMEText
import os
import json
import sys
from dotenv import load_dotenv, find_dotenv
from typing import List, Dict, Optional, Any, Tuple
from datetime import datetime, timedelta

# Load environment variables from .env file
load_dotenv(find_dotenv())

# ==============================================================================
# CONFIGURATION
# ==============================================================================
# Email configuration loaded from environment variables
FROM_ADDR = os.getenv("SENDER_EMAIL")
TO_ADDR = os.getenv("RECIPIENT_EMAIL")
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USERNAME = os.getenv("SENDER_EMAIL")
SMTP_PASSWORD = os.getenv("SENDER_PASSWORD")

# File paths loaded from environment or set to relative defaults
BASE_DATA_DIR = "./data"
log_file_path = os.getenv(
    "LOG_FILE_PATH", os.path.join(BASE_DATA_DIR, "monitordnsrelay.log")
)
version_file_path = os.getenv(
    "STARLINK_VERSION_FILE", os.path.join(BASE_DATA_DIR, "starlink_version.json")
)

# ==============================================================================
# CORE FUNCTIONS
# ==============================================================================


def load_starlink_version_data(version_file_path: str) -> Dict[str, Any]:
    """Load Starlink version tracking data from JSON file."""
    try:
        with open(version_file_path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        # Return a structure that prevents crashes
        return {"current_version": "Unknown", "version_history": []}
    except Exception as e:
        print(f"Error loading Starlink version data: {e}", file=sys.stderr)
        return {"current_version": "Unknown", "version_history": []}


def parse_outages_from_log(
    log_file_path: str, days_to_analyze: int = 30
) -> List[Dict[str, Any]]:
    """
    Parse outages from the log file. Looks for 'Internet outage detected' and 'Outage ended' pairs.
    Returns outages that ended within the last `days_to_analyze` days.
    """
    try:
        with open(log_file_path, "r") as f:
            log_content = f.read()
    except FileNotFoundError:
        print(f"Error: Log file not found at {log_file_path}", file=sys.stderr)
        return []
    except Exception as e:
        print(f"Error reading log file: {e}", file=sys.stderr)
        return []

    # Regex patterns
    outage_start_pattern = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) - Internet outage detected"
    )
    outage_end_pattern = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) - Outage ended\. Duration: ([\d\.]+) seconds"
    )
    firmware_update_pattern = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) - Starlink Firmware Update Detected: (.+?) -> (.+?)"
    )

    log_lines = log_content.splitlines()
    outages: List[Dict[str, Any]] = []
    current_outage: Optional[Dict[str, Any]] = None
    start_date = datetime.now() - timedelta(days=days_to_analyze)

    # 1. First pass: Identify start/end pairs and full outage blocks
    for line in log_lines:
        start_match = outage_start_pattern.search(line)
        end_match = outage_end_pattern.search(line)

        if start_match and current_outage is None:
            # New outage starts
            current_outage = {
                "start": datetime.strptime(start_match.group(1), "%Y-%m-%d %H:%M:%S"),
                "end": None,
                "duration_seconds": 0.0,
                "firmware_update": False,
                "notes": "",
            }
        elif end_match and current_outage is not None:
            # Current outage ends
            end_time = datetime.strptime(end_match.group(1), "%Y-%m-%d %H:%M:%S")
            duration = float(end_match.group(2))

            current_outage["end"] = end_time
            current_outage["duration_seconds"] = duration

            # Only include outages that ended within the analysis window
            if current_outage["end"] >= start_date:
                outages.append(current_outage)

            current_outage = None  # Reset for the next outage

    # 2. Second pass: Cross-reference outages with firmware updates
    firmware_updates: List[Dict[str, Any]] = []
    for line in log_lines:
        firmware_match = firmware_update_pattern.search(line)
        if firmware_match:
            firmware_time = datetime.strptime(
                firmware_match.group(1), "%Y-%m-%d %H:%M:%S"
            )
            old_version = firmware_match.group(2)
            new_version = firmware_match.group(3)

            firmware_updates.append(
                {
                    "time": firmware_time,
                    "old_version": old_version,
                    "new_version": new_version,
                }
            )

    # 3. Correlate outages with firmware updates
    FIRMWARE_OUTAGE_WINDOW_MINUTES = (
        30  # An outage within 30 minutes of a detected update is considered related
    )

    for outage in outages:
        outage_start: datetime = outage["start"]
        outage_end: datetime = outage["end"]  # type: ignore # mypy: End is always set for a complete outage

        for update in firmware_updates:
            update_time: datetime = update["time"]

            # Check if the update occurred near the start of the outage
            time_diff = abs((outage_start - update_time).total_seconds()) / 60

            if time_diff <= FIRMWARE_OUTAGE_WINDOW_MINUTES:
                outage["firmware_update"] = True
                outage["notes"] = (
                    f"Correlated with Firmware Update ({update['old_version']} -> {update['new_version']}) detected at {update['time'].strftime('%H:%M:%S')}"
                )
                break  # Move to the next outage

    return outages


def analyze_firmware_stats(outages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Analyzes outages to categorize and total firmware-related vs. connectivity-related downtime."""
    firmware_downtime = 0.0
    connectivity_downtime = 0.0
    firmware_outage_count = 0
    unique_firmware_updates: Dict[str, str] = {}  # {new_version: old_version}

    for outage in outages:
        duration = outage["duration_seconds"]
        if outage["firmware_update"]:
            firmware_downtime += duration
            firmware_outage_count += 1
            # Extract new version from notes for unique count (simple approach)
            match = re.search(r"-> (.+?)\)", outage["notes"])
            if match:
                new_version = match.group(1)
                # This assumes the 'notes' field is reliably populated with the version
                unique_firmware_updates[new_version] = outage["notes"]
        else:
            connectivity_downtime += duration

    return {
        "firmware_downtime": firmware_downtime,
        "connectivity_downtime": connectivity_downtime,
        "total_downtime": firmware_downtime + connectivity_downtime,
        "firmware_outage_count": firmware_outage_count,
        "connectivity_outage_count": len(outages) - firmware_outage_count,
        "unique_firmware_updates": len(unique_firmware_updates),
        "update_details": [
            re.search(r"\((.+)\)", details).group(1)
            for details in unique_firmware_updates.values()
            if re.search(r"\((.+)\)", details)
        ],
    }


def calculate_uptime_percentage(outages: List[Dict[str, Any]], days: int = 30) -> float:
    """Calculates the uptime percentage based on the total outage duration."""
    total_seconds_in_period = days * 24 * 60 * 60
    total_downtime_seconds = sum(outage["duration_seconds"] for outage in outages)
    uptime_seconds = total_seconds_in_period - total_downtime_seconds

    if total_seconds_in_period <= 0:
        return 0.0

    uptime_percentage = (uptime_seconds / total_seconds_in_period) * 100

    # Ensure percentage is between 0 and 100 (handling edge cases)
    return max(0.0, min(100.0, uptime_percentage))


def format_outage_report(
    outages: List[Dict[str, Any]],
    uptime_percentage: float,
    firmware_stats: Dict[str, Any],
    current_version: str,
) -> str:
    """Formats the final email body for the report."""
    report = []
    report.append("--- STARLINK MONITORING REPORT ---\n")
    report.append(
        f"Report Period: Last 30 Days (ending {datetime.now().strftime('%Y-%m-%d %H:%M:%S')})\n"
    )
    report.append("-" * 40)
    report.append(f"Current Starlink Software Version: {current_version}\n")

    # UPTIME SUMMARY
    report.append("### UPTIME SUMMARY")
    report.append(f"Total Uptime (30 Days): {uptime_percentage:.2f}%")
    report.append(
        f"Total Downtime: {str(timedelta(seconds=firmware_stats['total_downtime'])).split('.')[0]} (H:MM:SS)\n"
    )

    # DOWNTIME CLASSIFICATION
    report.append("### DOWNTIME CLASSIFICATION")
    report.append(
        f"Connectivity Outages (Unscheduled/ISP-related): {firmware_stats['connectivity_outage_count']} events"
    )
    report.append(
        f"   Total Downtime: {str(timedelta(seconds=firmware_stats['connectivity_downtime'])).split('.')[0]}"
    )
    report.append(
        f"Firmware Outages (Scheduled/Maintenance): {firmware_stats['firmware_outage_count']} events"
    )
    report.append(
        f"   Total Downtime: {str(timedelta(seconds=firmware_stats['firmware_downtime'])).split('.')[0]}"
    )
    report.append(
        f"Unique Firmware Updates Recorded: {firmware_stats['unique_firmware_updates']}\n"
    )

    if firmware_stats["update_details"]:
        report.append("   Update Details:")
        for detail in firmware_stats["update_details"]:
            report.append(f"     - {detail}")
        report.append("\n")

    # DETAILED OUTAGE LOG
    report.append("### DETAILED OUTAGE LOG (Last 30 Days)")
    if not outages:
        report.append("No outages recorded in the last 30 days.")
    else:
        for outage in outages:
            duration_td = timedelta(seconds=outage["duration_seconds"])
            duration_str = str(duration_td).split(".")[0]

            status = (
                "FIRMWARE (Maintenance)"
                if outage["firmware_update"]
                else "CONNECTIVITY (Unscheduled)"
            )
            notes = outage["notes"] if outage["notes"] else "No specific notes."

            report.append("-" * 30)
            report.append(f"Status: {status}")
            report.append(f"Start: {outage['start'].strftime('%Y-%m-%d %H:%M:%S')}")
            report.append(f"End:   {outage['end'].strftime('%Y-%m-%d %H:%M:%S')}")  # type: ignore
            report.append(f"Duration: {duration_str} (H:MM:SS)")
            report.append(f"Notes: {notes}")

    report.append("\n--- END OF REPORT ---")
    return "\n".join(report)


def send_email_report(subject: str, body: str) -> bool:
    """Sends the final report via SMTP."""
    if not all([FROM_ADDR, TO_ADDR, SMTP_USERNAME, SMTP_PASSWORD]):
        print(
            "Email not sent: Missing one or more SMTP configuration parameters (SENDER_EMAIL, RECIPIENT_EMAIL, SENDER_PASSWORD).",
            file=sys.stderr,
        )
        return False

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = FROM_ADDR
    msg["To"] = TO_ADDR

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(FROM_ADDR, [TO_ADDR], msg.as_string())
        return True
    except smtplib.SMTPAuthenticationError:
        print(
            "ERROR: SMTP Authentication failed. Check SENDER_EMAIL and SENDER_PASSWORD (App Password).",
            file=sys.stderr,
        )
        return False
    except Exception as e:
        print(f"ERROR: Failed to send email report: {e}", file=sys.stderr)
        return False


def prune_log_file(log_file_path: str, days_to_keep: int = 60) -> None:
    """
    Rotates and prunes the log file by keeping only entries from the last N days.
    Also creates a dated backup before pruning.
    """
    cutoff_date = datetime.now() - timedelta(days=days_to_keep)

    try:
        with open(log_file_path, "r") as f:
            lines = f.readlines()
    except FileNotFoundError:
        print("Log file not found, skipping pruning.")
        return
    except Exception as e:
        print(f"Error reading log file for pruning: {e}", file=sys.stderr)
        return

    kept_lines = []

    # 1. Determine lines to keep
    for line in lines:
        # Log lines start with the timestamp
        try:
            timestamp_str = line[:19]
            log_time = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
            if log_time >= cutoff_date:
                kept_lines.append(line)
        except ValueError:
            # Keep lines that don't match the standard timestamp format (e.g., error logs)
            kept_lines.append(line)
        except Exception:
            kept_lines.append(line)

    # 2. Create dated backup of the original file
    backup_file_path = (
        f"{log_file_path}.backup_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    )
    try:
        if len(kept_lines) < len(lines):
            os.rename(log_file_path, backup_file_path)
            # 3. Write the pruned content back to the original file path
            with open(log_file_path, "w") as f:
                f.writelines(kept_lines)
            print(
                f"Pruning complete. Old log backed up to {os.path.basename(backup_file_path)}. {len(lines) - len(kept_lines)} lines removed."
            )
        else:
            print(
                f"No log entries older than {days_to_keep} days found. Skipping pruning and backup."
            )

    except Exception as e:
        print(f"ERROR: Log pruning or backup failed: {e}", file=sys.stderr)
        # Attempt to restore original file content if an error occurred during write
        if os.path.exists(backup_file_path):
            os.rename(backup_file_path, log_file_path)


def main():
    """Main execution function for report generation and pruning."""
    # 1. Load data
    starlink_data = load_starlink_version_data(version_file_path)
    current_version = starlink_data.get(
        "current_version", "Unknown (Check service log)"
    )

    outages = parse_outages_from_log(log_file_path, days_to_analyze=30)

    # 2. Analyze data
    firmware_stats = analyze_firmware_stats(outages)
    uptime_percentage = calculate_uptime_percentage(outages)

    # 3. Generate report
    report_body = format_outage_report(
        outages, uptime_percentage, firmware_stats, current_version
    )

    # 4. Create enhanced email subject
    today = datetime.date.today().strftime("%Y-%m-%d")
    firmware_count = firmware_stats["firmware_outage_count"]

    # Generalized subject line for public use
    email_subject = (
        f"Starlink Monitoring Report ({today}) | Uptime: {uptime_percentage:.2f}%"
    )
    if firmware_count > 0:
        email_subject += f" | {firmware_count} Maintenance Events"

    # 5. Prune old log entries (60 days) - do this before sending report
    print("Checking for old log entries to prune (60 days)...")
    prune_log_file(log_file_path, days_to_keep=60)

    # 6. Send email report
    if send_email_report(email_subject, report_body):
        print(
            f"\nNetwork outage report sent successfully. Uptime: {uptime_percentage:.2f}%"
        )
        if firmware_count > 0:
            print(
                f"Report included {firmware_count} firmware updates and {firmware_stats['connectivity_outage_count']} connectivity issues"
            )
    else:
        print("\nFailed to send network outage report.")

    # Also print report to console for immediate feedback
    print("\n" + "=" * 80)
    print("REPORT PREVIEW (Console Output)")
    print("=" * 80)
    print(report_body)
    print("=" * 80)


if __name__ == "__main__":
    if not os.path.exists(BASE_DATA_DIR):
        print(f"Creating data directory: {BASE_DATA_DIR}")
        os.makedirs(BASE_DATA_DIR, exist_ok=True)

    if not os.path.exists(log_file_path):
        print(f"Log file not found at {log_file_path}. Creating an empty one.")
        try:
            with open(log_file_path, "w") as f:
                f.write("# Initial log file for Starlink Monitor\n")
        except Exception as e:
            print(f"Could not create log file: {e}")
            sys.exit(1)

    main()
