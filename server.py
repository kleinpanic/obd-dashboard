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
from datetime import datetime, timezone
from pathlib import Path
import threading
from contextlib import asynccontextmanager

try:
    import obd
    OBD_AVAILABLE = True
except ImportError:
    OBD_AVAILABLE = False

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn

# Config
DATA_DIR = Path.home() / ".local/share/obdc"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "obdc.db"

# Database setup
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS sensor_data (
            timestamp REAL,
            sensor TEXT,
            value REAL,
            unit TEXT,
            PRIMARY KEY (timestamp, sensor)
        )
    ''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_sensor ON sensor_data(sensor)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_timestamp ON sensor_data(timestamp)')
    conn.commit()
    conn.close()

init_db()

def log_sensor(sensor: str, value: float, unit: str):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO sensor_data (timestamp, sensor, value, unit) VALUES (?, ?, ?, ?)",
            (time.time(), sensor, value, unit)
        )
        conn.commit()
        conn.close()
    except:
        pass

def get_recent_data(sensor: str, minutes: int = 30):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    cutoff = time.time() - (minutes * 60)
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
        self.supported = []
        self.lock = threading.Lock()
        self.last_data = {}  # Cache last good readings
        
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
                self.last_data = {}
                return True
        except Exception as e:
            print(f"OBD connect error: {e}")
        return False
    
    def disconnect(self):
        if self.connection:
            try:
                self.connection.close()
            except:
                pass
            self.connected = False
    
    def is_healthy(self):
        """Check if connection is still good"""
        if not self.connection or not self.connected:
            return False
        try:
            # Quick ping - try to read status
            return self.connection.is_connected()
        except:
            return False
    
    def read_key_sensors(self):
        if not self.connected:
            return {}
        
        if not self.is_healthy():
            print("OBD connection lost, attempting reconnect...")
            self.disconnect()
            if not self.connect():
                return {}
        
        key_pids = ['RPM', 'SPEED', 'INTAKE_TEMP', 'THROTTLE_POS', 
                    'FUEL_LEVEL', 'ENGINE_LOAD', 'MAF', 'TIMING_ADVANCE',
                    'CONTROL_MODULE_VOLTAGE', 'BAROMETRIC_PRESSURE', 'AMBIANT_AIR_TEMP',
                    'OIL_TEMP', 'FUEL_RATE', 'COMMANDED_EQUIV_RATIO']
        
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
                                self.last_data[cmd.name] = data[cmd.name]
                    except Exception as e:
                        pass
        
        # Merge with last known good data for stability
        for k, v in self.last_data.items():
            if k not in data:
                data[k] = v
        
        return data
    
    def read_all(self):
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
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections[:]:
            try:
                await connection.send_json(message)
            except:
                self.disconnect(connection)

ws_manager = ConnectionManager()

# Background OBD reader
async def obd_reader():
    consecutive_failures = 0
    while True:
        if obd_manager.connected and ws_manager.active_connections:
            try:
                data = obd_manager.read_key_sensors()
                if data:
                    await ws_manager.broadcast({
                        "type": "sensor_update",
                        "timestamp": time.time(),
                        "data": data
                    })
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    if consecutive_failures > 5:
                        print("Too many failures, reconnecting...")
                        obd_manager.disconnect()
                        await asyncio.sleep(1)
                        obd_manager.connect()
                        consecutive_failures = 0
            except Exception as e:
                print(f"OBD reader error: {e}")
                consecutive_failures += 1
        elif not obd_manager.connected and ws_manager.active_connections:
            # Try to reconnect if we have clients but no OBD
            print("Attempting OBD reconnect...")
            obd_manager.connect()
        
        await asyncio.sleep(0.25)  # 4Hz for smooth gauges

# Lifespan context manager
@asynccontextmanager
async def lifespan(app):
    # Startup
    asyncio.create_task(obd_reader())
    yield
    # Shutdown
    obd_manager.disconnect()

app = FastAPI(title="OBD Commander", lifespan=lifespan)

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
async def history(sensor: str, minutes: int = 30):
    rows = get_recent_data(sensor, minutes)
    return [{"t": r[0], "v": r[1]} for r in rows]

@app.get("/api/dtc")
async def dtc():
    return {"dtc": obd_manager.get_dtc()}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)

# Dashboard HTML with gauges
DASHBOARD_HTML = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
    <meta name="theme-color" content="#000000">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <title>OBD Commander</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        :root {
            --bg: #000;
            --card: #0a0a0a;
            --border: #1a1a1a;
            --text: #fff;
            --muted: #555;
            --accent: #22c55e;
            --warning: #f59e0b;
            --danger: #ef4444;
            --blue: #3b82f6;
        }
        html, body { 
            height: 100%; 
            font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', system-ui, sans-serif;
            background: var(--bg);
            color: var(--text);
            overflow: hidden;
            -webkit-font-smoothing: antialiased;
        }
        
        .app {
            display: flex;
            flex-direction: column;
            height: 100%;
        }
        
        /* Header */
        header {
            padding: 8px 16px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border);
            flex-shrink: 0;
        }
        
        .logo {
            font-weight: 700;
            font-size: 14px;
            letter-spacing: -0.5px;
        }
        
        .status {
            display: flex;
            align-items: center;
            gap: 6px;
            font-size: 12px;
            color: var(--muted);
        }
        
        .status-dot {
            width: 6px;
            height: 6px;
            border-radius: 50%;
            background: var(--danger);
        }
        
        .status-dot.connected { background: var(--accent); }
        
        /* Pages */
        .page-container {
            flex: 1;
            overflow: hidden;
            position: relative;
        }
        
        .page {
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            display: none;
            overflow-y: auto;
            -webkit-overflow-scrolling: touch;
        }
        
        .page.active { display: block; }
        
        /* Dashboard Page */
        .dashboard {
            display: flex;
            flex-direction: column;
            padding: 16px;
            gap: 16px;
        }
        
        /* Main Gauges */
        .main-gauges {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
        }
        
        .gauge-container {
            aspect-ratio: 1;
            position: relative;
            display: flex;
            align-items: center;
            justify-content: center;
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 16px;
        }
        
        .gauge-svg {
            position: absolute;
            width: 100%;
            height: 100%;
        }
        
        .gauge-center {
            position: relative;
            text-align: center;
            z-index: 1;
        }
        
        .gauge-value {
            font-size: 48px;
            font-weight: 200;
            font-variant-numeric: tabular-nums;
            line-height: 1;
        }
        
        .gauge-unit {
            font-size: 12px;
            color: var(--muted);
            margin-top: 2px;
        }
        
        .gauge-label {
            position: absolute;
            bottom: 12px;
            left: 0;
            right: 0;
            text-align: center;
            font-size: 11px;
            color: var(--muted);
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        
        /* Stats Grid */
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 8px;
        }
        
        .stat {
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 12px 8px;
            text-align: center;
        }
        
        .stat-value {
            font-size: 20px;
            font-weight: 500;
            font-variant-numeric: tabular-nums;
            color: var(--accent);
        }
        
        .stat-label {
            font-size: 10px;
            color: var(--muted);
            margin-top: 4px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        
        /* Secondary Stats */
        .history-stats {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 8px;
            margin-top: 12px;
            padding-top: 12px;
            border-top: 1px solid var(--border);
        }
        
        .history-stat {
            text-align: center;
        }
        
        .history-stat-value {
            font-size: 16px;
            font-weight: 500;
            font-variant-numeric: tabular-nums;
            color: var(--accent);
        }
        
        .history-stat-label {
            font-size: 10px;
            color: var(--muted);
            text-transform: uppercase;
        }
        
        .secondary-stats {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 8px;
        }
        
        .stat-large {
            padding: 16px;
        }
        
        .stat-large .stat-value {
            font-size: 28px;
        }
        
        /* Sensors Page */
        .sensors-page {
            padding: 16px;
        }
        
        .section-title {
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 1px;
            color: var(--muted);
            margin-bottom: 12px;
            padding: 0 4px;
        }
        
        .sensors-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
            gap: 8px;
        }
        
        .sensor-card {
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 12px;
        }
        
        .sensor-name {
            font-size: 10px;
            color: var(--muted);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 4px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        
        .sensor-value {
            font-size: 18px;
            font-weight: 500;
            font-variant-numeric: tabular-nums;
            color: var(--text);
        }
        
        .sensor-unit {
            font-size: 10px;
            color: var(--muted);
            margin-left: 2px;
        }
        
        /* History Page */
        .history-page {
            padding: 16px;
        }
        
        .chart-container {
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 16px;
            margin-bottom: 16px;
        }
        
        .chart-title {
            font-size: 12px;
            color: var(--muted);
            margin-bottom: 12px;
        }
        
        .chart {
            height: 120px;
            position: relative;
        }
        
        .chart canvas {
            width: 100%;
            height: 100%;
        }
        
        /* Nav */
        nav {
            display: flex;
            border-top: 1px solid var(--border);
            flex-shrink: 0;
        }
        
        nav button {
            flex: 1;
            background: none;
            border: none;
            color: var(--muted);
            padding: 12px 8px;
            font-size: 10px;
            text-transform: uppercase;
            letter-spacing: 1px;
            cursor: pointer;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 4px;
        }
        
        nav button.active {
            color: var(--accent);
        }
        
        nav button svg {
            width: 20px;
            height: 20px;
        }
        
        /* Warnings */
        .warning { color: var(--warning); }
        .danger { color: var(--danger); }
    </style>
</head>
<body>
    <div class="app">
        <header>
            <div class="logo">OBD Commander</div>
            <div class="status">
                <div class="status-dot" id="status-dot"></div>
                <span id="status-text">Connecting</span>
            </div>
        </header>
        
        <div class="page-container">
            <!-- Dashboard Page -->
            <div class="page active" id="page-dashboard">
                <div class="dashboard">
                    <div class="main-gauges">
                        <div class="gauge-container">
                            <svg class="gauge-svg" viewBox="0 0 200 200" id="rpm-gauge"></svg>
                            <div class="gauge-center">
                                <div class="gauge-value" id="rpm-value">0</div>
                                <div class="gauge-unit">RPM</div>
                            </div>
                            <div class="gauge-label">Engine Speed</div>
                        </div>
                        <div class="gauge-container">
                            <svg class="gauge-svg" viewBox="0 0 200 200" id="speed-gauge"></svg>
                            <div class="gauge-center">
                                <div class="gauge-value" id="speed-value">0</div>
                                <div class="gauge-unit">km/h</div>
                            </div>
                            <div class="gauge-label">Speed</div>
                        </div>
                    </div>
                    
                    <div class="stats-grid">
                        <div class="stat">
                            <div class="stat-value" id="intake-value">--</div>
                            <div class="stat-label">Intake °C</div>
                        </div>
                        <div class="stat">
                            <div class="stat-value" id="load-value">--</div>
                            <div class="stat-label">Load %</div>
                        </div>
                        <div class="stat">
                            <div class="stat-value" id="throttle-value">--</div>
                            <div class="stat-label">Throttle %</div>
                        </div>
                        <div class="stat">
                            <div class="stat-value" id="timing-value">--</div>
                            <div class="stat-label">Timing °</div>
                        </div>
                    </div>
                    
                    <div class="secondary-stats">
                        <div class="stat stat-large">
                            <div class="stat-value" id="fuel-value">--</div>
                            <div class="stat-label">Fuel %</div>
                        </div>
                        <div class="stat stat-large">
                            <div class="stat-value" id="maf-value">--</div>
                            <div class="stat-label">MAF g/s</div>
                        </div>
                        <div class="stat stat-large">
                            <div class="stat-value" id="voltage-value">--</div>
                            <div class="stat-label">Voltage V</div>
                        </div>
                        <div class="stat stat-large">
                            <div class="stat-value" id="oil-value">--</div>
                            <div class="stat-label">Oil °C</div>
                        </div>
                    </div>
                </div>
            </div>
            
            <!-- Sensors Page -->
            <div class="page" id="page-sensors">
                <div class="sensors-page">
                    <div class="section-title">All Sensors</div>
                    <div class="sensors-grid" id="sensors-grid"></div>
                </div>
            </div>
            
            <!-- History Page -->
            <div class="page" id="page-history">
                <div class="history-page">
                    <div class="chart-container">
                        <div class="chart-title">Speed (last 5 min)</div>
                        <div class="chart"><canvas id="speed-chart"></canvas></div>
                        <div class="history-stats" id="speed-stats"></div>
                    </div>
                    <div class="chart-container">
                        <div class="chart-title">RPM (last 5 min)</div>
                        <div class="chart"><canvas id="rpm-chart"></canvas></div>
                        <div class="history-stats" id="rpm-stats"></div>
                    </div>
                    <div class="chart-container">
                        <div class="chart-title">Engine Load (last 5 min)</div>
                        <div class="chart"><canvas id="load-chart"></canvas></div>
                        <div class="history-stats" id="load-stats"></div>
                    </div>
                    <div class="chart-container">
                        <div class="chart-title">Fuel Level (last 5 min)</div>
                        <div class="chart"><canvas id="fuel-chart"></canvas></div>
                        <div class="history-stats" id="fuel-stats"></div>
                    </div>
                </div>
            </div>
        </div>
        
        <nav>
            <button class="active" onclick="showPage('dashboard')">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <circle cx="12" cy="12" r="10"/>
                    <path d="M12 6v6l4 2"/>
                </svg>
                Dashboard
            </button>
            <button onclick="showPage('sensors')">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <rect x="3" y="3" width="7" height="7"/>
                    <rect x="14" y="3" width="7" height="7"/>
                    <rect x="14" y="14" width="7" height="7"/>
                    <rect x="3" y="14" width="7" height="7"/>
                </svg>
                Sensors
            </button>
            <button onclick="showPage('history')">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M3 3v18h18"/>
                    <path d="M18 9l-5 5-4-4-3 3"/>
                </svg>
                History
            </button>
        </nav>
    </div>
    
    <script>
        const data = {};
        let ws;
        
        // Gauge drawing with SVG
        function drawGauge(svgId, value, max, color = '#22c55e') {
            const svg = document.getElementById(svgId);
            if (!svg) return;
            
            const percent = Math.min(value / max, 1);
            const startAngle = -135;
            const endAngle = startAngle + (270 * percent);
            
            // Calculate arc path
            const cx = 100, cy = 100, r = 80;
            const start = polarToCartesian(cx, cy, r, endAngle);
            const end = polarToCartesian(cx, cy, r, startAngle);
            const largeArc = percent > 0.5 ? 1 : 0;
            
            const path = `M ${start.x} ${start.y} A ${r} ${r} 0 ${largeArc} 0 ${end.x} ${end.y}`;
            
            svg.innerHTML = `
                <path d="M ${polarToCartesian(cx,cy,r,-135).x} ${polarToCartesian(cx,cy,r,-135).y} A ${r} ${r} 0 1 0 ${polarToCartesian(cx,cy,r,135).x} ${polarToCartesian(cx,cy,r,135).y}" 
                      fill="none" stroke="#1a1a1a" stroke-width="12" stroke-linecap="round"/>
                <path d="${path}" fill="none" stroke="${color}" stroke-width="12" stroke-linecap="round"/>
            `;
        }
        
        function polarToCartesian(cx, cy, r, angle) {
            const rad = (angle - 90) * Math.PI / 180;
            return { x: cx + r * Math.cos(rad), y: cy + r * Math.sin(rad) };
        }
        
        // Chart drawing
        function drawChart(canvasId, points, color = '#22c55e') {
            const canvas = document.getElementById(canvasId);
            if (!canvas || points.length < 2) {
                if (canvas) {
                    const ctx = canvas.getContext('2d');
                    const rect = canvas.getBoundingClientRect();
                    canvas.width = rect.width * 2;
                    canvas.height = rect.height * 2;
                    ctx.scale(2, 2);
                    ctx.fillStyle = '#666';
                    ctx.font = '12px system-ui';
                    ctx.fillText('No data', 10, rect.height / 2);
                }
                return;
            }
            
            try {
                const ctx = canvas.getContext('2d');
                const rect = canvas.getBoundingClientRect();
                canvas.width = rect.width * 2;
                canvas.height = rect.height * 2;
                ctx.scale(2, 2);
                
                const w = rect.width;
                const h = rect.height;
                const padding = 8;
                
                const vals = points.map(p => p.v);
                const max = Math.max(...vals, 1);
                const min = Math.min(...vals, 0);
                const range = max - min || 1;
                
                ctx.beginPath();
                points.forEach((p, i) => {
                    const x = padding + (i / (points.length - 1)) * (w - padding * 2);
                    const y = h - padding - ((p.v - min) / range) * (h - padding * 2);
                    if (i === 0) ctx.moveTo(x, y);
                    else ctx.lineTo(x, y);
                });
                ctx.strokeStyle = color;
                ctx.lineWidth = 2;
                ctx.stroke();
                
                ctx.lineTo(w - padding, h - padding);
                ctx.lineTo(padding, h - padding);
                ctx.closePath();
                ctx.fillStyle = color + '20';
                ctx.fill();
            } catch(e) {
                console.error('Chart error:', e);
            }
        }
        
        // Update UI
        function updateUI() {
            // RPM
            const rpm = data.RPM?.value || 0;
            const rpmEl = document.getElementById('rpm-value');
            rpmEl.textContent = Math.round(rpm).toLocaleString();
            rpmEl.className = 'gauge-value' + (rpm > 6000 ? ' danger' : rpm > 5000 ? ' warning' : '');
            drawGauge(document.getElementById('rpm-gauge'), rpm, 8000, rpm > 6000 ? '#ef4444' : rpm > 5000 ? '#f59e0b' : '#22c55e');
            
            // Speed
            const speed = data.SPEED?.value || 0;
            document.getElementById('speed-value').textContent = Math.round(speed);
            drawGauge(document.getElementById('speed-gauge'), speed, 200, '#3b82f6');
            
            // Stats
            const format = (v, u) => v !== undefined ? `${Math.round(v)}${u}` : '--';
            
            document.getElementById('intake-value').textContent = format(data.INTAKE_TEMP?.value, '°');
            document.getElementById('load-value').textContent = format(data.ENGINE_LOAD?.value, '%');
            document.getElementById('throttle-value').textContent = format(data.THROTTLE_POS?.value, '%');
            document.getElementById('timing-value').textContent = format(data.TIMING_ADVANCE?.value, '°');
            
            const fuel = data.FUEL_LEVEL?.value;
            const fuelEl = document.getElementById('fuel-value');
            fuelEl.textContent = format(fuel, '%');
            fuelEl.className = 'stat-value' + (fuel < 15 ? ' danger' : fuel < 25 ? ' warning' : '');
            
            document.getElementById('maf-value').textContent = data.MAF?.value ? `${data.MAF.value.toFixed(1)}` : '--';
            document.getElementById('voltage-value').textContent = data.CONTROL_MODULE_VOLTAGE?.value ? `${data.CONTROL_MODULE_VOLTAGE.value.toFixed(1)}` : '--';
            document.getElementById('oil-value').textContent = data.OIL_TEMP?.value ? `${Math.round(data.OIL_TEMP.value)}°` : '--';
            
            // Sensors list
            const grid = document.getElementById('sensors-grid');
            grid.innerHTML = Object.entries(data)
                .filter(([k]) => !['RPM', 'SPEED'].includes(k))
                .map(([name, val]) => `
                    <div class="sensor-card">
                        <div class="sensor-name">${name.replace(/_/g, ' ')}</div>
                        <div class="sensor-value">${val.value.toFixed(1)}<span class="sensor-unit">${val.unit}</span></div>
                    </div>
                `).join('');
        }
        
        // Page navigation
        function showPage(page) {
            document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
            document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
            document.getElementById(`page-${page}`).classList.add('active');
            event.target.closest('button').classList.add('active');
            
            if (page === 'history') {
                fetchHistory();
            }
        }
        
        function renderHistoryStats(containerId, points, unit = '') {
            if (!points || points.length < 2) return '';
            
            const values = points.map(p => p.v);
            const min = Math.min(...values);
            const max = Math.max(...values);
            const avg = values.reduce((a, b) => a + b, 0) / values.length;
            const current = values[values.length - 1];
            
            const container = document.getElementById(containerId);
            if (container) {
                container.innerHTML = `
                    <div class="history-stat">
                        <div class="history-stat-value">${current.toFixed(1)}</div>
                        <div class="history-stat-label">Current${unit}</div>
                    </div>
                    <div class="history-stat">
                        <div class="history-stat-value">${avg.toFixed(1)}</div>
                        <div class="history-stat-label">Avg${unit}</div>
                    </div>
                    <div class="history-stat">
                        <div class="history-stat-value">${min.toFixed(1)}</div>
                        <div class="history-stat-label">Min${unit}</div>
                    </div>
                    <div class="history-stat">
                        <div class="history-stat-value">${max.toFixed(1)}</div>
                        <div class="history-stat-label">Max${unit}</div>
                    </div>
                `;
            }
        }
        
        async function fetchHistory() {
            try {
                const [speed, rpm, load, fuel] = await Promise.all([
                    fetch('/api/history/SPEED?minutes=5').then(r => r.json()),
                    fetch('/api/history/RPM?minutes=5').then(r => r.json()),
                    fetch('/api/history/ENGINE_LOAD?minutes=5').then(r => r.json()),
                    fetch('/api/history/FUEL_LEVEL?minutes=5').then(r => r.json()),
                ]);
                
                drawChart('speed-chart', speed, '#3b82f6');
                drawChart('rpm-chart', rpm, '#22c55e');
                drawChart('load-chart', load, '#f59e0b');
                drawChart('fuel-chart', fuel, '#22c55e');
                
                renderHistoryStats('speed-stats', speed, ' km/h');
                renderHistoryStats('rpm-stats', rpm, ' RPM');
                renderHistoryStats('load-stats', load, '%');
                renderHistoryStats('fuel-stats', fuel, '%');
            } catch (e) {
                console.error('Failed to fetch history', e);
            }
        }
        
        // WebSocket connection
        function connect() {
            const wsUrl = `ws://${location.host}/ws`;
            console.log('Connecting to', wsUrl);
            
            try {
                ws = new WebSocket(wsUrl);
                
                ws.onopen = () => {
                    console.log('WebSocket connected');
                    document.getElementById('status-dot').classList.add('connected');
                    document.getElementById('status-text').textContent = 'Connected';
                };
                
                ws.onclose = () => {
                    console.log('WebSocket disconnected');
                    document.getElementById('status-dot').classList.remove('connected');
                    document.getElementById('status-text').textContent = 'Reconnecting...';
                    setTimeout(connect, 2000);
                };
                
                ws.onerror = (e) => {
                    console.error('WebSocket error:', e);
                };
                
                ws.onmessage = (event) => {
                    const msg = JSON.parse(event.data);
                    if (msg.type === 'sensor_update') {
                        Object.assign(data, msg.data);
                        updateUI();
                    }
                };
            } catch(e) {
                console.error('WebSocket init error:', e);
                setTimeout(connect, 2000);
            }
        }
        
        // Initial fetch
        fetch('/api/sensors')
            .then(r => r.json())
            .then(d => { Object.assign(data, d); updateUI(); })
            .catch(console.error);
        
        connect();
    </script>
</body>
</html>
'''

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
            print("✗ No OBD connection")
    else:
        print("✗ OBD library not installed")
    
    print(f"\nDashboard: http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop\n")
    
    uvicorn.run(app, host=args.host, port=args.port, log_level="error")
