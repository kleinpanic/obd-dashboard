# OBD Commander

Car computer backend for OBD-II vehicles. WebSocket server + SQLite logging + powerful CLI.

## Quick Start

```bash
# Setup
cd ~/codeWS/Python/obd-dashboard
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Start server
./obdc server start

# Open http://localhost:8000
```

## CLI Commands

### OBD Sensors
```bash
./obdc status              # Connection and vehicle info
./obdc scan                # List all sensors with values
./obdc get RPM             # Get single sensor value
./obdc get SPEED           # Current speed
./obdc live                # Stream live data (JSON lines)
./obdc vin                 # Get VIN
./obdc dtc                 # Diagnostic trouble codes
./obdc capabilities        # What can be controlled
```

### Server Control
```bash
./obdc server start [port]     # Start web server (default: 8000)
./obdc server stop             # Stop server
./obdc server status           # Check if running
./obdc server restart [port]   # Restart with optional port
```

### Database
```bash
./obdc db stats                # Database statistics
./obdc db export               # Export all data as JSON
./obdc db query "SELECT * FROM sensor_data LIMIT 10"
./obdc db clear                # Clear all data
```

### Logs
```bash
./obdc log tail 50             # Last 50 log lines
./obdc log follow              # Follow live logs
```

## Architecture

```
┌─────────────────────────────────────────┐
│        FastAPI + WebSocket Server       │
│         (uvicorn, ~50MB RAM)            │
├─────────────────────────────────────────┤
│  CLI (obdc) - Full system control       │
│  - Sensor queries                       │
│  - Server management                    │
│  - Database operations                  │
├─────────────────────────────────────────┤
│  SQLite (~/.local/share/obdc/obdc.db)   │
│  - Time-series sensor data              │
│  - 2Hz logging for key sensors          │
└─────────────────────────────────────────┘
```

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Dashboard UI (mobile-first) |
| `GET /api/status` | Connection status |
| `GET /api/sensors` | All sensor readings |
| `GET /api/history/{sensor}?hours=24` | Historical data |
| `GET /api/dtc` | Diagnostic trouble codes |
| `WS /ws` | Real-time WebSocket updates |

## WebSocket Protocol

```json
{
  "type": "sensor_update",
  "timestamp": 1742062800.0,
  "data": {
    "RPM": {"value": 2500, "unit": "revolutions_per_minute"},
    "SPEED": {"value": 100, "unit": "kilometer_per_hour"}
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

# Systemd service (auto-start on boot)
mkdir -p ~/.config/systemd/user
cp obdc.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now obdc

# Access at http://localhost:8000
```

## Offline Operation

No WiFi required. The server runs locally:
- All data stored in SQLite
- WebSocket works over localhost
- PWA can be installed for offline use

## Requirements

- Python 3.8+
- ELM327 OBD adapter (USB recommended)
- See `requirements.txt`

## Tested Vehicles

- 2021 Subaru Crosstrek (ISO 15765-4 CAN 11/500)
- Any OBD-II compliant vehicle (1996+)

## License

MIT
