# Architecture & Platform Support

## Overview

OBD Commander is written in **pure Python** with no native code dependencies. This means it runs on any platform supported by Python 3.9+.

## Supported Platforms

| Architecture | OS | Notes |
|-------------|-----|-------|
| x86_64 | Linux (Debian, Ubuntu, Fedora, Arch) | ✅ Primary development |
| x86_64 | macOS (Intel) | ✅ Supported |
| aarch64 | Raspberry Pi 4/5 (64-bit OS) | ✅ Tested on RPi4 |
| aarch64 | macOS (Apple Silicon) | ✅ Supported |
| armv7l | Raspberry Pi 3/Zero 2 (32-bit OS) | ✅ Supported |

## Raspberry Pi 4/5 Setup

For in-car use, the Raspberry Pi 4 or 5 is the recommended platform.

### Hardware Requirements

- Raspberry Pi 4 (2GB+ RAM) or Raspberry Pi 5
- 16GB+ microSD card (or SSD via USB3)
- ELM327 USB OBD-II adapter
- 5V/3A USB-C power supply (or car power adapter)
- Optional: 7" touchscreen or phone hotspot for dashboard access

### Software Setup

```bash
# Raspberry Pi OS Lite (64-bit) recommended
sudo apt update
sudo apt install python3 python3-pip python3-venv libusb-1.0-0 udev

# Clone and install
git clone https://github.com/kleinpanic/obd-dashboard.git
cd obd-dashboard
make install-deps
make install-user

# Add to dialout group
sudo usermod -aG dialout $USER
# Reboot required for group change
sudo reboot

# After reboot
source ~/.bashrc  # or re-login
obdc server start
```

### Auto-Start on Boot (systemd)

```bash
sudo make install-service
sudo systemctl enable --now obdc
```

### Access from Phone

1. Connect RPi4 to your phone's WiFi hotspot
2. Find RPi4 IP address: `hostname -I`
3. Open `http://<RPi4-IP>:9000` on phone browser

## Memory Footprint

The server is optimized for low-resource environments:

- **Idle:** ~40MB RAM
- **Active (4Hz streaming):** ~60MB RAM
- **With history charts:** ~80MB RAM

This fits comfortably on a 1GB RPi model.

## Offline Capability

**OBD Commander requires zero network connectivity.**

All features work offline:
- SQLite database for logging
- Embedded VIN decode table (no NHTSA API calls)
- No external JavaScript libraries or CDNs
- WebSocket runs on localhost
- DTC lookup uses local definitions

If you want NHTSA VIN decode (for unknown VINs), it would need network access, but this is optional and gracefully degraded.

## Data Storage

| File | Purpose | Typical Size |
|------|---------|--------------|
| `~/.local/share/obdc/obdc.db` | SQLite database | 10-100 MB/week |
| `~/.local/share/obdc/obdc.log` | Structured logs | 1-5 MB/day |
| `~/.local/share/obdc/config.json` | User settings | < 1 KB |

### Disk Space Management

```bash
# Check database size
obdc db stats

# Clean old data (keep last 30 days)
obdc db clean
```

## Threading Model

- **Main thread:** FastAPI/uvicorn async event loop
- **Background thread:** OBD polling at 4Hz
- **WebSocket:** Async broadcast to connected clients

The OBD polling is intentionally in a background thread to avoid blocking the async event loop with synchronous serial I/O.

## Dependencies

### Python Packages (requirements.txt)

| Package | Purpose |
|---------|---------|
| `obd` | ELM327 communication |
| `fastapi` | REST + WebSocket server |
| `uvicorn[standard]` | ASGI server |
| `websockets` | WebSocket protocol |
| `flask` | Legacy fallback (being removed) |
| `flask-cors` | Legacy fallback (being removed) |

### System Libraries

| Library | Purpose |
|---------|---------|
| `libusb-1.0` | USB serial communication |
| `python3-venv` | Virtual environment support |

## Performance Tuning

### Increase Sample Rate

Default is 4Hz. To increase:

1. Edit `~/.local/share/obdc/config.json`
2. Set `"refresh_rate": 8` (max recommended: 10)
3. Restart server

### Reduce CPU Usage

If running on a low-power device:

1. Lower `refresh_rate` to 2
2. Disable history charts in UI (config page)
3. Reduce `key_pids` list in `server.py`

## Security Considerations

- **No authentication:** Designed for local/trusted network only
- **Read-only OBD:** No write commands while vehicle is moving
- **No external APIs:** All data stays local
- **DTC clear:** Requires explicit confirmation

For remote access, use a VPN or SSH tunnel. Do not expose port 9000 directly to the internet.
