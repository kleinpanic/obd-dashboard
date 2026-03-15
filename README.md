# OBD Commander

CLI and web interface for OBD-II vehicles. Agent-friendly (JSON output) + human dashboard.

## Features

- **Vehicle-agnostic**: Works with any OBD-II vehicle (1996+)
- **CLI-first**: JSON output for agents and scripts
- **Web dashboard**: Mobile-friendly real-time UI
- **Read-only by default**: Safe for driving
- **VIN decoding**: Get vehicle identification
- **DTC scanning**: Read diagnostic trouble codes
- **Capabilities analysis**: See what sensors are available

## Install

```bash
python3 -m venv venv
source venv/bin/activate
pip install obd flask flask-cors
```

## CLI Usage

```bash
# Connection status and vehicle info
./obdc status

# Get VIN
./obdc vin

# Scan all supported sensors
./obdc scan

# Get single sensor
./obdc get RPM
./obdc get SPEED

# Stream live data (JSON lines - for agents)
./obdc live

# Get diagnostic trouble codes
./obdc dtc

# Show capabilities (what can be read/controlled)
./obdc capabilities

# Start web dashboard
./obdc web --port 5000
```

## Live Stream (for Agents)

```bash
./obdc live --interval 1
```

Output (JSON lines):
```json
{"timestamp": "2026-03-15T19:24:38Z", "sensors": {"RPM": {"value": 2952.0, "unit": "revolutions_per_minute"}, "SPEED": {"value": 113.0, "unit": "kilometer_per_hour"}, ...}}
```

Perfect for piping to jq, logging, or agent ingestion.

## Web Dashboard

```bash
./obdc web
```

Then open http://localhost:5000 on your phone/computer.

## Capabilities Analysis

```bash
./obdc capabilities
```

Shows:
- Protocol (CAN, ISO, etc.)
- Total supported sensors
- Read-only sensors
- Potentially writable commands (use with caution)

## Safety

**This tool is read-only by default.** OBD-II standard is primarily for diagnostics.

Commands that could affect the vehicle (like `CLEAR_DTC`) are flagged in capabilities output but require explicit implementation to execute.

## Tested Vehicles

- 2021 Subaru Crosstrek (ISO 15765-4 CAN 11/500)
- Should work with any OBD-II compliant vehicle (1996+)

## Hardware

Works with any ELM327-compatible adapter:
- USB adapters (recommended)
- Bluetooth adapters
- WiFi adapters

## Requirements

- Python 3.8+
- ELM327 OBD-II adapter
- Linux/macOS/Windows
