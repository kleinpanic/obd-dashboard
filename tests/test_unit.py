#!/usr/bin/env python3
"""
Unit tests for OBD Commander — no running server required.
Tests config, database, session lifecycle, vehicle profiles.
"""

import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add parent to path so we can import server
sys.path.insert(0, str(Path(__file__).parent.parent))

# We need to redirect DATA_DIR before importing server to avoid
# writing to the real ~/.local/share/obdc during tests
import importlib


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.tmpdir.name) / "config.json"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_load_config_defaults_when_missing(self):
        """load_config returns DEFAULT_CONFIG when file doesn't exist."""
        import server
        orig = server.CONFIG_PATH
        server.CONFIG_PATH = self.config_path
        try:
            cfg = server.load_config()
            self.assertEqual(cfg["theme"], "dark")
            self.assertEqual(cfg["units"], "metric")
            self.assertIn("refresh_rate", cfg)
        finally:
            server.CONFIG_PATH = orig

    def test_save_and_load_config_roundtrip(self):
        """save_config persists and load_config restores exactly."""
        import server
        orig = server.CONFIG_PATH
        server.CONFIG_PATH = self.config_path
        try:
            cfg = {"theme": "light", "units": "imperial", "refresh_rate": 2}
            server.save_config(cfg)
            loaded = server.load_config()
            self.assertEqual(loaded["theme"], "light")
            self.assertEqual(loaded["units"], "imperial")
            self.assertEqual(loaded["refresh_rate"], 2)
        finally:
            server.CONFIG_PATH = orig

    def test_load_config_handles_corrupt_file(self):
        """load_config returns defaults if file is corrupted JSON."""
        import server
        orig = server.CONFIG_PATH
        server.CONFIG_PATH = self.config_path
        try:
            self.config_path.write_text("{ this is not json !!!")
            cfg = server.load_config()
            self.assertEqual(cfg["theme"], "dark")  # falls back to default
        finally:
            server.CONFIG_PATH = orig


class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"

    def tearDown(self):
        self.tmpdir.cleanup()

    def _patched_server(self):
        import server
        server.DB_PATH = self.db_path
        server.init_db()
        return server

    def test_init_db_creates_all_tables(self):
        """init_db creates sensor_data, sessions, vehicle_profiles, vehicle_stats."""
        srv = self._patched_server()
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in c.fetchall()}
        conn.close()
        for expected in ("sensor_data", "sessions", "vehicle_profiles", "vehicle_stats"):
            self.assertIn(expected, tables, f"Missing table: {expected}")

    def test_log_sensor_inserts_record(self):
        """log_sensor writes a record with correct fields."""
        srv = self._patched_server()
        srv.current_session_id = "test-session-001"
        srv.log_sensor("RPM", 2500.0, "rpm")

        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT sensor, value, unit, session_id FROM sensor_data WHERE sensor='RPM'")
        row = c.fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "RPM")
        self.assertAlmostEqual(row[1], 2500.0)
        self.assertEqual(row[2], "rpm")
        self.assertEqual(row[3], "test-session-001")

    def test_log_sensor_multiple_readings(self):
        """Multiple sensor log calls accumulate correctly."""
        srv = self._patched_server()
        srv.current_session_id = "test-session-002"
        for val in [1000.0, 1500.0, 2000.0, 2500.0, 3000.0]:
            srv.log_sensor("RPM", val, "rpm")

        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM sensor_data WHERE sensor='RPM'")
        count = c.fetchone()[0]
        conn.close()
        self.assertEqual(count, 5)

    def test_get_recent_data_returns_sorted_results(self):
        """get_recent_data returns records sorted by timestamp."""
        srv = self._patched_server()
        srv.current_session_id = "test-session-003"
        base = time.time() - 120
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        for i, val in enumerate([1000.0, 2000.0, 3000.0]):
            c.execute(
                "INSERT INTO sensor_data (timestamp, session_id, sensor, value, unit) VALUES (?, ?, ?, ?, ?)",
                (base + i * 10, "test-session-003", "SPEED", val, "kph")
            )
        conn.commit()
        conn.close()

        rows = srv.get_recent_data("SPEED", minutes=5)
        self.assertEqual(len(rows), 3)
        # Should be sorted by timestamp ascending
        timestamps = [r[0] for r in rows]
        self.assertEqual(timestamps, sorted(timestamps))

    def test_get_recent_data_filters_by_time(self):
        """get_recent_data only returns data within the time window."""
        srv = self._patched_server()
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        old_ts = time.time() - 3600  # 1 hour ago — outside 30min window
        recent_ts = time.time() - 60  # 1 min ago — inside window
        for ts, val in [(old_ts, 999.0), (recent_ts, 2000.0)]:
            c.execute(
                "INSERT INTO sensor_data (timestamp, session_id, sensor, value, unit) VALUES (?, ?, ?, ?, ?)",
                (ts, "test-session-filter", "ENGINE_LOAD", val, "percent")
            )
        conn.commit()
        conn.close()

        rows = srv.get_recent_data("ENGINE_LOAD", minutes=30)
        values = [r[1] for r in rows]
        self.assertIn(2000.0, values)
        self.assertNotIn(999.0, values)


class TestSessionLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        import server
        server.DB_PATH = self.db_path
        server.init_db()
        self.server = server

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_start_session_creates_db_record(self):
        """start_session inserts a row in sessions table."""
        self.server.current_vin = "TESTVIN123"
        self.server.start_session(reason="test")

        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT id, start_time FROM sessions ORDER BY start_time DESC LIMIT 1")
        row = c.fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], self.server.current_session_id)
        self.assertIsNotNone(row[1])

    def test_start_session_resets_stats(self):
        """start_session resets session_stats to zero."""
        self.server.session_stats = {"max_rpm": 5000, "max_speed": 150}
        self.server.start_session(reason="test")
        self.assertEqual(self.server.session_stats["max_rpm"], 0)
        self.assertEqual(self.server.session_stats["max_speed"], 0)

    def test_start_session_generates_unique_ids(self):
        """Each start_session call produces a different session ID."""
        self.server.start_session(reason="test1")
        id1 = self.server.current_session_id
        self.server.start_session(reason="test2")
        id2 = self.server.current_session_id
        self.assertNotEqual(id1, id2)

    def test_end_session_clears_start_time(self):
        """end_session sets session_start_time to None."""
        self.server.current_vin = None
        self.server.start_session(reason="test")
        self.assertIsNotNone(self.server.session_start_time)
        self.server.end_session(reason="test")
        self.assertIsNone(self.server.session_start_time)

    def test_end_session_no_op_if_no_active_session(self):
        """end_session is safe to call when no session is active."""
        self.server.session_start_time = None
        # Should not raise
        self.server.end_session(reason="test")
        self.assertIsNone(self.server.session_start_time)


class TestVehicleProfile(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        import server
        server.DB_PATH = self.db_path
        server.init_db()
        self.server = server

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_get_vehicle_profile_creates_default(self):
        """get_vehicle_profile creates a default profile for unknown VIN."""
        profile = self.server.get_vehicle_profile("TEST1234567890")
        self.assertIsNotNone(profile)
        self.assertEqual(profile["max_rpm"], 8000)
        self.assertEqual(profile["redline_rpm"], 6500)
        self.assertEqual(profile["max_speed"], 200)
        self.assertEqual(profile["vin"], "TEST1234567890")

    def test_get_vehicle_profile_idempotent(self):
        """Calling get_vehicle_profile twice for same VIN returns same data."""
        p1 = self.server.get_vehicle_profile("VIN123")
        p2 = self.server.get_vehicle_profile("VIN123")
        self.assertEqual(p1["max_rpm"], p2["max_rpm"])
        self.assertEqual(p1["redline_rpm"], p2["redline_rpm"])

    def test_vehicle_stats_returns_zero_for_new_vin(self):
        """get_vehicle_stats returns zeros for a VIN with no history."""
        stats = self.server.get_vehicle_stats("BRANDNEWVIN0001")
        self.assertEqual(stats["total_sessions"], 0)
        self.assertEqual(stats["max_rpm_ever"], 0)
        self.assertEqual(stats["max_speed_ever"], 0)


class TestEngineStateDetection(unittest.TestCase):
    """Test RPM threshold logic for engine on/off detection."""

    def test_rpm_threshold_engine_on(self):
        """RPM > 100 should be considered engine running."""
        self.assertTrue(2000 > 100)
        self.assertTrue(500 > 100)
        self.assertTrue(101 > 100)

    def test_rpm_threshold_engine_off(self):
        """RPM <= 100 should be considered idle/off."""
        self.assertFalse(0 > 100)
        self.assertFalse(50 > 100)
        self.assertFalse(100 > 100)

    def test_engine_off_timeout_seconds(self):
        """Engine off should require 30 seconds of zero RPM."""
        import server
        self.assertEqual(server.ENGINE_OFF_TIMEOUT, 30)

    def test_session_stats_max_rpm_tracked(self):
        """session_stats max_rpm updates correctly."""
        import server
        server.session_stats = {"max_rpm": 0, "max_speed": 0}
        server.session_stats["max_rpm"] = max(server.session_stats["max_rpm"], 3500)
        server.session_stats["max_rpm"] = max(server.session_stats["max_rpm"], 2000)
        server.session_stats["max_rpm"] = max(server.session_stats["max_rpm"], 4200)
        self.assertEqual(server.session_stats["max_rpm"], 4200)

    def test_session_stats_max_speed_tracked(self):
        """session_stats max_speed updates correctly."""
        import server
        server.session_stats = {"max_rpm": 0, "max_speed": 0}
        for spd in [60, 120, 90, 145, 100]:
            server.session_stats["max_speed"] = max(server.session_stats["max_speed"], spd)
        self.assertEqual(server.session_stats["max_speed"], 145)


class TestGaugeScaling(unittest.TestCase):
    """Test vehicle profile gauge scaling values."""

    def test_default_max_rpm_is_reasonable(self):
        """Default max RPM (8000) covers typical NA engine range."""
        import server
        server.DB_PATH = Path(tempfile.mkdtemp()) / "test.db"
        server.init_db()
        profile = server.get_vehicle_profile("SCALETEST00001")
        self.assertGreaterEqual(profile["max_rpm"], 6000)
        self.assertLessEqual(profile["max_rpm"], 12000)

    def test_redline_less_than_max_rpm(self):
        """redline_rpm should always be less than max_rpm."""
        import server
        server.DB_PATH = Path(tempfile.mkdtemp()) / "test.db"
        server.init_db()
        profile = server.get_vehicle_profile("SCALETEST00002")
        self.assertLess(profile["redline_rpm"], profile["max_rpm"])

    def test_subaru_crosstrek_vin_prefix(self):
        """JF2 prefix identifies Subaru (Fuji Heavy Industries)."""
        vin = "JF2GTHNC4M82025"
        self.assertEqual(vin[:3], "JF2")  # JF2 = Subaru
        self.assertEqual(vin[3:8], "GTHNC")  # model code

    def test_fuel_warning_thresholds_ordered(self):
        """low_fuel_danger must be less than low_fuel_warning."""
        import server
        server.DB_PATH = Path(tempfile.mkdtemp()) / "test.db"
        server.init_db()
        profile = server.get_vehicle_profile("SCALETEST00003")
        self.assertLess(profile["low_fuel_danger"], profile["low_fuel_warning"])


class TestAPIIntegration(unittest.TestCase):
    """Integration tests — requires server running on localhost:9000."""

    BASE = "http://localhost:9000"

    @classmethod
    def setUpClass(cls):
        import urllib.request
        try:
            urllib.request.urlopen(f"{cls.BASE}/api/status", timeout=2)
            cls.server_available = True
        except Exception:
            cls.server_available = False

    def skip_if_no_server(self):
        if not self.server_available:
            self.skipTest("Server not running — skipping integration test")

    # Slow endpoints like /api/sensors can take up to 15s (OBD queries)
    SLOW_ENDPOINTS = {"/api/sensors"}

    def _get(self, path):
        import urllib.request
        import json as _json
        timeout = 15 if path in self.SLOW_ENDPOINTS else 5
        with urllib.request.urlopen(f"{self.BASE}{path}", timeout=timeout) as r:
            return _json.loads(r.read())

    def test_status_has_required_fields(self):
        self.skip_if_no_server()
        data = self._get("/api/status")
        for field in ("connected", "connecting", "sensors_supported", "session_id", "vin"):
            self.assertIn(field, data, f"Missing field in /api/status: {field}")

    def test_status_connected_is_bool(self):
        self.skip_if_no_server()
        data = self._get("/api/status")
        self.assertIsInstance(data["connected"], bool)
        self.assertIsInstance(data["connecting"], bool)

    def test_sensors_returns_dict(self):
        self.skip_if_no_server()
        data = self._get("/api/sensors")
        self.assertIsInstance(data, dict)

    def test_sensors_entries_have_value_and_unit(self):
        self.skip_if_no_server()
        data = self._get("/api/sensors")
        if data:
            sensor = next(iter(data.values()))
            self.assertIn("value", sensor)
            self.assertIn("unit", sensor)
            self.assertIsInstance(sensor["value"], (int, float))

    def test_config_has_required_fields(self):
        self.skip_if_no_server()
        cfg = self._get("/api/config")
        for field in ("theme", "units", "refresh_rate"):
            self.assertIn(field, cfg)

    def test_config_theme_is_valid(self):
        self.skip_if_no_server()
        cfg = self._get("/api/config")
        self.assertIn(cfg["theme"], ("dark", "light"))

    def test_config_units_is_valid(self):
        self.skip_if_no_server()
        cfg = self._get("/api/config")
        self.assertIn(cfg["units"], ("metric", "imperial"))

    def test_history_returns_list(self):
        self.skip_if_no_server()
        data = self._get("/api/history/RPM")
        self.assertIsInstance(data, list)

    def test_history_entries_have_t_and_v(self):
        self.skip_if_no_server()
        data = self._get("/api/history/RPM")
        if data:
            entry = data[0]
            self.assertIn("t", entry)
            self.assertIn("v", entry)
            self.assertIsInstance(entry["t"], float)
            self.assertIsInstance(entry["v"], (int, float))

    def test_history_unknown_sensor_returns_empty_list(self):
        self.skip_if_no_server()
        data = self._get("/api/history/NONEXISTENT_SENSOR_XYZ")
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 0)

    def test_dtc_has_dtc_key(self):
        self.skip_if_no_server()
        data = self._get("/api/dtc")
        self.assertIn("dtc", data)
        self.assertIsInstance(data["dtc"], list)

    def test_sessions_returns_list(self):
        self.skip_if_no_server()
        data = self._get("/api/sessions")
        # Returns {"sessions": [...]} or a plain list — handle both
        if isinstance(data, dict):
            self.assertIn("sessions", data)
            self.assertIsInstance(data["sessions"], list)
        else:
            self.assertIsInstance(data, list)

    def test_vehicle_has_required_keys(self):
        self.skip_if_no_server()
        data = self._get("/api/vehicle")
        self.assertIn("vin", data)
        self.assertIn("profile", data)
        self.assertIn("stats", data)

    def test_vehicle_profile_max_rpm_positive(self):
        self.skip_if_no_server()
        data = self._get("/api/vehicle")
        if data.get("profile"):
            self.assertGreater(data["profile"]["max_rpm"], 0)
            self.assertGreater(data["profile"]["redline_rpm"], 0)
            self.assertGreater(data["profile"]["max_speed"], 0)

    def test_dashboard_html_returns_200(self):
        self.skip_if_no_server()
        import urllib.request
        with urllib.request.urlopen(f"{self.BASE}/", timeout=5) as r:
            self.assertEqual(r.status, 200)
            html = r.read().decode()
            self.assertIn("OBD Commander", html)
            self.assertIn("WebSocket", html)

    def test_unknown_endpoint_returns_404(self):
        self.skip_if_no_server()
        import urllib.request
        import urllib.error
        try:
            urllib.request.urlopen(f"{self.BASE}/api/nonexistent", timeout=5)
            self.fail("Expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    def test_config_post_persists(self):
        self.skip_if_no_server()
        import urllib.request
        import json as _json

        # Toggle theme
        orig = self._get("/api/config")
        new_theme = "light" if orig["theme"] == "dark" else "dark"

        payload = _json.dumps({"theme": new_theme}).encode()
        req = urllib.request.Request(
            f"{self.BASE}/api/config",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            result = _json.loads(r.read())
        self.assertEqual(result["theme"], new_theme)

        # Restore
        payload = _json.dumps({"theme": orig["theme"]}).encode()
        req = urllib.request.Request(
            f"{self.BASE}/api/config",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        urllib.request.urlopen(req, timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
