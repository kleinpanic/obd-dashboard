-- Vehicle profiles table
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
    danger_temp REAL DEFAULT 115,
    low_fuel_warning REAL DEFAULT 25,
    low_fuel_danger REAL DEFAULT 15,
    created_at REAL,
    updated_at REAL
);

-- Session stats (aggregated from sensor_data)
CREATE TABLE IF NOT EXISTS session_stats (
    session_id TEXT PRIMARY KEY,
    vin TEXT,
    start_time REAL,
    end_time REAL,
    duration_seconds INTEGER,
    distance_km REAL DEFAULT 0,
    max_rpm INTEGER DEFAULT 0,
    avg_rpm REAL DEFAULT 0,
    max_speed INTEGER DEFAULT 0,
    avg_speed REAL DEFAULT 0,
    max_engine_load REAL DEFAULT 0,
    avg_engine_load REAL DEFAULT 0,
    max_coolant_temp REAL,
    fuel_start REAL,
    fuel_end REAL,
    fuel_used REAL,
    FOREIGN KEY (vin) REFERENCES vehicle_profiles(vin)
);

-- Create indexes
CREATE INDEX IF NOT EXISTS idx_session_stats_vin ON session_stats(vin);
CREATE INDEX IF NOT EXISTS idx_session_stats_start ON session_stats(start_time);
