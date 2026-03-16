# OBD Commander Roadmap

## Current Interfaces

| Interface | Status | Description |
|-----------|--------|-------------|
| **CLI** | ✅ Complete | Full command-line interface |
| **WebUI** | ✅ Complete | Mobile-first web dashboard (WebSocket) |
| **MCP** | ✅ Complete | AI assistant integration |

## Planned Interfaces

### TUI (Terminal User Interface)

**Goal:** Full-screen terminal dashboard using `rich` or `textual`

```
┌─────────────────────────────────────────────────┐
│ OBD Commander                    Connected ●    │
├─────────────────────────────────────────────────┤
│                                                 │
│    ╭──────────╮        ╭──────────╮            │
│    │   RPM    │        │  SPEED   │            │
│    │   3218   │        │   78     │            │
│    │  ██████ │        │  ████   │            │
│    ╰──────────╯        ╰──────────╯            │
│                                                 │
│ [F1] Dashboard  [F2] Sensors  [F3] History    │
│ [Q] Quit        [R] Refresh   [D] DTC         │
└─────────────────────────────────────────────────┘
```

**Tech stack options:**
- `textual` (Python, async, modern) — **recommended**
- `rich` (Python, simpler but limited interaction)
- `urwid` (Python, older but battle-tested)

**MVP features:**
- Real-time gauges (RPM, Speed)
- Sensor list with live values
- DTC viewer
- Keyboard navigation

**Implementation:**
```bash
# Dependencies
pip install textual rich

# Run
./obdc tui
```

**Status:** 🔴 Not started

---

### GUI (Desktop Application)

**Goal:** Native desktop app for Linux/macOS/Windows

**Tech stack options:**

| Framework | Pros | Cons |
|-----------|------|------|
| **PyQt6 / PySide6** | Native look, full featured | Large dependency, licensing (PyQt) |
| **Tkinter** | Built-in, simple | Ugly, limited widgets |
| **Kivy** | Cross-platform, good for touch | Non-native look |
| **Electron + Python backend** | Web tech, easy UI | Heavy, JavaScript |
| **PyWebView** | Reuse WebUI, native wrapper | Easiest path, lighter than Electron |

**Recommended approach:** `pywebview` wrapper around existing WebUI

```bash
# Dependencies
pip install pywebview

# Run
./obdc gui
```

**This reuses the entire existing WebUI stack with a native window.**

**Status:** 🔴 Not started

---

## Daemon Mode Enhancements

### Auto-detect Car State

**Goal:** Automatically start/stop based on car power state

```
[Car Power On] ──→ OBD device appears ──→ Start server
[Car Power Off] ─→ OBD device lost ──→ Stop server (after grace period)
```

**Implementation approaches:**

1. **udev rule** (Linux)
   ```bash
   # /etc/udev/rules.d/99-obd.rules
   ACTION=="add", SUBSYSTEM=="usb", ATTR{idVendor}=="0403", RUN+="/usr/local/bin/obdc-udev start"
   ACTION=="remove", SUBSYSTEM=="usb", ATTR{idVendor}=="0403", RUN+="/usr/local/bin/obdc-udev stop"
   ```

2. **Polling daemon** (simpler, cross-platform)
   ```python
   # Background process that:
   # - Checks /dev/ttyUSB* every 5s
   # - Starts server when device appears
   # - Stops server when device disappears
   ```

3. **systemd path unit**
   ```ini
   [Path]
   PathExists=/dev/ttyUSB0
   Unit=obdc.service
   ```

**Status:** 🟡 Planned for RPi4 deployment

---

## Future Features

### Data Export
- [ ] Export to CSV/JSON
- [ ] Import from previous sessions
- [ ] Cloud backup (optional)

### Analytics
- [ ] Fuel economy calculations
- [ ] Trip cost estimation
- [ ] Performance metrics (0-60, quarter mile)

### Multi-vehicle
- [ ] Switch between profiles
- [ ] Compare sessions across vehicles
- [ ] Fleet management mode

### Notifications
- [ ] DTC alerts via ntfy/push
- [ ] Maintenance reminders
- [ ] Threshold alerts (temp, RPM)

### Integrations
- [ ] Home Assistant MQTT
- [ ] Grafana dashboard
- [ ] InfluxDB logging (optional)

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) (when created)

## Version History

| Version | Date | Changes |
|--------|------|---------|
| 1.0.0 | 2026-03-15 | Initial release with CLI, WebUI, MCP |
