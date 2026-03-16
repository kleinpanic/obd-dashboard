# Installation Guide — OBD Commander

## Requirements

- Python 3.9+ (Python 3.11+ recommended)
- ELM327 USB OBD-II adapter
- Linux, macOS, or WSL2 on Windows
- User in `dialout` group (Linux) for USB access

## Supported Architectures

| Architecture | Platform | Status |
|-------------|----------|--------|
| x86_64 | amd64 desktop/server | ✅ Primary |
| aarch64 | Raspberry Pi 4/5 (64-bit), Apple Silicon | ✅ Tested |
| armv7l | Raspberry Pi 3/Zero 2 (32-bit) | ✅ Supported |

## Quick Install

```bash
# Clone
git clone https://github.com/kleinpanic/obd-dashboard.git
cd obd-dashboard

# Install system dependencies (Linux)
make install-deps

# Add yourself to dialout group (required for USB OBD)
sudo usermod -aG dialout $USER
# Log out and back in, or: newgrp dialout

# Setup Python environment
make venv
source venv/bin/activate

# Run
./obdc server start

# Open http://localhost:9000
```

## System Dependencies

### Debian/Ubuntu/Raspberry Pi OS

```bash
sudo apt update
sudo apt install python3 python3-pip python3-venv python3-dev \
                 libusb-1.0-0 libusb-1.0-0-dev udev build-essential
sudo usermod -aG dialout $USER
```

### Fedora/RHEL

```bash
sudo dnf install python3 python3-pip python3-venv python3-devel \
                 libusb1 libusb1-devel udev
sudo usermod -aG dialout $USER
```

### Arch Linux

```bash
sudo pacman -S python python-pip python-virtualenv libusb udev base-devel
sudo usermod -aG dialout $USER
```

### macOS

```bash
brew install python3 libusb
```

## User-Local Install (No sudo)

```bash
make install-user
# Adds ~/.local/bin/obdc to your PATH
obdc server start
```

## System-Wide Install

```bash
sudo make install
# Installs to /usr/local/bin/obdc
obdc server start
```

## systemd Service (Auto-start on Boot)

For Raspberry Pi or always-on setups:

```bash
sudo make install-service
sudo systemctl enable --now obdc

# Check status
sudo systemctl status obdc

# View logs
journalctl -u obdc -f
```

## Offline Operation

OBD Commander works **fully offline**. No network connection is required.

- All data stored in local SQLite (`~/.local/share/obdc/obdc.db`)
- No external APIs, CDNs, or cloud services
- VIN decoding uses embedded lookup table
- Web dashboard has no external JavaScript dependencies

## Verify Installation

```bash
# Check CLI
./obdc --version
./obdc --help

# Run tests
make test

# Security audit
make audit
```

## Troubleshooting

### USB Permission Denied

```
Permission denied: '/dev/ttyUSB0'
```

**Fix:** Add user to dialout group and re-login:
```bash
sudo usermod -aG dialout $USER
# Log out and back in
```

### No USB Device Found

```bash
# List USB serial devices
ls /dev/ttyUSB*

# Check dmesg for USB errors
dmesg | grep -i usb | tail -20
```

### Python Version

```bash
python3 --version  # Need 3.9+
```

### Virtual Environment Issues

```bash
# Recreate venv
rm -rf venv
make venv
source venv/bin/activate
```
