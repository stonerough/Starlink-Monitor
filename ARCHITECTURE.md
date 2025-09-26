# ARCHITECTURE

The Starlink Enhanced Monitoring system utilizes a classic two-tier architecture: a persistent **Monitoring Service** (`monitordnsrelay_service.py`) that handles real-time data collection and state management, and a scheduled **Reporting Tool** (`reportdnsrelay.py`) that performs analytics, log maintenance, and notification.

---

## System Components and Data Flow

The system relies on three primary components and two persistence files.

### 1. The Monitoring Service (`monitordnsrelay_service.py`)

This is the long-running, daemonized component.

| Function | Detail | Frequency |
| :--- | :--- | :--- |
| **DNS Polling** | Pings four public DNS servers (`1.1.1.1`, `8.8.8.8`, etc.). An outage is declared only if **all** fail. | **60 seconds** (Normal) / **5 seconds** (Outage) |
| **Starlink Status Check** | Connects to the local Starlink dish via gRPC (`192.168.100.1:9200`) to retrieve the current `software_version`. | **6 hours** (Normal) |
| **State Management** | Logs state transitions (Outage Start, Outage End, Firmware Update Detection) to the log file. | Event-driven |
| **Alerting** | Sends an immediate email notification upon **Outage End** with the total duration. | Event-driven |

### 2. The Reporting Tool (`reportdnsrelay.py`)

This component is designed to run periodically (e.g., daily via `cron`). It is stateless and operates exclusively on the log and version files.

| Function | Detail | Frequency |
| :--- | :--- | :--- |
| **Log Parsing** | Reads the full log file, pairing `Outage detected` and `Outage ended` events. | Daily |
| **Firmware Correlation** | Cross-references logged outages with `Firmware Update Detected` events, classifying outages within a **30-minute window** of an update as 'Maintenance'. | Daily |
| **Analytics** | Calculates 30-day uptime percentage and totals downtime categorized by 'Connectivity' vs. 'Firmware'. | Daily |
| **Notification** | Sends a comprehensive daily uptime report email summary. | Daily |
| **Log Pruning** | Rotates the log file, keeping only the last **60 days** of entries to prevent uncontrolled disk usage. | Daily |

---

## Data Persistence & Storage

The system uses a dedicated `./data` directory (or user-defined path via `.env`) for all state and log data.

### 1. `monitordnsrelay.log`

This is the primary data source. Each line represents a time-stamped event critical for reporting:
* `YYYY-MM-DD HH:MM:SS - Internet outage detected...`
* `YYYY-MM-DD HH:MM:SS - Outage ended. Duration: X.XX seconds...`
* `YYYY-MM-DD HH:MM:SS - Starlink Firmware Update Detected: OLD -> NEW`

### 2. `starlink_version.json`

A simple JSON file storing the last successfully retrieved Starlink software version. This allows the Monitoring Service to detect a change on subsequent polls, which is a stronger indicator of a *completed* firmware update than simply checking status.

---

## System Flow Diagram

The diagram illustrates the asynchronous nature of the monitoring and reporting processes.

```mermaid
graph TD
    subgraph MONITORING_TIER
        A[monitordnsrelay_service.py]
        A -->|Writes State/Events| B(monitordnsrelay.log)
        A -->|Writes Current Version| C(starlink_version.json)
        A -->|Outage End Event| D(Immediate Email Notification)
    end

    subgraph REPORTING_TIER
        E[reportdnsrelay.py - CRON]
        E -->|Reads Log & Correlates| B
        E -->|Reads Current Version| C
        E -->|Writes Pruned Log| B
        E -->|Daily Summary| F(Email Report: Uptime, Classification)
    end

    B ---|Pruning Check| E


**Key Takeaway:** The system is designed for high separation of concerns. The Monitoring Service focuses solely on **event detection** and **low-latency alerting**, while the Reporting Tool handles **expensive analytics** and **log housekeeping** on a non-critical schedule.
