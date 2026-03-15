# OBD Dashboard

Real-time web dashboard for OBD-II vehicle data. Read-only, safe for driving.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install obd flask flask-cors
```

## Run

```bash
python3 dashboard.py
```

Then open http://localhost:5000 in your browser.

## Safety

This is **read-only**. No commands are sent to modify vehicle state.
Safe to use while driving - displays data only.

## Supported Vehicles

Works with any OBD-II compliant vehicle (1996+).
Tested on 2021 Subaru Crosstrek.

## Requirements

- OBD-II adapter (USB/Bluetooth)
- Linux/macOS/Windows
- Python 3.8+
