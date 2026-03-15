# OBD Commander

Car computer backend for OBD-II vehicles. WebSocket server + SQLite logging + REST API. Optimized for RPi4 car computer setup.

## Features

- **WebSocket server**: Real-time push updates (no polling)
- **SQLite logging**: Historical data storage
- **Mobile-first UI**: Dark theme, PWA installable
- **Offline-capable**: Works without WiFi
- **Small footprint**: Designed for RPi4
- **Read-only**: Safe for driving

## Quick Start

```bash
# Setup
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Run
python server.py

# Open http://localhost:8000 on your phone
```

## Architecture

```
┌─────────────────────────────────────────┐
│           FastAPI + WebSocket           │
│    (uvicorn, ~50MB RAM, single proc)    │
├─────────────────────────────────────────┤
│  OBD Reader (background async task)     │
│  - Reads key sensors at 2Hz             │
│  - Logs all sensors to SQLite           │
│  - Broadcasts via WebSocket             │
├─────────────────────────────────────────┤
│  SQLite (~/.local/share/obdc/obdc.db)   │
│  - Time-series sensor data              │
│  - Auto-managed, small footprint        │
└─────────────────────────────────────────┘
```

## API

| Endpoint | Description |
|----------|-------------|
| `GET /` | Dashboard UI |
| `GET /api/status` | Connection status |
| `GET /api/sensors` | All sensor readings |
| `GET /api/history/{sensor}` | Historical data |
| `GET /api/dtc` | Diagnostic trouble codes |
| `WS /ws` | Real-time sensor updates |

## WebSocket Protocol

Connect to `ws://host:8000/ws` and receive JSON messages:

```json
{
  "type": "sensor_update",
  "timestamp": 1742062800.0,
  "data": {
    "RPM": {"value": 2500, "unit": "revolutions_per_minute"},
    "SPEED": {"value": 100, "unit": "kilometer_per_hour"},
    ...
  }
}
```

## RPi4 Car Computer Setup

```bash
# Install
cd ~/codeWS/Python/obd-dashboard
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Systemd service (auto-start)
cp obdc.service ~/.config/systemd/user/
systemctl --user enable --now obdc

# Access at http://localhost:8000
```

## Database

All sensor data logged to `~/.local/share/obdc/obdc.db`

Query recent history:
```sql
SELECT datetime(timestamp, 'unixepoch'), sensor, value 
FROM sensor_data 
WHERE sensor = 'RPM' 
ORDER BY timestamp DESC LIMIT 100;
```

## History API

```bash
# Get last 24h of RPM data
curl http://localhost:8000/api/history/RPM?hours=24

# Returns: [{"t": timestamp, "v": value}, ...]
```

## Hardware

- Any ELM327-compatible OBD adapter
- USB recommended (most reliable)
- Bluetooth/WiFi also work

## Requirements

- Python 3.8+
- FastAPI, uvicorn, websockets
- obd (python-obd library)

## Tested On

- 2021 Subaru Crosstrek (ISO 15765-4 CAN 11/500)
- Any OBD-II vehicle (1996+)

## License

MIT
