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
import uuid
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
CONFIG_PATH = DATA_DIR / "config.json"

# Default config
DEFAULT_CONFIG = {
    "theme": "dark",
    "units": "metric",
    "refresh_rate": 4
}

def load_config():
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except:
            pass
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))

config = load_config()

# Database setup
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Sessions table
    c.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            start_time REAL,
            end_time REAL,
            distance_km REAL DEFAULT 0,
            max_rpm INTEGER DEFAULT 0,
            max_speed INTEGER DEFAULT 0,
            avg_fuel REAL DEFAULT 0
        )
    ''')
    
    # Sensor data table (drop and recreate if schema changed)
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sensor_data'")
    if c.fetchone():
        c.execute("PRAGMA table_info(sensor_data)")
        columns = [col[1] for col in c.fetchall()]
        if 'session_id' not in columns:
            c.execute("DROP TABLE sensor_data")
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS sensor_data (
            timestamp REAL,
            session_id TEXT,
            sensor TEXT,
            value REAL,
            unit TEXT,
            PRIMARY KEY (timestamp, sensor)
        )
    ''')
    
    c.execute('CREATE INDEX IF NOT EXISTS idx_sensor ON sensor_data(sensor)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_session ON sensor_data(session_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_timestamp ON sensor_data(timestamp)')
    
    # DTC table
    c.execute('''
        CREATE TABLE IF NOT EXISTS dtc_history (
            timestamp REAL PRIMARY KEY,
            code TEXT,
            description TEXT,
            cleared INTEGER DEFAULT 0
        )
    ''')
    
    conn.commit()
    conn.close()

init_db()

current_session_id = str(uuid.uuid4())
session_start_time = None

def start_session():
    global current_session_id, session_start_time
    current_session_id = str(uuid.uuid4())
    session_start_time = time.time()
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO sessions (id, start_time) VALUES (?, ?)", (current_session_id, session_start_time))
    conn.commit()
    conn.close()

def end_session():
    global session_start_time
    if session_start_time:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("UPDATE sessions SET end_time = ? WHERE id = ?", (time.time(), current_session_id))
        conn.commit()
        conn.close()
        session_start_time = None

def log_sensor(sensor: str, value: float, unit: str):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO sensor_data (timestamp, session_id, sensor, value, unit) VALUES (?, ?, ?, ?, ?)",
            (time.time(), current_session_id, sensor, value, unit)
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

def get_sessions(limit: int = 10):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT id, start_time, end_time, distance_km, max_rpm, max_speed, avg_fuel 
        FROM sessions 
        WHERE end_time IS NOT NULL 
        ORDER BY start_time DESC 
        LIMIT ?
    ''', (limit,))
    rows = c.fetchall()
    conn.close()
    return rows

# OBD Connection
class OBDManager:
    def __init__(self):
        self.connection = None
        self.connected = False
        self.connecting = False
        self.supported = []
        self.lock = threading.Lock()
        self.last_data = {}
        self.dtc_codes = []
        
    def connect(self):
        if not OBD_AVAILABLE:
            return False
        
        self.connecting = True
        ports = obd.scan_serial()
        
        if not ports:
            self.connecting = False
            return False
        try:
            self.connection = obd.OBD(ports[0], protocol="6", fast=False)
            if self.connection.is_connected():
                self.connected = True
                self.connecting = False
                self.supported = list(self.connection.supported_commands)
                self.last_data = {}
                start_session()
                return True
        except Exception as e:
            print(f"OBD connect error: {e}")
        self.connecting = False
        return False
    
    def disconnect(self):
        if self.connection:
            try:
                self.connection.close()
            except:
                pass
            self.connected = False
            end_session()
    
    def is_healthy(self):
        if not self.connection or not self.connected:
            return False
        try:
            return self.connection.is_connected()
        except:
            return False
    
    def read_key_sensors(self):
        if not self.connected:
            return {}
        
        if not self.is_healthy():
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
                self.dtc_codes = [(code, desc) for code, desc in resp.value]
                return self.dtc_codes
        except:
            pass
        return []
    
    def clear_dtc(self):
        if not self.connected:
            return False
        try:
            resp = self.connection.query(obd.commands.CLEAR_DTC)
            return True
        except:
            return False

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
    connection_step = 0
    
    while True:
        if obd_manager.connected and ws_manager.active_connections:
            try:
                data = obd_manager.read_key_sensors()
                if data:
                    await ws_manager.broadcast({
                        "type": "sensor_update",
                        "timestamp": time.time(),
                        "data": data,
                        "session_id": current_session_id
                    })
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    if consecutive_failures > 5:
                        await ws_manager.broadcast({
                            "type": "connection_progress",
                            "step": 1,
                            "message": "Connection lost, reconnecting...",
                            "status": "warning"
                        })
                        obd_manager.disconnect()
                        await asyncio.sleep(1)
                        connection_step = 0
                        consecutive_failures = 0
            except Exception as e:
                print(f"OBD reader error: {e}")
                consecutive_failures += 1
                
        elif obd_manager.connecting and ws_manager.active_connections:
            # Already connecting, wait
            pass
            
        elif not obd_manager.connected and ws_manager.active_connections:
            # Start connection with progress updates
            connection_step += 1
            
            if connection_step == 1:
                await ws_manager.broadcast({
                    "type": "connection_progress",
                    "step": 1,
                    "progress": 10,
                    "message": "Scanning for OBD adapter...",
                    "status": "info"
                })
                await asyncio.sleep(0.3)
                
            elif connection_step == 2:
                import obd as obd_module
                ports = obd_module.scan_serial()
                if ports:
                    await ws_manager.broadcast({
                        "type": "connection_progress",
                        "step": 2,
                        "progress": 30,
                        "message": f"Found adapter: {ports[0]}",
                        "status": "success"
                    })
                else:
                    await ws_manager.broadcast({
                        "type": "connection_progress",
                        "step": 2,
                        "progress": 20,
                        "message": "No adapter found, retrying...",
                        "status": "error"
                    })
                    connection_step = 0
                await asyncio.sleep(0.3)
                
            elif connection_step == 3:
                await ws_manager.broadcast({
                    "type": "connection_progress",
                    "step": 3,
                    "progress": 50,
                    "message": "Initializing ELM327...",
                    "status": "info"
                })
                await asyncio.sleep(0.3)
                
            elif connection_step == 4:
                await ws_manager.broadcast({
                    "type": "connection_progress",
                    "step": 4,
                    "progress": 70,
                    "message": "Querying vehicle protocols...",
                    "status": "info"
                })
                # Actually try to connect
                if obd_manager.connect():
                    await ws_manager.broadcast({
                        "type": "connection_progress",
                        "step": 5,
                        "progress": 90,
                        "message": f"Connected! {len(obd_manager.supported)} sensors found",
                        "status": "success"
                    })
                    await asyncio.sleep(0.3)
                    await ws_manager.broadcast({
                        "type": "connection_progress",
                        "step": 6,
                        "progress": 100,
                        "message": "Ready",
                        "status": "success"
                    })
                else:
                    await ws_manager.broadcast({
                        "type": "connection_progress",
                        "step": 4,
                        "progress": 40,
                        "message": "Connection failed, retrying...",
                        "status": "error"
                    })
                    connection_step = 0
                    obd_manager.connecting = False
                await asyncio.sleep(0.3)
            else:
                connection_step = 0
        
        await asyncio.sleep(0.25)

@asynccontextmanager
async def lifespan(app):
    asyncio.create_task(obd_reader())
    yield
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
        "connecting": obd_manager.connecting,
        "sensors_supported": len(obd_manager.supported),
        "vin": obd_manager.get_vin(),
        "session_id": current_session_id,
        "session_start": session_start_time
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

@app.post("/api/dtc/clear")
async def clear_dtc():
    if obd_manager.clear_dtc():
        return {"status": "cleared"}
    return {"status": "failed"}

@app.get("/api/sessions")
async def sessions():
    return {"sessions": get_sessions()}

@app.get("/api/config")
async def get_config():
    return config

@app.post("/api/config")
async def update_config(cfg: dict):
    global config
    config.update(cfg)
    save_config(config)
    return config

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        while True:
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
    <meta name="theme-color" content="#000000">
    <meta name="apple-mobile-web-app-capable" content="yes">
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
        
        [data-theme="light"] {
            --bg: #f5f5f5;
            --card: #fff;
            --border: #e0e0e0;
            --text: #000;
            --muted: #888;
        }
        
        html, body { 
            height: 100%; 
            font-family: -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
            background: var(--bg);
            color: var(--text);
            overflow: hidden;
        }
        
        .app {
            display: flex;
            flex-direction: column;
            height: 100%;
        }
        
        /* Connection overlay */
        .connection-overlay {
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: var(--bg);
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            z-index: 1000;
            padding: 20px;
        }
        
        .connection-overlay.hidden { display: none; }
        
        .spinner {
            width: 50px;
            height: 50px;
            border: 3px solid var(--border);
            border-top-color: var(--accent);
            border-radius: 50%;
            animation: spin 1s linear infinite;
            margin-bottom: 20px;
        }
        
        @keyframes spin { to { transform: rotate(360deg); } }
        
        .connection-text {
            font-size: 16px;
            color: var(--muted);
            margin-bottom: 20px;
        }
        
        .progress-bar {
            width: 200px;
            height: 4px;
            background: var(--border);
            border-radius: 2px;
            overflow: hidden;
            margin-bottom: 16px;
        }
        
        .progress-fill {
            height: 100%;
            background: var(--accent);
            width: 0%;
            transition: width 0.3s ease;
        }
        
        .connection-logs {
            width: 280px;
            max-height: 120px;
            overflow-y: auto;
            background: var(--card);
            border-radius: 8px;
            padding: 10px;
            font-family: 'SF Mono', 'Monaco', monospace;
            font-size: 10px;
        }
        
        .log-entry {
            color: var(--muted);
            padding: 2px 0;
            display: flex;
            gap: 6px;
        }
        
        .log-entry .time {
            color: #666;
        }
        
        .log-entry.success {
            color: var(--accent);
        }
        
        .log-entry.error {
            color: var(--danger);
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
        
        .logo { font-weight: 700; font-size: 14px; }
        
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
        }
        
        .page {
            position: absolute;
            top: 0; left: 0; right: 0; bottom: 0;
            display: none;
            overflow-y: auto;
        }
        
        .page.active { display: block; }
        
        /* Dashboard */
        .dashboard {
            padding: 16px;
            display: flex;
            flex-direction: column;
            gap: 16px;
        }
        
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
            overflow: hidden;
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
        
        .gauge-unit { font-size: 12px; color: var(--muted); margin-top: 2px; }
        .gauge-label {
            position: absolute;
            bottom: 12px; left: 0; right: 0;
            text-align: center;
            font-size: 11px;
            color: var(--muted);
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        
        /* Tooltips */
        [data-tooltip] {
            position: relative;
        }
        
        [data-tooltip]:hover::after {
            content: attr(data-tooltip);
            position: absolute;
            bottom: 100%;
            left: 50%;
            transform: translateX(-50%);
            background: var(--card);
            border: 1px solid var(--border);
            padding: 6px 10px;
            border-radius: 6px;
            font-size: 11px;
            white-space: nowrap;
            z-index: 100;
            color: var(--text);
        }
        
        .stat[data-tooltip]:hover::after {
            margin-bottom: 4px;
        }
        
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
        }
        
        .secondary-stats {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 8px;
        }
        
        .stat-large {
            padding: 16px;
        }
        
        .stat-large .stat-value { font-size: 28px; }
        
        /* Action buttons */
        .action-buttons {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 8px;
            margin-top: 8px;
        }
        
        .action-btn {
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 12px;
            color: var(--text);
            font-size: 13px;
            font-weight: 500;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
        }
        
        .action-btn:active { background: var(--border); }
        .action-btn.danger { border-color: var(--danger); color: var(--danger); }
        
        /* Sensors page */
        .sensors-page { padding: 16px; }
        
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
            cursor: pointer;
        }
        
        .sensor-card:active { background: var(--border); }
        
        .sensor-name {
            font-size: 10px;
            color: var(--muted);
            text-transform: uppercase;
            margin-bottom: 4px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        
        .sensor-value {
            font-size: 18px;
            font-weight: 500;
            font-variant-numeric: tabular-nums;
        }
        
        .sensor-unit { font-size: 10px; color: var(--muted); margin-left: 2px; }
        
        /* Sensor detail modal */
        .modal {
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0,0,0,0.8);
            display: none;
            align-items: center;
            justify-content: center;
            z-index: 100;
            padding: 16px;
        }
        
        .modal.active { display: flex; }
        
        .modal-content {
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 16px;
            width: 100%;
            max-width: 400px;
            max-height: 80vh;
            overflow-y: auto;
        }
        
        .modal-header {
            padding: 16px;
            border-bottom: 1px solid var(--border);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .modal-title { font-size: 16px; font-weight: 600; }
        .modal-close {
            background: none;
            border: none;
            color: var(--muted);
            font-size: 24px;
            cursor: pointer;
        }
        
        .modal-body { padding: 16px; }
        
        .modal-chart {
            height: 150px;
            background: var(--bg);
            border-radius: 8px;
            margin-bottom: 16px;
        }
        
        .modal-chart canvas { width: 100%; height: 100%; }
        
        .modal-stats {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 8px;
        }
        
        .modal-stat {
            background: var(--bg);
            border-radius: 8px;
            padding: 12px;
            text-align: center;
        }
        
        /* History page */
        .history-page { padding: 16px; }
        
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
        
        .chart { height: 120px; }
        .chart canvas { width: 100%; height: 100%; }
        
        .history-stats {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 8px;
            margin-top: 12px;
            padding-top: 12px;
            border-top: 1px solid var(--border);
        }
        
        .history-stat { text-align: center; }
        .history-stat-value { font-size: 16px; font-weight: 500; color: var(--accent); }
        .history-stat-label { font-size: 10px; color: var(--muted); text-transform: uppercase; }
        
        /* Config page */
        .config-page { padding: 16px; }
        
        .config-section {
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 12px;
            margin-bottom: 12px;
        }
        
        .config-item {
            padding: 16px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border);
        }
        
        .config-item:last-child { border-bottom: none; }
        
        .config-label { font-size: 14px; }
        .config-desc { font-size: 11px; color: var(--muted); margin-top: 2px; }
        
        .toggle {
            width: 50px;
            height: 28px;
            background: var(--border);
            border-radius: 14px;
            position: relative;
            cursor: pointer;
        }
        
        .toggle.active { background: var(--accent); }
        
        .toggle::after {
            content: '';
            position: absolute;
            width: 24px;
            height: 24px;
            background: var(--text);
            border-radius: 50%;
            top: 2px;
            left: 2px;
            transition: left 0.2s;
        }
        
        .toggle.active::after { left: 24px; }
        
        /* DTC modal */
        .dtc-list { margin: 16px 0; }
        
        .dtc-item {
            background: var(--bg);
            border-radius: 8px;
            padding: 12px;
            margin-bottom: 8px;
        }
        
        .dtc-code { font-weight: 600; color: var(--warning); }
        .dtc-desc { font-size: 12px; color: var(--muted); margin-top: 4px; }
        
        .no-dtc { text-align: center; color: var(--accent); padding: 20px; }
        
        /* Confirm modal */
        .confirm-text {
            text-align: center;
            padding: 20px;
            color: var(--muted);
        }
        
        .confirm-buttons {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px;
            padding: 16px;
        }
        
        .confirm-btn {
            padding: 12px;
            border-radius: 8px;
            border: none;
            font-weight: 500;
            cursor: pointer;
        }
        
        .confirm-btn.cancel {
            background: var(--border);
            color: var(--text);
        }
        
        .confirm-btn.confirm {
            background: var(--danger);
            color: #fff;
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
        
        nav button.active { color: var(--accent); }
        nav button svg { width: 20px; height: 20px; }
        
        .warning { color: var(--warning); }
        .danger { color: var(--danger); }
    </style>
</head>
<body data-theme="dark">
    <!-- Connection overlay -->
    <div class="connection-overlay" id="connection-overlay">
        <div class="spinner"></div>
        <div class="connection-text" id="connection-text">Initializing...</div>
        <div class="progress-bar">
            <div class="progress-fill" id="progress-fill"></div>
        </div>
        <div class="connection-logs" id="connection-logs"></div>
    </div>
    
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
                                <div class="gauge-unit" id="speed-unit">km/h</div>
                            </div>
                            <div class="gauge-label">Speed</div>
                        </div>
                    </div>
                    
                    <div class="stats-grid">
                        <div class="stat" data-tooltip="Air temperature entering the engine">
                            <div class="stat-value" id="intake-value">--</div>
                            <div class="stat-label" id="intake-label">Intake °C</div>
                        </div>
                        <div class="stat" data-tooltip="How hard the engine is working (0-100%)">
                            <div class="stat-value" id="load-value">--</div>
                            <div class="stat-label">Load %</div>
                        </div>
                        <div class="stat" data-tooltip="Throttle plate opening percentage">
                            <div class="stat-value" id="throttle-value">--</div>
                            <div class="stat-label">Throttle %</div>
                        </div>
                        <div class="stat" data-tooltip="Ignition timing advance/retard">
                            <div class="stat-value" id="timing-value">--</div>
                            <div class="stat-label">Timing °</div>
                        </div>
                    </div>
                    
                    <div class="secondary-stats">
                        <div class="stat stat-large" data-tooltip="Remaining fuel in tank">
                            <div class="stat-value" id="fuel-value">--</div>
                            <div class="stat-label">Fuel %</div>
                        </div>
                        <div class="stat stat-large" data-tooltip="Mass air flow rate into engine">
                            <div class="stat-value" id="maf-value">--</div>
                            <div class="stat-label">MAF g/s</div>
                        </div>
                        <div class="stat stat-large" data-tooltip="Vehicle electrical system voltage">
                            <div class="stat-value" id="voltage-value">--</div>
                            <div class="stat-label">Voltage V</div>
                        </div>
                        <div class="stat stat-large" data-tooltip="Engine oil temperature">
                            <div class="stat-value" id="oil-value">--</div>
                            <div class="stat-label" id="oil-label">Oil °C</div>
                        </div>
                    </div>
                    
                    <div class="action-buttons">
                        <button class="action-btn" onclick="showDTC()">
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <circle cx="12" cy="12" r="10"/>
                                <line x1="12" y1="8" x2="12" y2="12"/>
                                <line x1="12" y1="16" x2="12.01" y2="16"/>
                            </svg>
                            View Codes
                        </button>
                        <button class="action-btn danger" onclick="confirmClearDTC()">
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <polyline points="3 6 5 6 21 6"/>
                                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
                            </svg>
                            Clear Codes
                        </button>
                    </div>
                </div>
            </div>
            
            <!-- Sensors Page -->
            <div class="page" id="page-sensors">
                <div class="sensors-page">
                    <div class="section-title">All Sensors (tap for details)</div>
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
            
            <!-- Config Page -->
            <div class="page" id="page-config">
                <div class="config-page">
                    <div class="section-title">Settings</div>
                    
                    <div class="config-section">
                        <div class="config-item">
                            <div>
                                <div class="config-label">Dark Mode</div>
                                <div class="config-desc">Toggle dark/light theme</div>
                            </div>
                            <div class="toggle active" id="theme-toggle" onclick="toggleTheme()"></div>
                        </div>
                        <div class="config-item">
                            <div>
                                <div class="config-label">Metric Units</div>
                                <div class="config-desc">km/h, °C, L/100km</div>
                            </div>
                            <div class="toggle active" id="units-toggle" onclick="toggleUnits()"></div>
                        </div>
                    </div>
                    
                    <div class="section-title">Session Info</div>
                    <div class="config-section">
                        <div class="config-item">
                            <div>
                                <div class="config-label">Session ID</div>
                                <div class="config-desc" id="session-id">--</div>
                            </div>
                        </div>
                        <div class="config-item">
                            <div>
                                <div class="config-label">Session Start</div>
                                <div class="config-desc" id="session-start">--</div>
                            </div>
                        </div>
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
            <button onclick="showPage('config')">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <circle cx="12" cy="12" r="3"/>
                    <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/>
                </svg>
                Config
            </button>
        </nav>
    </div>
    
    <!-- Sensor Detail Modal -->
    <div class="modal" id="sensor-modal">
        <div class="modal-content">
            <div class="modal-header">
                <div class="modal-title" id="sensor-modal-title">Sensor</div>
                <button class="modal-close" onclick="closeSensorModal()">&times;</button>
            </div>
            <div class="modal-body">
                <div class="modal-chart"><canvas id="sensor-chart"></canvas></div>
                <div class="modal-stats" id="sensor-modal-stats"></div>
            </div>
        </div>
    </div>
    
    <!-- DTC Modal -->
    <div class="modal" id="dtc-modal">
        <div class="modal-content">
            <div class="modal-header">
                <div class="modal-title">Diagnostic Codes</div>
                <button class="modal-close" onclick="closeDTCModal()">&times;</button>
            </div>
            <div class="modal-body">
                <div class="dtc-list" id="dtc-list"></div>
            </div>
        </div>
    </div>
    
    <!-- Confirm Modal -->
    <div class="modal" id="confirm-modal">
        <div class="modal-content">
            <div class="modal-header">
                <div class="modal-title">Confirm Action</div>
                <button class="modal-close" onclick="closeConfirmModal()">&times;</button>
            </div>
            <div class="modal-body">
                <div class="confirm-text">Are you sure you want to clear all diagnostic codes?</div>
                <div class="confirm-buttons">
                    <button class="confirm-btn cancel" onclick="closeConfirmModal()">Cancel</button>
                    <button class="confirm-btn confirm" onclick="clearDTC()">Clear Codes</button>
                </div>
            </div>
        </div>
    </div>
    
    <script>
        const data = {};
        let config = { theme: 'dark', units: 'metric' };
        let ws;
        let isConnected = false;
        
        // Load config
        fetch('/api/config').then(r => r.json()).then(c => {
            config = c;
            applyConfig();
        });
        
        function applyConfig() {
            document.body.dataset.theme = config.theme;
            document.getElementById('theme-toggle').classList.toggle('active', config.theme === 'dark');
            document.getElementById('units-toggle').classList.toggle('active', config.units === 'metric');
        }
        
        function toggleTheme() {
            config.theme = config.theme === 'dark' ? 'light' : 'dark';
            applyConfig();
            fetch('/api/config', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(config) });
        }
        
        function toggleUnits() {
            config.units = config.units === 'metric' ? 'imperial' : 'metric';
            applyConfig();
            fetch('/api/config', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(config) });
            updateUI();
        }
        
        // Convert units
        function convertSpeed(kmh) {
            return config.units === 'metric' ? kmh : kmh * 0.621371;
        }
        
        function convertTemp(c) {
            return config.units === 'metric' ? c : c * 9/5 + 32;
        }
        
        // Gauge drawing
        function drawGauge(svgId, value, max, color = '#22c55e') {
            const svg = document.getElementById(svgId);
            if (!svg) return;
            
            const percent = Math.min(Math.max(value / max, 0), 1);
            const startAngle = -135;
            const endAngle = startAngle + (270 * percent);
            const cx = 100, cy = 100, r = 75;
            
            // Create background arc (full)
            const bgPath = describeArc(cx, cy, r, -135, 135);
            
            // Create value arc
            const valPath = describeArc(cx, cy, r, -135, endAngle);
            
            svg.innerHTML = `
                <path d="${bgPath}" fill="none" stroke="#1a1a1a" stroke-width="10" stroke-linecap="round"/>
                <path d="${valPath}" fill="none" stroke="${color}" stroke-width="10" stroke-linecap="round"/>
            `;
        }
        
        function describeArc(x, y, radius, startAngle, endAngle) {
            const start = polarToCartesian(x, y, radius, endAngle);
            const end = polarToCartesian(x, y, radius, startAngle);
            const largeArcFlag = endAngle - startAngle <= 180 ? "0" : "1";
            return [
                "M", start.x, start.y,
                "A", radius, radius, 0, largeArcFlag, 0, end.x, end.y
            ].join(" ");
        }
        
        function polarToCartesian(cx, cy, r, angle) {
            const rad = (angle - 90) * Math.PI / 180;
            return { x: cx + r * Math.cos(rad), y: cy + r * Math.sin(rad) };
        }
        
        // Chart drawing
        function drawChart(canvasId, points, color = '#22c55e') {
            const canvas = document.getElementById(canvasId);
            if (!canvas) return;
            
            const ctx = canvas.getContext('2d');
            const rect = canvas.getBoundingClientRect();
            canvas.width = rect.width * 2;
            canvas.height = rect.height * 2;
            ctx.scale(2, 2);
            
            if (!points || points.length < 2) {
                ctx.fillStyle = '#666';
                ctx.font = '12px system-ui';
                ctx.fillText('No data', 10, rect.height / 2);
                return;
            }
            
            const w = rect.width, h = rect.height, padding = 8;
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
        }
        
        // Update UI
        function updateUI() {
            const unitC = config.units === 'metric' ? '°C' : '°F';
            
            const rpm = data.RPM?.value || 0;
            const rpmEl = document.getElementById('rpm-value');
            rpmEl.textContent = Math.round(rpm).toLocaleString();
            rpmEl.className = 'gauge-value' + (rpm > 6000 ? ' danger' : rpm > 5000 ? ' warning' : '');
            drawGauge('rpm-gauge', rpm, 8000, rpm > 6000 ? '#ef4444' : rpm > 5000 ? '#f59e0b' : '#22c55e');
            
            const speedKmh = data.SPEED?.value || 0;
            const speed = convertSpeed(speedKmh);
            document.getElementById('speed-value').textContent = Math.round(speed);
            document.getElementById('speed-unit').textContent = config.units === 'metric' ? 'km/h' : 'mph';
            drawGauge('speed-gauge', speed, config.units === 'metric' ? 200 : 125, '#3b82f6');
            
            const format = (v, u) => v !== undefined ? `${Math.round(v)}${u}` : '--';
            
            const intakeTemp = data.INTAKE_TEMP?.value;
            document.getElementById('intake-value').textContent = format(convertTemp(intakeTemp), '');
            document.getElementById('intake-label').textContent = `Intake ${unitC}`;
            
            document.getElementById('load-value').textContent = format(data.ENGINE_LOAD?.value, '%');
            document.getElementById('throttle-value').textContent = format(data.THROTTLE_POS?.value, '%');
            document.getElementById('timing-value').textContent = format(data.TIMING_ADVANCE?.value, '°');
            
            const fuel = data.FUEL_LEVEL?.value;
            const fuelEl = document.getElementById('fuel-value');
            fuelEl.textContent = format(fuel, '%');
            fuelEl.className = 'stat-value' + (fuel < 15 ? ' danger' : fuel < 25 ? ' warning' : '');
            
            document.getElementById('maf-value').textContent = data.MAF?.value ? `${data.MAF.value.toFixed(1)}` : '--';
            document.getElementById('voltage-value').textContent = data.CONTROL_MODULE_VOLTAGE?.value ? `${data.CONTROL_MODULE_VOLTAGE.value.toFixed(1)}` : '--';
            
            const oilTemp = data.OIL_TEMP?.value;
            document.getElementById('oil-value').textContent = oilTemp ? Math.round(convertTemp(oilTemp)) : '--';
            document.getElementById('oil-label').textContent = `Oil ${unitC}`;
            
            // Sensors list
            const grid = document.getElementById('sensors-grid');
            grid.innerHTML = Object.entries(data)
                .filter(([k]) => !['RPM', 'SPEED'].includes(k))
                .map(([name, val]) => `
                    <div class="sensor-card" onclick="showSensorDetail('${name}')" data-tooltip="${getSensorDescription(name)}">
                        <div class="sensor-name">${name.replace(/_/g, ' ')}</div>
                        <div class="sensor-value">${val.value.toFixed(1)}<span class="sensor-unit">${val.unit}</span></div>
                    </div>
                `).join('');
        }
        
        function getSensorDescription(name) {
            const descriptions = {
                'INTAKE_TEMP': 'Temperature of air entering engine',
                'THROTTLE_POS': 'Throttle plate position',
                'ENGINE_LOAD': 'Current engine load percentage',
                'FUEL_LEVEL': 'Fuel tank level',
                'MAF': 'Mass Air Flow sensor reading',
                'TIMING_ADVANCE': 'Spark timing relative to TDC',
                'CONTROL_MODULE_VOLTAGE': 'ECU supply voltage',
                'BAROMETRIC_PRESSURE': 'Atmospheric pressure',
                'OIL_TEMP': 'Engine oil temperature',
                'FUEL_RATE': 'Fuel consumption rate',
                'COMMANDED_EQUIV_RATIO': 'Target air-fuel ratio'
            };
            return descriptions[name] || 'Vehicle sensor data';
        }
        
        // Sensor detail modal
        async function showSensorDetail(sensor) {
            document.getElementById('sensor-modal-title').textContent = sensor.replace(/_/g, ' ');
            document.getElementById('sensor-modal').classList.add('active');
            
            try {
                const history = await fetch(`/api/history/${sensor}?minutes=30`).then(r => r.json());
                drawChart('sensor-chart', history, '#22c55e');
                
                if (history && history.length > 0) {
                    const values = history.map(p => p.v);
                    const min = Math.min(...values);
                    const max = Math.max(...values);
                    const avg = values.reduce((a, b) => a + b, 0) / values.length;
                    const current = values[values.length - 1];
                    
                    document.getElementById('sensor-modal-stats').innerHTML = `
                        <div class="modal-stat">
                            <div class="history-stat-value">${current.toFixed(1)}</div>
                            <div class="history-stat-label">Current</div>
                        </div>
                        <div class="modal-stat">
                            <div class="history-stat-value">${avg.toFixed(1)}</div>
                            <div class="history-stat-label">Average</div>
                        </div>
                        <div class="modal-stat">
                            <div class="history-stat-value">${min.toFixed(1)}</div>
                            <div class="history-stat-label">Min</div>
                        </div>
                        <div class="modal-stat">
                            <div class="history-stat-value">${max.toFixed(1)}</div>
                            <div class="history-stat-label">Max</div>
                        </div>
                    `;
                }
            } catch(e) {
                console.error(e);
            }
        }
        
        function closeSensorModal() {
            document.getElementById('sensor-modal').classList.remove('active');
        }
        
        // DTC
        async function showDTC() {
            document.getElementById('dtc-modal').classList.add('active');
            
            try {
                const result = await fetch('/api/dtc').then(r => r.json());
                const list = document.getElementById('dtc-list');
                
                if (result.dtc && result.dtc.length > 0) {
                    list.innerHTML = result.dtc.map(([code, desc]) => `
                        <div class="dtc-item">
                            <div class="dtc-code">${code}</div>
                            <div class="dtc-desc">${desc || 'Unknown'}</div>
                        </div>
                    `).join('');
                } else {
                    list.innerHTML = '<div class="no-dtc">✓ No diagnostic codes</div>';
                }
            } catch(e) {
                console.error(e);
            }
        }
        
        function closeDTCModal() {
            document.getElementById('dtc-modal').classList.remove('active');
        }
        
        function confirmClearDTC() {
            document.getElementById('confirm-modal').classList.add('active');
        }
        
        function closeConfirmModal() {
            document.getElementById('confirm-modal').classList.remove('active');
        }
        
        async function clearDTC() {
            try {
                await fetch('/api/dtc/clear', { method: 'POST' });
                closeConfirmModal();
                showDTC();
            } catch(e) {
                console.error(e);
            }
        }
        
        // History stats
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
                
                renderHistoryStats('speed-stats', speed, config.units === 'metric' ? ' km/h' : ' mph');
                renderHistoryStats('rpm-stats', rpm, ' RPM');
                renderHistoryStats('load-stats', load, '%');
                renderHistoryStats('fuel-stats', fuel, '%');
            } catch (e) {
                console.error('Failed to fetch history', e);
            }
        }
        
        // Page navigation
        function showPage(page) {
            document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
            document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
            document.getElementById(`page-${page}`).classList.add('active');
            event.target.closest('button').classList.add('active');
            
            if (page === 'history') fetchHistory();
            if (page === 'config') updateSessionInfo();
        }
        
        async function updateSessionInfo() {
            try {
                const status = await fetch('/api/status').then(r => r.json());
                document.getElementById('session-id').textContent = status.session_id?.substring(0, 8) || '--';
                document.getElementById('session-start').textContent = status.session_start 
                    ? new Date(status.session_start * 1000).toLocaleTimeString() 
                    : '--';
            } catch(e) {}
        }
        
        // WebSocket connection
        let connectionLogs = [];
        
        function addLog(message, status = 'info') {
            const now = new Date();
            const time = now.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
            connectionLogs.push({ time, message, status });
            
            // Keep last 10 logs
            if (connectionLogs.length > 10) {
                connectionLogs.shift();
            }
            
            const logsEl = document.getElementById('connection-logs');
            if (logsEl) {
                logsEl.innerHTML = connectionLogs.map(log => `
                    <div class="log-entry ${log.status}">
                        <span class="time">${log.time}</span>
                        <span>${log.message}</span>
                    </div>
                `).join('');
                logsEl.scrollTop = logsEl.scrollHeight;
            }
        }
        
        function connect() {
            const wsUrl = `ws://${location.host}/ws`;
            console.log('Connecting to', wsUrl);
            
            addLog('Starting connection...', 'info');
            
            try {
                ws = new WebSocket(wsUrl);
                
                ws.onopen = () => {
                    console.log('WebSocket connected');
                    isConnected = true;
                    document.getElementById('status-dot').classList.add('connected');
                    document.getElementById('status-text').textContent = 'Connected';
                };
                
                ws.onclose = () => {
                    console.log('WebSocket disconnected');
                    isConnected = false;
                    document.getElementById('status-dot').classList.remove('connected');
                    document.getElementById('status-text').textContent = 'Reconnecting...';
                    document.getElementById('connection-overlay').classList.remove('hidden');
                    connectionLogs = [];
                    setTimeout(connect, 2000);
                };
                
                ws.onerror = (e) => {
                    console.error('WebSocket error:', e);
                    addLog('WebSocket error', 'error');
                };
                
                ws.onmessage = (event) => {
                    const msg = JSON.parse(event.data);
                    
                    if (msg.type === 'sensor_update') {
                        Object.assign(data, msg.data);
                        updateUI();
                        
                        if (document.getElementById('connection-overlay').classList.contains('hidden') === false) {
                            document.getElementById('connection-overlay').classList.add('hidden');
                        }
                    } else if (msg.type === 'connection_progress') {
                        // Update progress bar
                        const progress = msg.progress || 0;
                        document.getElementById('progress-fill').style.width = progress + '%';
                        
                        // Update text
                        document.getElementById('connection-text').textContent = msg.message;
                        
                        // Add log
                        addLog(msg.message, msg.status || 'info');
                    }
                };
            } catch(e) {
                console.error('WebSocket init error:', e);
                addLog('Connection failed: ' + e.message, 'error');
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
    parser.add_argument("--port", "-p", type=int, default=9000)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    
    print("OBD Commander - Car Computer")
    print("=" * 40)
    
    if OBD_AVAILABLE:
        print("Connecting to OBD...")
        if obd_manager.connect():
            print(f"✓ Connected! {len(obd_manager.supported)} sensors")
        else:
            print("✗ No OBD connection - will retry...")
    else:
        print("✗ OBD library not installed")
    
    print(f"\nDashboard: http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop\n")
    
    uvicorn.run(app, host=args.host, port=args.port, log_level="error")
