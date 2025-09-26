# Configuration Guide

This document details all parameters used to configure the Starlink Enhanced Monitoring system. Configuration is primarily handled through a central **`.env`** file for environment variables and directly within the Python service file for core constants.

---

## 1. Environment Variables (`.env` File)

Create a file named `.env` in the project root to override the default settings. These variables are loaded by both `monitordnsrelay_service.py` and `reportdnsrelay.py`.

| Variable | Default Value | Script | Description |
| :--- | :--- | :--- | :--- |
| **`SENDER_EMAIL`** | *None* | Both | The Gmail account used to send notifications and reports. **Requires a Gmail App Password.** |
| **`SENDER_PASSWORD`** | *None* | Both | The **Gmail App Password** generated for the `SENDER_EMAIL`. (Not your account password). |
| **`RECIPIENT_EMAIL`** | *None* | Both | The email address where immediate outage alerts and daily reports are sent. |
| **`LOG_FILE_PATH`** | `./data/monitordnsrelay.log` | Both | The absolute or relative path to the primary monitoring log file. Must be writable by the service user. |
| **`STARLINK_VERSION_FILE`** | `./data/starlink_version.json` | Both | The path to the JSON file used to store the last known Starlink software version. Must be writable. |
| **`STARLINK_STATUS_URL`** | `192.168.100.1:9200` | Monitor | The IP address and gRPC port of the Starlink dish/router. Modify only if your dishy is on a non-standard subnet or port. |

---

## 2. Core Service Constants (`monitordnsrelay_service.py`)

These values are set as constants at the top of the monitoring script. Changes require restarting the `systemd` service.

### Monitoring Intervals

| Constant | Production Value | Unit | Trade-offs of Modification |
| :--- | :--- | :--- | :--- |
| **`NORMAL_PING_INTERVAL`** | `60` | seconds | **Decrease:** Provides quicker detection of new outages, but increases system load (disk I/O for logging) and network traffic. |
| **`REDUCED_PING_INTERVAL`** | `5` | seconds | **Decrease:** Reduces the time to detect *recovery* from an outage, minimizing the reported downtime. Increasing it saves resources but artificially inflates outage duration. |
| **`STARLINK_CHECK_INTERVAL_HOURS`**| `6` | hours | **Decrease:** More frequent checks mean faster detection of a *completed* firmware update, improving correlation accuracy, but slightly increases network traffic to the dish. |

### Ping and Outage Definition

| Constant | Value | Unit | Notes |
| :--- | :--- | :--- | :--- |
| **`DNS_SERVERS`** | `["1.1.1.1", "8.8.8.8", ...]` | List | The list of DNS servers used for connectivity checks. An outage is declared **only if all servers fail**. It is recommended to use at least two geographically diverse, reliable public DNS servers. |
| **`PING_TIMEOUT`** | `3` | seconds | Timeout for the individual DNS check (ping/TCP query). Increasing this prevents false positives during transient high latency, but delays true outage detection. |
| **`STATUS_LOG_INTERVAL`** | `25` | checks | The number of monitoring checks between periodic 'System operational' logs. Decreasing this provides more detailed status logs but increases log file size and disk I/O. |

---

## 3. Reporting and Analytics Constants (`reportdnsrelay.py`)

These constants define the boundaries for the report and the log rotation policy.

| Constant | Value | Unit | Description and Trade-offs |
| :--- | :--- | :--- | :--- |
| **`DAYS_TO_ANALYZE`** | `30` | days | The look-back period for the report summary (e.g., 30-day uptime). Increasing this makes the report more resource-intensive but provides a longer trend history. |
| **`DAYS_TO_KEEP`** | `60` | days | The log pruning cutoff date. All log entries older than this limit are backed up and removed. Increasing this consumes more disk space but maintains more granular historical data. |
| **`FIRMWARE_OUTAGE_WINDOW_MINUTES`** | `30` | minutes | The maximum time window (before or after a logged `Firmware Update Detected` event) an outage can fall into to be classified as 'Maintenance'. Decreasing this improves correlation specificity but might miss valid maintenance outages due to clock drift or reporting lag. |

### Note on Time-Based Log Pruning

The log pruning function in `reportdnsrelay.py` ensures the log file does not grow indefinitely. It automatically creates a dated backup before rewriting the main log file, providing a robust, simple form of log rotation. You may wish to set up a separate process to archive or clean up these dated backup files.