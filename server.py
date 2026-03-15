#!/usr/bin/env python3
"""
OBD Commander - Car computer backend
WebSocket server + SQLite logging + REST API
Optimized for RPi4, offline-capable
"""

import asyncio
import json
import sqlite3
import time
import os
from datetime import datetime
from pathlib import Path
import threading

try:
    import obd
    OBD_AVAILABLE = True
except ImportError:
    OBD_AVAILABLE = False

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# Config
DATA_DIR = Path.home() / ".local/share/obdc"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "obdc.db"

app = FastAPI(title="OBD Commander")

# Database setup
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS sensor_data (
            timestamp REAL PRIMARY KEY,
            sensor TEXT,
            value REAL,
            unit TEXT
        )
    ''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_sensor ON sensor_data(sensor)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_timestamp ON sensor_data(timestamp)')
    conn.commit()
    conn.close()

init_db()

def log_sensor(sensor: str, value: float, unit: str):
    """Log sensor reading to SQLite"""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO sensor_data (timestamp, sensor, value, unit) VALUES (?, ?, ?, ?)",
            (time.time(), sensor, value, unit)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"DB error: {e}")

def get_recent_data(sensor: str, hours: int = 24):
    """Get recent sensor data for graphs"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    cutoff = time.time() - (hours * 3600)
    c.execute(
        "SELECT timestamp, value FROM sensor_data WHERE sensor = ? AND timestamp > ? ORDER BY timestamp",
        (sensor, cutoff)
    )
    rows = c.fetchall()
    conn.close()
    return rows

# OBD Connection
class OBDManager:
    def __init__(self):
        self.connection = None
        self.connected = False
        self.sensors = {}
        self.supported = []
        self.lock = threading.Lock()
        
    def connect(self):
        if not OBD_AVAILABLE:
            return False
        ports = obd.scan_serial()
        if not ports:
            return False
        try:
            self.connection = obd.OBD(ports[0], protocol="6", fast=False)
            if self.connection.is_connected():
                self.connected = True
                self.supported = list(self.connection.supported_commands)
                return True
        except:
            pass
        return False
    
    def disconnect(self):
        if self.connection:
            self.connection.close()
            self.connected = False
    
    def read_all(self):
        """Read all supported sensors"""
        if not self.connected:
            return {}
        
        data = {}
        with self.lock:
            for cmd in self.supported:
                try:
                    resp = self.connection.query(cmd)
                    if not resp.is_null():
                        val = resp.value
                        value = float(val.magnitude) if hasattr(val, 'magnitude') and isinstance(val.magnitude, (int, float)) else None
                        if value is not None:
                            unit = str(val.units) if hasattr(val, 'units') else ""
                            data[cmd.name] = {"value": value, "unit": unit}
                            log_sensor(cmd.name, value, unit)
                except:
                    pass
        return data
    
    def read_key_sensors(self):
        """Read just the key sensors for real-time updates"""
        if not self.connected:
            return {}
        
        key_pids = ['RPM', 'SPEED', 'COOLANT_TEMP', 'INTAKE_TEMP', 'THROTTLE_POS', 
                    'FUEL_LEVEL', 'ENGINE_LOAD', 'MAF', 'TIMING_ADVANCE']
        
        data = {}
        with self.lock:
            for cmd in self.supported:
                if cmd.name in key_pids:
                    try:
                        resp = self.connection.query(cmd)
                        if not resp.is_null():
                            val = resp.value
                            value = float(val.magnitude) if hasattr(val, 'magnitude') and isinstance(val.magnitude, (int, float)) else None
                            if value is not None:
                                unit = str(val.units) if hasattr(val, 'units') else ""
                                data[cmd.name] = {"value": value, "unit": unit}
                                log_sensor(cmd.name, value, unit)
                    except:
                        pass
        return data
    
    def get_vin(self):
        if not self.connected:
            return None
        try:
            resp = self.connection.query(obd.commands.VIN)
            if not resp.is_null():
                return str(resp.value)
        except:
            pass
        return None
    
    def get_dtc(self):
        if not self.connected:
            return []
        try:
            resp = self.connection.query(obd.commands.GET_DTC)
            if not resp.is_null():
                return [(code, desc) for code, desc in resp.value]
        except:
            pass
        return []

obd_manager = OBDManager()

# WebSocket manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except:
                pass

ws_manager = ConnectionManager()

# Background OBD reader
async def obd_reader():
    """Continuously read OBD data and broadcast to WebSocket clients"""
    while True:
        if obd_manager.connected and ws_manager.active_connections:
            data = obd_manager.read_key_sensors()
            if data:
                await ws_manager.broadcast({
                    "type": "sensor_update",
                    "timestamp": time.time(),
                    "data": data
                })
        await asyncio.sleep(0.5)  # 2Hz update rate

# Routes
@app.get("/")
async def root():
    return HTMLResponse(content=DASHBOARD_HTML)

@app.get("/api/status")
async def status():
    return {
        "connected": obd_manager.connected,
        "sensors_supported": len(obd_manager.supported),
        "vin": obd_manager.get_vin()
    }

@app.get("/api/sensors")
async def sensors():
    return obd_manager.read_all()

@app.get("/api/history/{sensor}")
async def history(sensor: str, hours: int = 24):
    rows = get_recent_data(sensor, hours)
    return [{"t": r[0], "v": r[1]} for r in rows]

@app.get("/api/dtc")
async def dtc():
    return {"dtc": obd_manager.get_dtc()}

@app.get("/manifest.json")
async def manifest():
    return {
        "name": "OBD Commander",
        "short_name": "OBDC",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0a0a0a",
        "theme_color": "#10b981",
        "icons": [{"src": "/icon.png", "sizes": "192x192", "type": "image/png"}]
    }

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        while True:
            # Just keep connection alive, data is pushed from reader
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)

# Dashboard HTML
DASHBOARD_HTML = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
    <meta name="theme-color" content="#0a0a0a">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <title>OBD Commander</title>
    <link rel="manifest" href="/manifest.json">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        :root {
            --bg: #050505;
            --card: #0d0d0d;
            --border: #1a1a1a;
            --text: #e5e5e5;
            --muted: #666;
            --accent: #10b981;
            --warning: #f59e0b;
            --danger: #ef4444;
        }
        html, body { 
            height: 100%; 
            font-family: -apple-system, BlinkMacSystemFont, 'SF Pro', system-ui, sans-serif;
            background: var(--bg);
            color: var(--text);
            overflow: hidden;
        }
        
        .app {
            display: grid;
            grid-template-rows: auto 1fr auto;
            height: 100%;
            max-width: 800px;
            margin: 0 auto;
        }
        
        header {
            padding: 12px 16px;
            border-bottom: 1px solid var(--border);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .status {
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 13px;
        }
        
        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: var(--danger);
        }
        
        .status-dot.connected { background: var(--accent); }
        
        main {
            overflow-y: auto;
            padding: 12px;
            -webkit-overflow-scrolling: touch;
        }
        
        /* Gauges - Primary Display */
        .gauges {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 10px;
            margin-bottom: 12px;
        }
        
        .gauge {
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 16px;
            text-align: center;
            position: relative;
            overflow: hidden;
        }
        
        .gauge.large {
            grid-column: span 2;
            display: flex;
            justify-content: space-around;
            align-items: center;
        }
        
        .gauge-value {
            font-size: 42px;
            font-weight: 200;
            font-variant-numeric: tabular-nums;
            line-height: 1;
            color: var(--accent);
        }
        
        .gauge.large .gauge-value { font-size: 56px; }
        
        .gauge-unit {
            font-size: 12px;
            color: var(--muted);
            margin-top: 4px;
        }
        
        .gauge-label {
            font-size: 11px;
            color: var(--muted);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-top: 8px;
        }
        
        .gauge.warning .gauge-value { color: var(--warning); }
        .gauge.danger .gauge-value { color: var(--danger); }
        
        /* Sensors Grid */
        .sensors-title {
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: var(--muted);
            margin: 16px 0 8px;
            padding: 0 4px;
        }
        
        .sensors {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
            gap: 6px;
        }
        
        .sensor {
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 10px 12px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 13px;
        }
        
        .sensor-name {
            color: var(--muted);
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            flex: 1;
        }
        
        .sensor-value {
            color: var(--accent);
            font-weight: 500;
            font-variant-numeric: tabular-nums;
        }
        
        /* Bottom Nav */
        nav {
            border-top: 1px solid var(--border);
            display: flex;
            justify-content: space-around;
            padding: 8px 0;
            background: var(--card);
        }
        
        nav button {
            background: none;
            border: none;
            color: var(--muted);
            padding: 8px 16px;
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            cursor: pointer;
        }
        
        nav button.active { color: var(--accent); }
        
        /* Offline indicator */
        .offline {
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            background: var(--warning);
            color: #000;
            padding: 4px;
            text-align: center;
            font-size: 12px;
            font-weight: 500;
        }
        
        @media (min-width: 600px) {
            .gauges { grid-template-columns: repeat(3, 1fr); }
            .gauge.large { grid-column: span 3; }
        }
    </style>
</head>
<body>
    <div class="app">
        <header>
            <div style="font-weight: 600; font-size: 15px;">OBD Commander</div>
            <div class="status">
                <div class="status-dot" id="status-dot"></div>
                <span id="status-text">Connecting...</span>
            </div>
        </header>
        
        <main id="main">
            <div class="gauges" id="gauges"></div>
            <div class="sensors-title">All Sensors</div>
            <div class="sensors" id="sensors"></div>
        </main>
        
        <nav>
            <button class="active">Dashboard</button>
            <button onclick="fetch('/api/sensors')">Refresh</button>
        </nav>
    </div>
    
    <script>
        const ws = new WebSocket(`ws://${location.host}/ws`);
        const gauges = ['RPM', 'SPEED', 'COOLANT_TEMP', 'FUEL_LEVEL', 'ENGINE_LOAD', 'INTAKE_TEMP'];
        const allData = {};
        
        ws.onopen = () => {
            document.getElementById('status-dot').classList.add('connected');
            document.getElementById('status-text').textContent = 'Connected';
        };
        
        ws.onclose = () => {
            document.getElementById('status-dot').classList.remove('connected');
            document.getElementById('status-text').textContent = 'Disconnected';
        };
        
        ws.onmessage = (event) => {
            const msg = JSON.parse(event.data);
            if (msg.type === 'sensor_update') {
                Object.assign(allData, msg.data);
                render();
            }
        };
        
        function render() {
            // Gauges
            const gaugesEl = document.getElementById('gauges');
            gaugesEl.innerHTML = `
                <div class="gauge large">
                    <div>
                        <div class="gauge-value">${(allData.RPM?.value || 0).toFixed(0)}</div>
                        <div class="gauge-unit">RPM</div>
                    </div>
                    <div>
                        <div class="gauge-value">${(allData.SPEED?.value || 0).toFixed(0)}</div>
                        <div class="gauge-unit">km/h</div>
                    </div>
                </div>
            `;
            
            // Other priority gauges
            for (const name of gauges.slice(2)) {
                const val = allData[name];
                if (val) {
                    const warning = (name === 'COOLANT_TEMP' && val.value > 100) || 
                                   (name === 'FUEL_LEVEL' && val.value < 15);
                    const danger = (name === 'COOLANT_TEMP' && val.value > 110);
                    gaugesEl.innerHTML += `
                        <div class="gauge ${danger ? 'danger' : warning ? 'warning' : ''}">
                            <div class="gauge-value">${val.value.toFixed(0)}</div>
                            <div class="gauge-unit">${val.unit}</div>
                            <div class="gauge-label">${name.replace(/_/g, ' ')}</div>
                        </div>
                    `;
                }
            }
            
            // All sensors
            const sensorsEl = document.getElementById('sensors');
            sensorsEl.innerHTML = Object.entries(allData)
                .filter(([k]) => !gauges.includes(k))
                .map(([name, val]) => `
                    <div class="sensor">
                        <span class="sensor-name">${name.replace(/_/g, ' ')}</span>
                        <span class="sensor-value">${val.value.toFixed(1)}</span>
                    </div>
                `).join('');
        }
        
        // Initial fetch
        fetch('/api/sensors').then(r => r.json()).then(data => {
            Object.assign(allData, data);
            render();
        });
    </script>
</body>
</html>
'''

@app.on_event("startup")
async def startup_event():
    """Start OBD reader on startup"""
    asyncio.create_task(obd_reader())


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="OBD Commander Car Computer")
    parser.add_argument("--port", "-p", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    
    print("OBD Commander - Car Computer")
    print("=" * 40)
    
    if OBD_AVAILABLE:
        print("Connecting to OBD...")
        if obd_manager.connect():
            print(f"✓ Connected! {len(obd_manager.supported)} sensors")
        else:
            print("✗ No OBD connection (running in demo mode)")
    else:
        print("✗ OBD library not installed")
    
    print(f"\nDashboard: http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop\n")
    
    uvicorn.run(app, host=args.host, port=args.port, log_level="error")
