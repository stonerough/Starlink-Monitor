## IMPLEMENTATION

This document provides a technical deep-dive into the core functions and methodologies implemented within the two Python scripts.

-----

## 1\. `monitordnsrelay_service.py`: State Machine and gRPC Integration

The monitoring script operates as a simple, event-driven state machine managing two primary states: **Operational** and **Outage**.

### Connectivity and Outage Declaration

The function `check_dns_server()` is critical. Instead of relying on a single DNS query or the `socket` library alone, it implements a robust fall-through strategy to account for different system environments:

1.  **Primary Check:** Executes `ping -c 1 -W 3` via `subprocess.run()`. This is fast, low-overhead, and checks true ICMP connectivity, which is the most reliable measure for general network health.
2.  **Fallback Check:** If `ping` fails (e.g., command not found or generic error), it falls back to `check_tcp_port(host, 53, timeout)`. This checks if the standard DNS UDP port 53 is open via a TCP connection, confirming basic reachability to the DNS server, even if ICMP is blocked or missing.

**Outage Threshold:** An outage is declared **only if all** configured `DNS_SERVERS` fail the check. This multi-homing approach minimizes false positives caused by a single unresponsive DNS server.

### Starlink gRPC Interaction

The core value-add is the `get_starlink_software_version()` function, which uses the `starlink-grpc-core` library to communicate directly with the dish.

```python
# Snippet from monitordnsrelay_service.py (concept)
from starlink_grpc import grpc_channel, ChannelDeadlineExceeded

def get_starlink_software_version() -> Optional[str]:
    try:
        # Use context manager for reliable channel handling
        with grpc_channel(STARLINK_STATUS_URL, timeout=10) as channel:
            # Retrieves full status; we extract the software_version field
            version = channel.get_status().device_info.software_version
            return version
    except ChannelDeadlineExceeded:
        # Expected failure during an ISP outage or dish reboot
        log_event("Warning: Starlink gRPC timeout.")
        return None
    # ... other error handling ...
```

This interaction is used to:

1.  **Initial State:** Record the baseline version to `starlink_version.json`.
2.  **Detection:** Periodically compare the polled version against the stored version. A change triggers the crucial `Starlink Firmware Update Detected: OLD -> NEW` log entry, which is the anchor point for the reporting script.

-----

## 2\. `reportdnsrelay.py`: Log Parsing and Correlation Logic

The reporting script is an analytical tool built entirely around the log file structure. The robustness of the report depends on accurate log pattern matching.

### Log Pattern Matching (Regex)

Three primary regular expressions are used to extract all required data points from the `monitordnsrelay.log`:

| Data Point | Regex Pattern (Simplified) | Purpose |
| :--- | :--- | :--- |
| **Outage Start** | `(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) - Internet outage detected` | Captures the exact timestamp of the failure. |
| **Outage End** | `(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) - Outage ended\. Duration: ([\d\.]+) seconds` | Captures the end timestamp and the computed duration. |
| **Firmware Update** | `(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) - Starlink Firmware Update Detected: (.+?) -> (.+?)` | Captures the update timestamp, the old version string, and the new version string. |

### Firmware Correlation Window

The `parse_outages_from_log()` function performs the classification. It establishes a correlation by iterating through the list of completed outages and the list of firmware update detections.

An outage is classified as **Firmware (Maintenance)** if the time difference between the outage start and the update detection time is less than or equal to `FIRMWARE_OUTAGE_WINDOW_MINUTES` (default 30 minutes).

```python
# Snippet from reportdnsrelay.py (concept)
FIRMWARE_OUTAGE_WINDOW_MINUTES = 30 
# ...
time_diff = abs((outage_start - update_time).total_seconds()) / 60
            
if time_diff <= FIRMWARE_OUTAGE_WINDOW_MINUTES:
    outage["firmware_update"] = True
    # ... classification logic ...
```

**Trade-off/Edge Case:** This logic accounts for two scenarios:

1.  The dish reboots *just before* the monitoring service can poll the new version.
2.  The monitoring service polls the new version *just before* the final connection drop associated with the update reboot cycle.

By checking the absolute time difference, we treat the detection time as the center of the maintenance event, encompassing the potential full reboot window.

### Log Pruning (Simple Log Rotation)

The `prune_log_file()` function provides a form of log rotation to manage file size:

1.  It determines a cutoff date (e.g., 60 days ago).
2.  It creates a dated backup of the **entire current log file** *before* any data is removed.
3.  It rewrites the original log file with only entries that are newer than the cutoff.

This design prioritizes **data safety** (by creating a backup of the original content) and **simplicity** over more complex tools like `logrotate`, which simplifies the deployment process for the user.

-----

The final pieces you need are the `requirements.txt`, the `.env.example`, and the sample `systemd` service file.

## Final Utility Files

Here are the remaining files to complete your repository.

### 1\. `requirements.txt`

```
requests
python-dotenv
starlink-grpc-core
psutil
```

### 2\. `.env.example`

```env
# Rename this file to .env and fill in your details
# ==============================================================================
# EMAIL CONFIGURATION - Requires a Gmail App Password for SENDER_PASSWORD
# ==============================================================================
SENDER_EMAIL=your_monitoring_email@gmail.com
SENDER_PASSWORD=your_gmail_app_password
RECIPIENT_EMAIL=your_report_recipient@example.com

# ==============================================================================
# FILE PATH CONFIGURATION (Optional - defaults to 'data/' if not set)
# ==============================================================================
# LOG_FILE_PATH=/path/to/monitordnsrelay.log
# STARLINK_VERSION_FILE=/path/to/starlink_version.json
# STARLINK_STATUS_URL=192.168.100.1:9200
```

### 3\. `dns-monitor.service` (Sample Systemd Unit File)

This is a template file that allows the user to easily set up the script as a robust system service. **Paths must be adjusted by the user.**

```ini
[Unit]
Description=Starlink Enhanced DNS and Outage Monitoring Service
# Ensure the service starts after network and local filesystems are ready
Requires=network-online.target
After=network-online.target

[Service]
# IMPORTANT: Change 'sysadmin_user' to the user account that will run the service
User=sysadmin_user
# Set Group if necessary
# Group=sysadmin_group

# IMPORTANT: Set this to the absolute path of your project directory
WorkingDirectory=/opt/starlink-monitor

# Type set to simple since the script runs its own loop
Type=simple

# IMPORTANT: Change the path to point to the python executable within your venv
# and the absolute path of the monitoring script
ExecStart=/opt/starlink-monitor/venv/bin/python /opt/starlink-monitor/monitordnsrelay_service.py

# Standard operational settings
Restart=always
RestartSec=10
TimeoutStopSec=5
StandardOutput=journal
StandardError=journal

[Install]
# Enable the service for multi-user targets
WantedBy=multi-user.target
```

