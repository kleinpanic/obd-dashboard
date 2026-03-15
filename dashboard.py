#!/usr/bin/env python3
"""
OBD Web Dashboard - Real-time vehicle data viewer
Read-only, safe for driving.
"""

import obd
from flask import Flask, jsonify, render_template_string
from flask_cors import CORS
import threading
import time

app = Flask(__name__)
CORS(app)

# Global state
obd_connection = None
live_data = {}
supported_commands = []

# HTML template
DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>OBD Dashboard - Crosstrek 2021</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #0a0a0a; color: #fff; padding: 20px;
        }
        .header { text-align: center; margin-bottom: 20px; }
        .header h1 { font-size: 1.5em; color: #4ade80; }
        .status { font-size: 0.9em; color: #666; }
        
        .gauges { 
            display: grid; 
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 15px; margin-bottom: 20px;
        }
        .gauge {
            background: #1a1a1a; border-radius: 12px; padding: 20px;
            text-align: center; border: 1px solid #333;
        }
        .gauge-value { font-size: 2.5em; font-weight: bold; color: #4ade80; }
        .gauge-unit { font-size: 0.8em; color: #666; margin-top: 5px; }
        .gauge-label { font-size: 0.9em; color: #888; margin-top: 10px; }
        
        .warning { background: #2a1a1a; border-color: #f87171; }
        .warning .gauge-value { color: #f87171; }
        
        .sensors { 
            display: grid; 
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 10px;
        }
        .sensor {
            background: #1a1a1a; border-radius: 8px; padding: 12px;
            display: flex; justify-content: space-between;
            border: 1px solid #333;
        }
        .sensor-name { color: #888; }
        .sensor-value { color: #4ade80; font-weight: bold; }
        
        .refresh { text-align: center; color: #444; margin-top: 20px; font-size: 0.8em; }
    </style>
</head>
<body>
    <div class="header">
        <h1>🚗 Crosstrek 2021</h1>
        <div class="status" id="status">Connecting...</div>
    </div>
    
    <div class="gauges" id="gauges"></div>
    <div class="sensors" id="sensors"></div>
    
    <div class="refresh">Auto-refresh: 1s | <a href="/api/sensors" target="_blank">API</a></div>
    
    <script>
        const gaugeOrder = ['RPM', 'SPEED', 'COOLANT_TEMP', 'INTAKE_TEMP', 'THROTTLE_POS', 'FUEL_LEVEL', 'ENGINE_LOAD'];
        
        async function fetch() {
            const res = await fetch('/api/data');
            const data = await res.json();
            render(data);
        }
        
        function render(data) {
            document.getElementById('status').textContent = 
                data.connected ? '✓ Connected' : '✗ Disconnected';
            
            // Gauges
            const gauges = document.getElementById('gauges');
            gauges.innerHTML = gaugeOrder.map(name => {
                const val = data.data[name];
                if (!val) return '';
                const isWarning = (name === 'COOLANT_TEMP' && val.value > 100) ||
                                 (name === 'FUEL_LEVEL' && val.value < 15);
                return `
                    <div class="gauge ${isWarning ? 'warning' : ''}">
                        <div class="gauge-value">${val.display}</div>
                        <div class="gauge-unit">${val.unit}</div>
                        <div class="gauge-label">${name.replace(/_/g, ' ')}</div>
                    </div>
                `;
            }).join('');
            
            // All sensors
            const sensors = document.getElementById('sensors');
            sensors.innerHTML = Object.entries(data.data)
                .filter(([k]) => !gaugeOrder.includes(k))
                .map(([name, val]) => `
                    <div class="sensor">
                        <span class="sensor-name">${name.replace(/_/g, ' ')}</span>
                        <span class="sensor-value">${val.display} ${val.unit}</span>
                    </div>
                `).join('');
        }
        
        setInterval(fetch, 1000);
        fetch();
    </script>
</body>
</html>
"""

def connect_obd():
    global obd_connection, supported_commands
    ports = obd.scan_serial()
    if ports:
        obd_connection = obd.OBD(ports[0], protocol="3")
        if obd_connection.is_connected():
            supported_commands = obd_connection.supported_commands
            return True
    return False

def update_data():
    global live_data
    while True:
        if obd_connection and obd_connection.is_connected():
            for cmd in supported_commands:
                try:
                    response = obd_connection.query(cmd)
                    if not response.is_null():
                        val = response.value
                        live_data[cmd.name] = {
                            'value': float(val.magnitude) if hasattr(val, 'magnitude') else str(val),
                            'unit': str(val.units) if hasattr(val, 'units') else '',
                            'display': f"{float(val.magnitude):.1f}" if hasattr(val, 'magnitude') and isinstance(val.magnitude, (int, float)) else str(val)
                        }
                except:
                    pass
        time.sleep(0.5)

@app.route('/')
def index():
    return render_template_string(DASHBOARD_HTML)

@app.route('/api/data')
def api_data():
    return jsonify({
        'connected': obd_connection.is_connected() if obd_connection else False,
        'data': live_data
    })

@app.route('/api/sensors')
def api_sensors():
    return jsonify({
        'supported': [cmd.name for cmd in supported_commands],
        'count': len(supported_commands)
    })

if __name__ == '__main__':
    print("Connecting to OBD...")
    if connect_obd():
        print(f"✓ Connected! {len(supported_commands)} sensors available.")
        print("\nDashboard: http://localhost:5000")
        print("Press Ctrl+C to stop\n")
        
        # Start data updater thread
        updater = threading.Thread(target=update_data, daemon=True)
        updater.start()
        
        app.run(host='0.0.0.0', port=5000, debug=False)
    else:
        print("Failed to connect to OBD!")
