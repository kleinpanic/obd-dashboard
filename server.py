#!/usr/bin/env python3
"""
OBD Commander - Car computer backend
WebSocket server + SQLite logging + REST API
Optimized for RPi4, offline-capable
"""

import asyncio
import json
import logging
import sqlite3
import time
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
LOG_PATH = DATA_DIR / "obdc.log"

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("obdc")

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

    # Vehicle profiles table
    c.execute('''
        CREATE TABLE IF NOT EXISTS vehicle_profiles (
            vin TEXT PRIMARY KEY,
            make TEXT,
            model TEXT,
            year INTEGER,
            max_rpm INTEGER DEFAULT 8000,
            max_speed INTEGER DEFAULT 200,
            redline_rpm INTEGER DEFAULT 6500,
            normal_temp_min REAL DEFAULT 70,
            normal_temp_max REAL DEFAULT 95,
            warning_temp REAL DEFAULT 105,
            low_fuel_warning REAL DEFAULT 25,
            low_fuel_danger REAL DEFAULT 15,
            created_at REAL,
            updated_at REAL
        )
    ''')

    # Sessions table - add vin column if missing
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'")
    if c.fetchone():
        c.execute("PRAGMA table_info(sessions)")
        columns = [col[1] for col in c.fetchall()]
        if 'vin' not in columns:
            c.execute("ALTER TABLE sessions ADD COLUMN vin TEXT")

    c.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            vin TEXT,
            start_time REAL,
            end_time REAL,
            distance_km REAL DEFAULT 0,
            max_rpm INTEGER DEFAULT 0,
            max_speed INTEGER DEFAULT 0,
            avg_fuel REAL DEFAULT 0,
            FOREIGN KEY (vin) REFERENCES vehicle_profiles(vin)
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

    # Vehicle stats cache
    c.execute('''
        CREATE TABLE IF NOT EXISTS vehicle_stats (
            vin TEXT PRIMARY KEY,
            total_sessions INTEGER DEFAULT 0,
            total_distance_km REAL DEFAULT 0,
            total_duration_seconds INTEGER DEFAULT 0,
            max_rpm_ever INTEGER DEFAULT 0,
            max_speed_ever INTEGER DEFAULT 0,
            avg_rpm REAL DEFAULT 0,
            avg_speed REAL DEFAULT 0,
            last_session REAL
        )
    ''')

    conn.commit()
    conn.close()

init_db()

# Vehicle profile management
# ── VIN decode ──────────────────────────────────────────────────────────────
# WMI prefix (first 5 chars) + engine code (position 8, 0-indexed pos 7)
# → (make, model, redline_rpm, max_rpm, max_speed_kmh)
# max_speed = speedometer dial max, not ECU limiter
_VIN_SPECS = {
    # Format: (wmi5, engine_pos8): (make, model, redline_rpm, max_rpm, max_speed_kmh)
    # redline_rpm = where red zone starts; max_rpm = gauge arc end (tachometer max)

    # Subaru Crosstrek (JF2GT) — 2.0L FB20 (engine code C)
    # Tachometer: 8k max, red zone starts at 6k
    ("JF2GT", "C"): ("Subaru", "Crosstrek 2.0L", 6000, 8000, 240),
    # Subaru Crosstrek 2.5L (engine code N, 2024+)
    ("JF2GT", "N"): ("Subaru", "Crosstrek 2.5L", 6400, 8000, 240),
    # Subaru WRX 2.0T
    ("JF1VA", "H"): ("Subaru", "WRX 2.0T", 6800, 9000, 260),
    # Subaru WRX STI 2.5T
    ("JF1GR", "X"): ("Subaru", "WRX STI 2.5T", 7200, 9000, 280),
    # Subaru Outback / Legacy 2.5L
    ("4S4BT", "A"): ("Subaru", "Outback 2.5L", 6500, 7500, 220),
    # Toyota Corolla 1.8L
    ("JTDBU", "U"): ("Toyota", "Corolla 1.8L", 6400, 7500, 200),
    # Honda Civic 1.5T
    ("2HGF", "R"): ("Honda", "Civic 1.5T", 6200, 7500, 220),
    # Honda Civic (alt WMI)
    ("19XF", "R"): ("Honda", "Civic 1.5T", 6200, 7500, 220),
    # Ford F-150 5.0L
    ("1FTEW", "F"): ("Ford", "F-150 5.0L", 6500, 7000, 200),
    # Mazda 3
    ("JM1BN", "P"): ("Mazda", "3 2.5L", 6500, 7500, 220),
    # Volkswagen GTI / Golf 2.0T
    ("WVWGJ", "H"): ("Volkswagen", "GTI 2.0T", 6500, 7000, 250),
}

_MODEL_YEAR = {
    'A': 2010, 'B': 2011, 'C': 2012, 'D': 2013, 'E': 2014,
    'F': 2015, 'G': 2016, 'H': 2017, 'J': 2018, 'K': 2019,
    'L': 2020, 'M': 2021, 'N': 2022, 'P': 2023, 'R': 2024, 'S': 2025,
}


def decode_vin(vin: str) -> dict:
    """
    Decode VIN into vehicle specs using WMI pattern + engine code.
    Position key:
      vin[0:3]  = WMI (World Manufacturer Identifier)
      vin[0:5]  = WMI prefix used for model matching
      vin[7]    = engine/restraint code (position 8)
      vin[9]    = model year code (position 10)
    Returns dict with make, model, year, redline_rpm, max_rpm, max_speed.
    Falls back to sensible defaults for unknown VINs.
    """
    if not vin or len(vin) < 10:
        return {}

    wmi5 = vin[:5]
    engine_code = vin[7]
    year = _MODEL_YEAR.get(vin[9])

    specs = None
    # Exact match: WMI prefix + engine code
    if (wmi5, engine_code) in _VIN_SPECS:
        specs = _VIN_SPECS[(wmi5, engine_code)]
    else:
        # Fallback: match on WMI prefix alone
        for (wmi, _eng), s in _VIN_SPECS.items():
            if vin.startswith(wmi):
                specs = s
                break

    result = {}
    if year:
        result["year"] = year
    if specs:
        make, model, redline, max_rpm, max_speed = specs
        result.update({
            "make": make, "model": model,
            "redline_rpm": redline, "max_rpm": max_rpm, "max_speed": max_speed,
        })
    return result


def get_vehicle_profile(vin):
    """Get or create vehicle profile for VIN, auto-decoding specs from VIN."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM vehicle_profiles WHERE vin = ?", (vin,))
    row = c.fetchone()

    decoded = decode_vin(vin)

    # Defaults (used when VIN is unknown)
    defaults = {
        "max_rpm": decoded.get("max_rpm", 8000),
        "max_speed": decoded.get("max_speed", 200),
        "redline_rpm": decoded.get("redline_rpm", 6500),
        "normal_temp_min": 70, "normal_temp_max": 95,
        "warning_temp": 105,
        "low_fuel_warning": 25, "low_fuel_danger": 15,
    }

    if row:
        existing = {
            "vin": row[0], "make": row[1], "model": row[2], "year": row[3],
            "max_rpm": row[4], "max_speed": row[5], "redline_rpm": row[6],
            "normal_temp_min": row[7], "normal_temp_max": row[8],
            "warning_temp": row[9], "low_fuel_warning": row[10], "low_fuel_danger": row[11],
        }
        # If profile was created with generic defaults, upgrade it with decoded specs
        if existing["max_rpm"] in (8000,) and decoded.get("max_rpm"):
            update_vehicle_profile(vin,
                make=decoded.get("make"), model=decoded.get("model"),
                year=decoded.get("year"),
                max_rpm=decoded["max_rpm"], max_speed=decoded["max_speed"],
                redline_rpm=decoded["redline_rpm"],
            )
            existing.update({
                "make": decoded.get("make"), "model": decoded.get("model"),
                "year": decoded.get("year"), "max_rpm": decoded["max_rpm"],
                "max_speed": decoded["max_speed"], "redline_rpm": decoded["redline_rpm"],
            })
        conn.close()
        return existing

    # Create new profile
    now = time.time()
    c.execute('''
        INSERT INTO vehicle_profiles (vin, make, model, year, max_rpm, max_speed,
            redline_rpm, normal_temp_min, normal_temp_max, warning_temp,
            low_fuel_warning, low_fuel_danger, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        vin, decoded.get("make"), decoded.get("model"), decoded.get("year"),
        defaults["max_rpm"], defaults["max_speed"], defaults["redline_rpm"],
        defaults["normal_temp_min"], defaults["normal_temp_max"], defaults["warning_temp"],
        defaults["low_fuel_warning"], defaults["low_fuel_danger"], now, now,
    ))
    conn.commit()
    conn.close()

    return {"vin": vin, **decoded, **defaults}

def update_vehicle_profile(vin, **kwargs):
    """Update vehicle profile fields"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    fields = []
    values = []
    for k, v in kwargs.items():
        fields.append(f"{k} = ?")
        values.append(v)

    values.append(time.time())  # updated_at
    values.append(vin)

    c.execute(f'''
        UPDATE vehicle_profiles
        SET {', '.join(fields)}, updated_at = ?
        WHERE vin = ?
    ''', values)
    conn.commit()
    conn.close()

def get_vehicle_stats(vin):
    """Get aggregated stats for a vehicle"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Get from cache
    c.execute("SELECT * FROM vehicle_stats WHERE vin = ?", (vin,))
    row = c.fetchone()

    if row:
        conn.close()
        return {
            "total_sessions": row[1],
            "total_distance_km": row[2],
            "total_duration_seconds": row[3],
            "max_rpm_ever": row[4],
            "max_speed_ever": row[5],
            "avg_rpm": row[6],
            "avg_speed": row[7],
            "last_session": row[8],
        }

    # Calculate from sessions
    c.execute('''
        SELECT
            COUNT(*),
            COALESCE(SUM(distance_km), 0),
            COALESCE(SUM(end_time - start_time), 0),
            COALESCE(MAX(max_rpm), 0),
            COALESCE(MAX(max_speed), 0)
        FROM sessions WHERE vin = ?
    ''', (vin,))
    row = c.fetchone()

    stats = {
        "total_sessions": row[0],
        "total_distance_km": row[1],
        "total_duration_seconds": int(row[2]) if row[2] else 0,
        "max_rpm_ever": row[3],
        "max_speed_ever": row[4],
        "avg_rpm": 0,
        "avg_speed": 0,
    }

    conn.close()
    return stats

def save_session_stats(session_id, vin, data):
    """Save aggregated session stats"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Update session record
    c.execute('''
        UPDATE sessions
        SET vin = ?, max_rpm = ?, max_speed = ?, end_time = ?
        WHERE id = ?
    ''', (vin, data.get('max_rpm', 0), data.get('max_speed', 0), time.time(), session_id))

    # Update vehicle stats cache
    c.execute('''
        INSERT INTO vehicle_stats (vin, total_sessions, max_rpm_ever, max_speed_ever, last_session)
        VALUES (?, 1, ?, ?, ?)
        ON CONFLICT(vin) DO UPDATE SET
            total_sessions = total_sessions + 1,
            max_rpm_ever = MAX(max_rpm_ever, ?),
            max_speed_ever = MAX(max_speed_ever, ?),
            last_session = ?
    ''', (vin, data.get('max_rpm', 0), data.get('max_speed', 0), time.time(),
          data.get('max_rpm', 0), data.get('max_speed', 0), time.time()))

    conn.commit()
    conn.close()

current_session_id = str(uuid.uuid4())
session_start_time = None
current_vin = None
vehicle_profile = None
session_stats = {"max_rpm": 0, "max_speed": 0}

# Engine state tracking
engine_on = False
engine_off_since = None
ENGINE_OFF_TIMEOUT = 30  # seconds of RPM=0 before declaring engine off

def start_session(reason="server_start"):
    global current_session_id, session_start_time, session_stats
    current_session_id = str(uuid.uuid4())
    session_start_time = time.time()
    session_stats = {"max_rpm": 0, "max_speed": 0}

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO sessions (id, start_time) VALUES (?, ?)", (current_session_id, session_start_time))
    conn.commit()
    conn.close()
    logger.info(f"SESSION START [{reason}] id={current_session_id[:8]} vin={current_vin or 'unknown'}")

def end_session(reason="server_stop"):
    global session_start_time
    if session_start_time:
        duration = time.time() - session_start_time
        save_session_stats(current_session_id, current_vin, session_stats)
        logger.info(
            f"SESSION END [{reason}] id={current_session_id[:8]} "
            f"duration={duration:.0f}s max_rpm={session_stats['max_rpm']} "
            f"max_speed={session_stats['max_speed']}"
        )
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
        self.last_sensor_data = {}
        self.all_sensors_data = {}
        self.dtc_codes = []

    def connect(self):
        global current_vin, vehicle_profile
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

                # Get VIN and load vehicle profile
                try:
                    vin_resp = self.connection.query(obd.commands.VIN)
                    if not vin_resp.is_null():
                        vin_val = vin_resp.value
                        # Handle different VIN formats from obd library
                        if hasattr(vin_val, 'decode'):
                            current_vin = vin_val.decode('utf-8').strip()
                        elif isinstance(vin_val, bytes):
                            current_vin = vin_val.decode('utf-8').strip()
                        elif 'bytearray' in str(type(vin_val)):
                            current_vin = bytes(vin_val).decode('utf-8').strip()
                        else:
                            current_vin = str(vin_val).strip()
                        vehicle_profile = get_vehicle_profile(current_vin)
                        print(f"VIN detected: {current_vin}")
                except Exception as e:
                    print(f"VIN decode error: {e}")
                    pass

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

                                # Track session maxes
                                if cmd.name == 'RPM':
                                    session_stats['max_rpm'] = max(session_stats['max_rpm'], int(value))
                                elif cmd.name == 'SPEED':
                                    session_stats['max_speed'] = max(session_stats['max_speed'], int(value))
                    except Exception as e:
                        pass

        for k, v in self.last_data.items():
            if k not in data:
                data[k] = v

        if data:
            self.last_sensor_data = data
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
        # Cache all sensors for API endpoint
        if data:
            self.all_sensors_data = data
        return data

    def get_vin(self):
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
    global engine_on, engine_off_since
    consecutive_failures = 0
    connection_step = 0
    full_sensor_counter = 0

    while True:
        if obd_manager.connected and ws_manager.active_connections:
            try:
                data = obd_manager.read_key_sensors()
                if data:
                    # --- Engine state detection ---
                    rpm = data.get('RPM', {}).get('value', None)
                    if rpm is not None:
                        if rpm > 100:
                            # Engine is running
                            engine_off_since = None
                            if not engine_on:
                                engine_on = True
                                start_session(reason="engine_on")
                                logger.info(f"ENGINE ON rpm={rpm:.0f}")
                        else:
                            # RPM near 0 - may be engine off
                            if engine_on:
                                if engine_off_since is None:
                                    engine_off_since = time.time()
                                elif time.time() - engine_off_since >= ENGINE_OFF_TIMEOUT:
                                    engine_on = False
                                    engine_off_since = None
                                    end_session(reason="engine_off")
                                    logger.info("ENGINE OFF")
                    # --- End engine state detection ---

                    await ws_manager.broadcast({
                        "type": "sensor_update",
                        "timestamp": time.time(),
                        "data": data,
                        "session_id": current_session_id,
                        "vehicle": vehicle_profile,
                        "session_stats": session_stats
                    })
                    consecutive_failures = 0
                    
                    # --- Periodic full sensor query (every 10 iterations = ~2.5s) ---
                    full_sensor_counter += 1
                    if full_sensor_counter >= 10:
                        full_sensor_counter = 0
                        # Run in thread pool to not block event loop
                        loop = asyncio.get_event_loop()
                        await loop.run_in_executor(None, obd_manager.read_all)
                else:
                    consecutive_failures += 1
                    if consecutive_failures > 5:
                        logger.warning("OBD connection lost after repeated failures, reconnecting...")
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
                logger.error(f"OBD reader error: {e}")
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
                    try:
                        proto = obd_manager.connection.protocol_name()
                    except Exception:
                        proto = "unknown"
                    logger.info(
                        f"OBD CONNECTED protocol={proto} "
                        f"sensors={len(obd_manager.supported)} vin={current_vin or 'pending'}"
                    )
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
                    logger.warning("OBD connection attempt failed, retrying...")
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
    logger.info("OBD Commander starting up")
    start_session(reason="server_start")
    asyncio.create_task(obd_reader())
    yield
    end_session(reason="server_stop")
    obd_manager.disconnect()
    logger.info("OBD Commander shut down")

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
        "vin": current_vin,
        "session_id": current_session_id,
        "session_start": session_start_time,
        "session_max_rpm": session_stats.get('max_rpm', 0),
        "session_max_speed": session_stats.get('max_speed', 0)
    }

@app.get("/api/vehicle")
async def get_vehicle():
    if not current_vin:
        return {"error": "No VIN detected"}

    profile = get_vehicle_profile(current_vin)
    stats = get_vehicle_stats(current_vin)

    return {
        "vin": current_vin,
        "profile": profile,
        "stats": stats
    }

@app.post("/api/vehicle")
async def update_vehicle(data: dict):
    if not current_vin:
        return {"error": "No VIN detected"}

    update_vehicle_profile(current_vin, **data)
    global vehicle_profile
    vehicle_profile = get_vehicle_profile(current_vin)

    return vehicle_profile


@app.get("/api/profiles")
async def list_profiles():
    """List all stored vehicle profiles."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT vp.vin, vp.make, vp.model, vp.year,
               vp.max_rpm, vp.redline_rpm, vp.max_speed,
               vp.normal_temp_min, vp.normal_temp_max,
               vp.warning_temp, vp.low_fuel_warning, vp.low_fuel_danger,
               vp.created_at,
               COALESCE(vs.total_sessions, 0) as total_sessions,
               COALESCE(vs.max_rpm_ever, 0) as max_rpm_ever,
               COALESCE(vs.max_speed_ever, 0) as max_speed_ever,
               COALESCE(vs.last_session, 0) as last_session
        FROM vehicle_profiles vp
        LEFT JOIN vehicle_stats vs ON vp.vin = vs.vin
        ORDER BY vs.last_session DESC NULLS LAST
    """)
    rows = c.fetchall()
    conn.close()

    profiles = []
    for row in rows:
        vin = row[0]
        decoded = decode_vin(vin)
        profiles.append({
            "vin": vin,
            "make": row[1] or decoded.get("make"),
            "model": row[2] or decoded.get("model"),
            "year": row[3] or decoded.get("year"),
            "max_rpm": row[4],
            "redline_rpm": row[5],
            "max_speed": row[6],
            "normal_temp_min": row[7],
            "normal_temp_max": row[8],
            "warning_temp": row[9],
            "low_fuel_warning": row[10],
            "low_fuel_danger": row[11],
            "created_at": row[12],
            "total_sessions": row[13],
            "max_rpm_ever": row[14],
            "max_speed_ever": row[15],
            "last_session": row[16],
            "is_active": vin == current_vin,
            "decoded": decoded,
        })
    return {"profiles": profiles, "count": len(profiles)}


@app.delete("/api/profiles/{vin}")
async def delete_profile(vin: str):
    """Delete a vehicle profile (and its stats). Cannot delete the currently active VIN."""
    if vin == current_vin:
        return {"error": "Cannot delete the currently active vehicle profile"}
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM vehicle_stats WHERE vin = ?", (vin,))
    c.execute("DELETE FROM vehicle_profiles WHERE vin = ?", (vin,))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    if deleted:
        logger.info(f"PROFILE DELETED vin={vin}")
        return {"status": "deleted", "vin": vin}
    return {"error": "Profile not found"}

@app.get("/api/sensors")
async def sensors():
    # Return all cached sensors (populated by periodic background query)
    # Falls back to last_data if all_sensors_data is empty
    if obd_manager.all_sensors_data:
        return obd_manager.all_sensors_data
    return obd_manager.last_data

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
    config.update(cfg)
    save_config(config)
    return config


@app.get("/api/logs")
async def get_logs(limit: int = 100, offset: int = 0):
    """Return paginated, parsed log entries from the log file (newest first)."""
    try:
        if not LOG_PATH.exists():
            return {"entries": [], "total": 0, "offset": offset, "limit": limit}
        lines = LOG_PATH.read_text(errors="replace").splitlines()
        # Only app-level structured entries (skip raw obd library noise)
        app_lines = [
            l for l in lines
            if any(tag in l for tag in ("[INFO]", "[WARNING]", "[ERROR]", "[DEBUG]"))
            and l.strip()
        ]
        app_lines.reverse()  # newest first
        total = len(app_lines)
        page = app_lines[offset:offset + limit]
        entries = []
        for line in page:
            level = "info"
            if "[ERROR]" in line:
                level = "error"
            elif "[WARNING]" in line:
                level = "warning"
            elif "[DEBUG]" in line:
                level = "debug"
            entries.append({"text": line.strip(), "level": level})
        return {"entries": entries, "total": total, "offset": offset, "limit": limit}
    except Exception as e:
        logger.error(f"Failed to read logs: {e}")
        return {"entries": [], "total": 0, "offset": offset, "limit": limit}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        if obd_manager.connected and obd_manager.last_sensor_data:
            await websocket.send_json({
                "type": "sensor_update",
                "timestamp": time.time(),
                "data": obd_manager.last_sensor_data,
                "session_id": current_session_id,
                "vehicle": vehicle_profile,
                "session_stats": session_stats
            })
        else:
            await websocket.send_json({
                "type": "connection_progress",
                "step": 0,
                "progress": 0,
                "message": "Waiting for OBD connection...",
                "status": "info"
            })
    except Exception:
        pass
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

        .ascii-logo {
            font-family: 'Courier New', monospace;
            font-size: 6px;
            line-height: 1;
            color: var(--accent);
            white-space: pre;
            margin-bottom: 16px;
            text-align: center;
        }

        @media (min-width: 400px) {
            .ascii-logo { font-size: 7px; }
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

        /* Sparkline graphs */
        .sparkline-container {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 8px;
            margin-bottom: 16px;
        }

        .sparkline-card {
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 8px 6px 6px 6px;
        }

        .sparkline-label {
            font-size: 10px;
            color: var(--muted);
            text-transform: uppercase;
            margin-bottom: 4px;
        }

        .sparkline-canvas {
            height: 48px;
            width: 100%;
            display: block;
        }

        .sparkline-value {
            font-size: 11px;
            font-weight: 600;
            color: var(--text);
            text-align: center;
            margin-top: 3px;
        }

        /* Logs section */
        .logs-toggle {
            cursor: pointer;
            user-select: none;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .logs-toggle:hover { opacity: 0.8; }

        .logs-section {
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 12px;
            margin-bottom: 16px;
            overflow: hidden;
        }

        .logs-list {
            height: 280px;
            overflow-y: auto;
            padding: 8px;
            font-family: 'Courier New', monospace;
            font-size: 10px;
            line-height: 1.5;
        }

        .log-entry { padding: 2px 4px; border-radius: 3px; margin-bottom: 1px; word-break: break-all; }
        .log-entry.info  { color: var(--accent); }
        .log-entry.warning { color: var(--warning); }
        .log-entry.error { color: var(--danger); }
        .log-entry.debug { color: var(--muted); }

        .logs-footer {
            padding: 8px 12px;
            border-top: 1px solid var(--border);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .logs-meta { font-size: 11px; color: var(--muted); }

        .logs-load-btn {
            background: var(--border);
            border: none;
            color: var(--text);
            padding: 4px 12px;
            border-radius: 6px;
            font-size: 11px;
            cursor: pointer;
        }
        .logs-load-btn:hover { background: var(--muted); }

        /* Profiles section */
        .profile-card {
            border: 1px solid var(--border);
            border-radius: 10px;
            margin: 8px;
            overflow: hidden;
            transition: border-color 0.2s;
        }
        .profile-card.active-profile {
            border-color: var(--accent);
        }
        .profile-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 10px 12px;
            cursor: pointer;
            background: var(--bg);
        }
        .profile-header:active { opacity: 0.8; }
        .profile-title {
            font-size: 13px;
            font-weight: 600;
            color: var(--text);
        }
        .profile-subtitle {
            font-size: 11px;
            color: var(--muted);
            margin-top: 2px;
        }
        .profile-badge {
            font-size: 10px;
            background: var(--accent);
            color: #000;
            padding: 2px 7px;
            border-radius: 10px;
            font-weight: 700;
        }
        .profile-chevron {
            font-size: 10px;
            color: var(--muted);
            margin-left: 8px;
        }
        .profile-body {
            display: none;
            background: var(--card);
            border-top: 1px solid var(--border);
        }
        .profile-body.expanded { display: block; }
        .profile-rows { padding: 8px 12px; }
        .profile-row {
            display: flex;
            justify-content: space-between;
            padding: 5px 0;
            border-bottom: 1px solid var(--border);
            font-size: 12px;
        }
        .profile-row:last-child { border-bottom: none; }
        .profile-row-label { color: var(--muted); }
        .profile-row-val { color: var(--text); font-weight: 500; }
        .profile-actions {
            padding: 8px 12px;
            border-top: 1px solid var(--border);
            display: flex;
            justify-content: flex-end;
        }
        .profile-delete-btn {
            background: transparent;
            border: 1px solid var(--danger);
            color: var(--danger);
            padding: 5px 14px;
            border-radius: 6px;
            font-size: 12px;
            cursor: pointer;
        }
        .profile-delete-btn:hover { background: var(--danger); color: #fff; }
        .profile-delete-btn:disabled { opacity: 0.4; cursor: not-allowed; }
        .profiles-empty {
            text-align: center;
            color: var(--muted);
            font-size: 12px;
            padding: 20px;
        }

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
        <pre class="ascii-logo">
  ____  ____  ____  ____
 / __ \\/ __ \\/ __ \\/ __ \\
| |  | |  | | |  | |  | |
| |__| |__| | |__| |__| |
 \\____\\____\\____\\____\\___\\

    O B D   C O M M A N D E R
        </pre>
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

                    <!-- Mini Sparkline Graphs -->
                    <div class="sparkline-container">
                        <div class="sparkline-card">
                            <div class="sparkline-label">RPM</div>
                            <canvas id="rpm-sparkline" class="sparkline-canvas"></canvas>
                            <div class="sparkline-value" id="spark-rpm-val">--</div>
                        </div>
                        <div class="sparkline-card">
                            <div class="sparkline-label">Speed</div>
                            <canvas id="speed-sparkline" class="sparkline-canvas"></canvas>
                            <div class="sparkline-value" id="spark-speed-val">--</div>
                        </div>
                        <div class="sparkline-card">
                            <div class="sparkline-label">Load</div>
                            <canvas id="load-sparkline" class="sparkline-canvas"></canvas>
                            <div class="sparkline-value" id="spark-load-val">--</div>
                        </div>
                        <div class="sparkline-card">
                            <div class="sparkline-label">Throttle</div>
                            <canvas id="throttle-sparkline" class="sparkline-canvas"></canvas>
                            <div class="sparkline-value" id="spark-throttle-val">--</div>
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
                    <div class="section-title">Vehicle</div>
                    <div class="config-section">
                        <div class="config-item">
                            <div>
                                <div class="config-label">VIN</div>
                                <div class="config-desc" id="vehicle-vin">--</div>
                            </div>
                        </div>
                        <div class="config-item">
                            <div>
                                <div class="config-label">Make / Model / Year</div>
                                <div class="config-desc" id="vehicle-model">--</div>
                            </div>
                        </div>
                        <div class="config-item">
                            <div>
                                <div class="config-label">Max RPM / Redline</div>
                                <div class="config-desc" id="vehicle-redline">--</div>
                            </div>
                        </div>
                        <div class="config-item">
                            <div>
                                <div class="config-label">Max Speed (gauge)</div>
                                <div class="config-desc" id="vehicle-maxspeed">--</div>
                            </div>
                        </div>
                    </div>

                    <div class="section-title">Vehicle Stats (All Time)</div>
                    <div class="config-section">
                        <div class="config-item">
                            <div>
                                <div class="config-label">Total Sessions</div>
                                <div class="config-desc" id="stats-sessions">--</div>
                            </div>
                        </div>
                        <div class="config-item">
                            <div>
                                <div class="config-label">Max RPM Ever</div>
                                <div class="config-desc" id="stats-maxrpm">--</div>
                            </div>
                        </div>
                        <div class="config-item">
                            <div>
                                <div class="config-label">Max Speed Ever</div>
                                <div class="config-desc" id="stats-maxspeed">--</div>
                            </div>
                        </div>
                        <div class="config-item">
                            <div>
                                <div class="config-label">Total Distance</div>
                                <div class="config-desc" id="stats-distance">--</div>
                            </div>
                        </div>
                    </div>

                    <div class="section-title">Session</div>
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
                        <div class="config-item">
                            <div>
                                <div class="config-label">Session Max RPM</div>
                                <div class="config-desc" id="session-maxrpm">--</div>
                            </div>
                        </div>
                        <div class="config-item">
                            <div>
                                <div class="config-label">Session Max Speed</div>
                                <div class="config-desc" id="session-maxspeed">--</div>
                            </div>
                        </div>
                    </div>

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

                    <div class="section-title logs-toggle" onclick="toggleProfiles(this)">
                        Vehicle Profiles
                        <span id="profiles-chevron" style="font-size:12px;color:var(--muted);">▶ collapsed</span>
                    </div>
                    <div id="profiles-section" style="display:none;margin-bottom:16px;">
                        <div id="profiles-list"></div>
                    </div>

                    <div class="section-title logs-toggle" onclick="toggleLogs(this)">
                        System Logs
                        <span id="logs-chevron" style="font-size:12px;color:var(--muted);">▶ collapsed</span>
                    </div>
                    <div id="logs-section" class="logs-section" style="display:none;">
                        <div id="logs-list" class="logs-list"></div>
                        <div class="logs-footer">
                            <button class="logs-load-btn" id="logs-more-btn" onclick="loadMoreLogs()" style="display:none;">Load more</button>
                            <span id="logs-meta" class="logs-meta">--</span>
                        </div>
                    </div>

                </div>
            </div>
        </div>

        <nav>
            <button class="active" onclick="showPage('dashboard', this)">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <circle cx="12" cy="12" r="10"/>
                    <path d="M12 6v6l4 2"/>
                </svg>
                Dashboard
            </button>
            <button onclick="showPage('sensors', this)">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <rect x="3" y="3" width="7" height="7"/>
                    <rect x="14" y="3" width="7" height="7"/>
                    <rect x="14" y="14" width="7" height="7"/>
                    <rect x="3" y="14" width="7" height="7"/>
                </svg>
                Sensors
            </button>
            <button onclick="showPage('history', this)">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M3 3v18h18"/>
                    <path d="M18 9l-5 5-4-4-3 3"/>
                </svg>
                History
            </button>
            <button onclick="showPage('config', this)">
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
        let vehicle = { max_rpm: 8000, max_speed: 200, redline_rpm: 6500, low_fuel_warning: 25, low_fuel_danger: 15 };
        let sessionMax = { rpm: 0, speed: 0 };
        let ws;
        let isConnected = false;

        // Sparkline data buffers (keep last 60 points = 15 seconds at 4Hz)
        const sparklineData = {
            RPM: [],
            SPEED: [],
            ENGINE_LOAD: [],
            THROTTLE_POS: []
        };
        const SPARKLINE_MAX = 60;

        // Load config
        fetch('/api/config')
            .then(r => r.json())
            .then(c => {
                config = c;
                applyConfig();
            })
            .catch(() => {
                // Use defaults if fetch fails
                console.log('Using default config');
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

        // Sparkline drawing
        function drawSparkline(canvasId, values, color = '#22c55e') {
            const canvas = document.getElementById(canvasId);
            if (!canvas || values.length < 2) return;

            const ctx = canvas.getContext('2d');
            const rect = canvas.getBoundingClientRect();
            canvas.width = rect.width * 2;
            canvas.height = rect.height * 2;
            ctx.scale(2, 2);

            const w = rect.width;
            const h = rect.height;
            const padL = 26, padR = 3, padT = 6, padB = 4;

            const rawMax = Math.max(...values);
            const rawMin = Math.min(...values);
            const range = rawMax - rawMin || 1;

            const toY = v => padT + (1 - (v - rawMin) / range) * (h - padT - padB);
            const toX = i => padL + (i / (values.length - 1)) * (w - padL - padR);

            // Grid lines at 25%, 50%, 75%
            ctx.strokeStyle = 'rgba(255,255,255,0.06)';
            ctx.lineWidth = 0.5;
            [0.25, 0.5, 0.75].forEach(pct => {
                const y = padT + pct * (h - padT - padB);
                ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
            });

            // Y-axis labels: max at top, min at bottom
            ctx.fillStyle = 'rgba(255,255,255,0.3)';
            ctx.font = '6px sans-serif';
            ctx.textAlign = 'right';
            ctx.fillText(rawMax >= 1000 ? (rawMax/1000).toFixed(1)+'k' : Math.round(rawMax), padL - 2, padT + 5);
            ctx.fillText(rawMin >= 1000 ? (rawMin/1000).toFixed(1)+'k' : Math.round(rawMin), padL - 2, h - padB);

            // Filled area under line
            ctx.beginPath();
            values.forEach((v, i) => {
                const x = toX(i), y = toY(v);
                if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
            });
            ctx.lineTo(toX(values.length - 1), h - padB);
            ctx.lineTo(toX(0), h - padB);
            ctx.closePath();
            ctx.fillStyle = color.replace(')', ', 0.12)').replace('rgb', 'rgba').replace('#', 'rgba(').replace('rgba(', 'rgba(');
            // Simple semi-transparent fill
            const grad = ctx.createLinearGradient(0, padT, 0, h);
            grad.addColorStop(0, color + '33');
            grad.addColorStop(1, color + '00');
            ctx.fillStyle = grad;
            ctx.fill();

            // Data line
            ctx.beginPath();
            values.forEach((v, i) => {
                const x = toX(i), y = toY(v);
                if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
            });
            ctx.strokeStyle = color;
            ctx.lineWidth = 1.5;
            ctx.lineJoin = 'round';
            ctx.stroke();

            // Current value dot
            const lastX = toX(values.length - 1);
            const lastY = toY(values[values.length - 1]);
            ctx.beginPath();
            ctx.arc(lastX, lastY, 2, 0, Math.PI * 2);
            ctx.fillStyle = color;
            ctx.fill();
        }

        function updateSparklines() {
            drawSparkline('rpm-sparkline', sparklineData.RPM, '#22c55e');
            drawSparkline('speed-sparkline', sparklineData.SPEED, '#3b82f6');
            drawSparkline('load-sparkline', sparklineData.ENGINE_LOAD, '#f59e0b');
            drawSparkline('throttle-sparkline', sparklineData.THROTTLE_POS, '#a855f7');
            const rpm = (sparklineData.RPM || []).map(p => p.v !== undefined ? p.v : p);
            const spd = (sparklineData.SPEED || []).map(p => p.v !== undefined ? p.v : p);
            const ld = (sparklineData.ENGINE_LOAD || []).map(p => p.v !== undefined ? p.v : p);
            const th = (sparklineData.THROTTLE_POS || []).map(p => p.v !== undefined ? p.v : p);
            if (rpm.length) document.getElementById('spark-rpm-val').textContent = Math.round(rpm[rpm.length-1]);
            if (spd.length) document.getElementById('spark-speed-val').textContent = Math.round(spd[spd.length-1]);
            if (ld.length) document.getElementById('spark-load-val').textContent = Math.round(ld[ld.length-1]) + '%';
            if (th.length) document.getElementById('spark-throttle-val').textContent = Math.round(th[th.length-1]) + '%';
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

            // Color based on vehicle redline
            const rpmColor = rpm > vehicle.redline_rpm ? '#ef4444' : rpm > vehicle.redline_rpm * 0.9 ? '#f59e0b' : '#22c55e';
            rpmEl.className = 'gauge-value' + (rpm > vehicle.redline_rpm ? ' danger' : rpm > vehicle.redline_rpm * 0.9 ? ' warning' : '');
            drawGauge('rpm-gauge', rpm, vehicle.max_rpm, rpmColor);

            const speedKmh = data.SPEED?.value || 0;
            const speed = convertSpeed(speedKmh);
            document.getElementById('speed-value').textContent = Math.round(speed);
            document.getElementById('speed-unit').textContent = config.units === 'metric' ? 'km/h' : 'mph';
            drawGauge('speed-gauge', speed, config.units === 'metric' ? vehicle.max_speed : vehicle.max_speed * 0.621, '#3b82f6');

            const format = (v, u) => v !== undefined ? `${Math.round(v)}${u}` : '--';

            const intakeTemp = data.INTAKE_TEMP?.value;
            document.getElementById('intake-value').textContent = format(convertTemp(intakeTemp), '');
            document.getElementById('intake-label').textContent = `Intake ${unitC}`;

            // Engine load color
            const load = data.ENGINE_LOAD?.value || 0;
            const loadEl = document.getElementById('load-value');
            loadEl.textContent = format(load, '%');
            loadEl.className = 'stat-value' + (load > 90 ? ' danger' : load > 75 ? ' warning' : '');

            document.getElementById('throttle-value').textContent = format(data.THROTTLE_POS?.value, '%');
            document.getElementById('timing-value').textContent = format(data.TIMING_ADVANCE?.value, '°');

            const fuel = data.FUEL_LEVEL?.value;
            const fuelEl = document.getElementById('fuel-value');
            fuelEl.textContent = format(fuel, '%');
            fuelEl.className = 'stat-value' + (fuel < vehicle.low_fuel_danger ? ' danger' : fuel < vehicle.low_fuel_warning ? ' warning' : '');

            document.getElementById('maf-value').textContent = data.MAF?.value ? `${data.MAF.value.toFixed(1)}` : '--';
            document.getElementById('voltage-value').textContent = data.CONTROL_MODULE_VOLTAGE?.value ? `${data.CONTROL_MODULE_VOLTAGE.value.toFixed(1)}` : '--';

            const oilTemp = data.OIL_TEMP?.value;
            document.getElementById('oil-value').textContent = oilTemp ? Math.round(convertTemp(oilTemp)) : '--';
            document.getElementById('oil-label').textContent = `Oil ${unitC}`;

            // Update sparkline buffers
            if (rpm > 0) {
                sparklineData.RPM.push(rpm);
                if (sparklineData.RPM.length > SPARKLINE_MAX) sparklineData.RPM.shift();
            }
            if (speedKmh > 0) {
                sparklineData.SPEED.push(speedKmh);
                if (sparklineData.SPEED.length > SPARKLINE_MAX) sparklineData.SPEED.shift();
            }
            if (load > 0) {
                sparklineData.ENGINE_LOAD.push(load);
                if (sparklineData.ENGINE_LOAD.length > SPARKLINE_MAX) sparklineData.ENGINE_LOAD.shift();
            }
            const throttle = data.THROTTLE_POS?.value || 0;
            if (throttle >= 0) {
                sparklineData.THROTTLE_POS.push(throttle);
                if (sparklineData.THROTTLE_POS.length > SPARKLINE_MAX) sparklineData.THROTTLE_POS.shift();
            }

            updateSparklines();

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

        // ── Vehicle Profiles ─────────────────────────────────────────────────
        function toggleProfiles(btn) {
            const section = document.getElementById('profiles-section');
            const chevron = document.getElementById('profiles-chevron');
            const visible = section.style.display !== 'none';
            if (visible) {
                section.style.display = 'none';
                chevron.textContent = '▶ collapsed';
            } else {
                section.style.display = 'block';
                chevron.textContent = '▼';
                loadProfiles();
            }
        }

        function loadProfiles() {
            fetch('/api/profiles')
                .then(r => r.json())
                .then(data => {
                    const container = document.getElementById('profiles-list');
                    container.innerHTML = '';
                    if (!data.profiles || data.profiles.length === 0) {
                        container.innerHTML = '<div class="profiles-empty">No vehicle profiles stored yet.</div>';
                        return;
                    }
                    data.profiles.forEach((p, idx) => renderProfileCard(container, p, idx));
                })
                .catch(() => {
                    document.getElementById('profiles-list').innerHTML =
                        '<div class="profiles-empty">Failed to load profiles.</div>';
                });
        }

        function renderProfileCard(container, p, idx) {
            const card = document.createElement('div');
            card.className = 'profile-card' + (p.is_active ? ' active-profile' : '');
            card.id = `profile-card-${p.vin}`;

            const make = p.make || '?';
            const model = p.model || '?';
            const year = p.year || '?';
            const lastSeen = p.last_session
                ? new Date(p.last_session * 1000).toLocaleDateString()
                : 'Never';
            const speedMph = Math.round((p.max_speed || 200) * 0.621);
            const maxRpmEverMph = p.max_speed_ever
                ? Math.round(p.max_speed_ever * 0.621) : 0;

            card.innerHTML = `
                <div class="profile-header" onclick="toggleProfileCard('${p.vin}')">
                    <div>
                        <div class="profile-title">${make} ${model} ${year}
                            ${p.is_active ? '<span class="profile-badge">ACTIVE</span>' : ''}
                        </div>
                        <div class="profile-subtitle">VIN: ${p.vin} · Last seen: ${lastSeen}</div>
                    </div>
                    <span class="profile-chevron" id="pchev-${p.vin}">▶</span>
                </div>
                <div class="profile-body" id="pbody-${p.vin}">
                    <div class="profile-rows">
                        <div class="profile-row">
                            <span class="profile-row-label">VIN</span>
                            <span class="profile-row-val" style="font-family:monospace;font-size:11px;">${p.vin}</span>
                        </div>
                        <div class="profile-row">
                            <span class="profile-row-label">Make / Model</span>
                            <span class="profile-row-val">${make} ${model}</span>
                        </div>
                        <div class="profile-row">
                            <span class="profile-row-label">Model Year</span>
                            <span class="profile-row-val">${year}</span>
                        </div>
                        <div class="profile-row">
                            <span class="profile-row-label">Engine Code</span>
                            <span class="profile-row-val" style="font-family:monospace;">${p.vin.length >= 8 ? p.vin[7] : '?'}</span>
                        </div>
                        <div class="profile-row">
                            <span class="profile-row-label">WMI</span>
                            <span class="profile-row-val" style="font-family:monospace;">${p.vin.substring(0,3)}</span>
                        </div>
                        <div class="profile-row">
                            <span class="profile-row-label">Max RPM (gauge)</span>
                            <span class="profile-row-val">${p.max_rpm || 8000}</span>
                        </div>
                        <div class="profile-row">
                            <span class="profile-row-label">Redline</span>
                            <span class="profile-row-val" style="color:var(--danger);">${p.redline_rpm || 6500} RPM</span>
                        </div>
                        <div class="profile-row">
                            <span class="profile-row-label">Max Speed (gauge)</span>
                            <span class="profile-row-val">${p.max_speed || 200} km/h / ${speedMph} mph</span>
                        </div>
                        <div class="profile-row">
                            <span class="profile-row-label">Total Sessions</span>
                            <span class="profile-row-val">${p.total_sessions}</span>
                        </div>
                        <div class="profile-row">
                            <span class="profile-row-label">All-Time Max RPM</span>
                            <span class="profile-row-val">${p.max_rpm_ever || '--'}</span>
                        </div>
                        <div class="profile-row">
                            <span class="profile-row-label">All-Time Max Speed</span>
                            <span class="profile-row-val">${p.max_speed_ever ? p.max_speed_ever + ' km/h / ' + maxRpmEverMph + ' mph' : '--'}</span>
                        </div>
                    </div>
                    ${!p.is_active ? `
                    <div class="profile-actions">
                        <button class="profile-delete-btn" onclick="deleteProfile('${p.vin}', event)">Delete Profile</button>
                    </div>` : ''}
                </div>
            `;
            container.appendChild(card);
        }

        function toggleProfileCard(vin) {
            const body = document.getElementById(`pbody-${vin}`);
            const chev = document.getElementById(`pchev-${vin}`);
            const expanded = body.classList.toggle('expanded');
            chev.textContent = expanded ? '▼' : '▶';
        }

        function deleteProfile(vin, event) {
            event.stopPropagation();
            if (!confirm(`Delete profile for VIN ${vin}?\n\nThis will remove all stored data for this vehicle.`)) return;
            fetch(`/api/profiles/${vin}`, { method: 'DELETE' })
                .then(r => r.json())
                .then(data => {
                    if (data.status === 'deleted') {
                        const card = document.getElementById(`profile-card-${vin}`);
                        if (card) card.remove();
                        const list = document.getElementById('profiles-list');
                        if (!list.querySelector('.profile-card')) {
                            list.innerHTML = '<div class="profiles-empty">No vehicle profiles stored yet.</div>';
                        }
                    } else {
                        alert(data.error || 'Delete failed');
                    }
                });
        }

        // ── Logs ──────────────────────────────────────────────────────────────
        let logsOffset = 0;
        const LOGS_PER_PAGE = 100;

        function toggleLogs(btn) {
            const section = document.getElementById('logs-section');
            const chevron = document.getElementById('logs-chevron');
            const visible = section.style.display !== 'none';
            if (visible) {
                section.style.display = 'none';
                chevron.textContent = '▶ collapsed';
            } else {
                section.style.display = 'block';
                chevron.textContent = '▼';
                if (logsOffset === 0) loadLogs(false);
            }
        }

        function loadLogs(append) {
            fetch(`/api/logs?limit=${LOGS_PER_PAGE}&offset=${logsOffset}`)
                .then(r => r.json())
                .then(data => {
                    const list = document.getElementById('logs-list');
                    if (!append) {
                        list.innerHTML = '';
                        list.scrollTop = 0;
                    }
                    data.entries.forEach(e => {
                        const div = document.createElement('div');
                        div.className = `log-entry ${e.level}`;
                        div.textContent = e.text;
                        list.appendChild(div);
                    });
                    if (!append) list.scrollTop = list.scrollHeight;
                    const shown = Math.min(logsOffset + LOGS_PER_PAGE, data.total);
                    document.getElementById('logs-meta').textContent = `${shown} / ${data.total} entries`;
                    const moreBtn = document.getElementById('logs-more-btn');
                    moreBtn.style.display = shown < data.total ? 'inline-block' : 'none';
                })
                .catch(() => {
                    document.getElementById('logs-meta').textContent = 'Failed to load logs';
                });
        }

        function loadMoreLogs() {
            logsOffset += LOGS_PER_PAGE;
            loadLogs(true);
        }

        function updateVehicleUI() {
            fetch('/api/vehicle').then(r => r.json()).then(v => {
                if (v.profile) {
                    vehicle = v.profile;
                    document.getElementById('vehicle-vin').textContent = v.vin || '--';
                    const make = v.profile.make || '';
                    const model = v.profile.model || '';
                    const year = v.profile.year || '';
                    document.getElementById('vehicle-model').textContent =
                        [make, model, year].filter(Boolean).join(' ') || 'Unknown';
                    document.getElementById('vehicle-redline').textContent = `${vehicle.max_rpm || 8000} / ${vehicle.redline_rpm || 6500} RPM`;
                    document.getElementById('vehicle-maxspeed').textContent =
                        `${vehicle.max_speed || 200} km/h / ${Math.round((vehicle.max_speed || 200) * 0.621)} mph`;
                }
                if (v.stats) {
                    const stats = v.stats;
                    document.getElementById('stats-sessions').textContent = stats.total_sessions || 0;
                    document.getElementById('stats-maxrpm').textContent = stats.max_rpm_ever || 0;
                    document.getElementById('stats-maxspeed').textContent = `${stats.max_speed_ever || 0} km/h`;
                    document.getElementById('stats-distance').textContent = `${(stats.total_distance_km || 0).toFixed(1)} km`;
                }
            }).catch(() => {});
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
        function showPage(page, btnElement) {
            document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
            document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
            document.getElementById(`page-${page}`).classList.add('active');
            if (btnElement) {
                btnElement.classList.add('active');
            }

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
                document.getElementById('session-maxrpm').textContent = status.session_max_rpm || 0;
                document.getElementById('session-maxspeed').textContent = `${status.session_max_speed || 0} km/h`;

                // Also update vehicle info
                updateVehicleUI();
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
                        if (msg.vehicle) {
                            vehicle = msg.vehicle;
                        }
                        if (msg.session_stats) {
                            sessionMax = msg.session_stats;
                        }
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
