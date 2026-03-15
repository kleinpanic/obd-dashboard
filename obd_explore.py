#!/usr/bin/env python3
"""
OBD Explorer - Read-only OBD-II scanner for Subaru Crosstrek 2021
Safe exploration of available sensors and PIDs.
"""

import obd
import sys

def main():
    print("=" * 60)
    print("OBD-II Explorer - Read Only Mode")
    print("=" * 60)
    
    # List available ports
    ports = obd.scan_serial()
    print(f"\nAvailable ports: {ports}")
    
    if not ports:
        print("No OBD ports found!")
        sys.exit(1)
    
    # Connect to first available port
    port = ports[0]
    print(f"\nConnecting to {port}...")
    
    connection = obd.OBD(port)
    
    if not connection.is_connected():
        print("Failed to connect!")
        sys.exit(1)
    
    print(f"✓ Connected!")
    print(f"  Protocol: {connection.protocol_name()}")
    
    # Get supported commands
    print("\n" + "=" * 60)
    print("SUPPORTED SENSORS (Read-Only Scan)")
    print("=" * 60)
    
    supported = connection.supported_commands
    print(f"\nTotal supported commands: {len(supported)}")
    
    # Group by category
    categories = {}
    for cmd in supported:
        cat = cmd.name.split('_')[0] if '_' in cmd.name else 'OTHER'
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(cmd)
    
    for cat, cmds in sorted(categories.items()):
        print(f"\n[{cat}] - {len(cmds)} sensors")
        for cmd in cmds[:10]:  # Show first 10 per category
            print(f"  • {cmd.name} ({cmd.pid})")
        if len(cmds) > 10:
            print(f"  ... and {len(cmds) - 10} more")
    
    # Test a few safe reads
    print("\n" + "=" * 60)
    print("LIVE DATA SAMPLE (Safe Reads)")
    print("=" * 60)
    
    safe_commands = [
        obd.commands.RPM,
        obd.commands.SPEED,
        obd.commands.COOLANT_TEMP,
        obd.commands.INTAKE_TEMP,
        obd.commands.THROTTLE_POS,
        obd.commands.FUEL_LEVEL,
        obd.commands.ENGINE_LOAD,
    ]
    
    for cmd in safe_commands:
        if cmd in supported:
            response = connection.query(cmd)
            if not response.is_null():
                print(f"  {cmd.name}: {response.value}")
            else:
                print(f"  {cmd.name}: (no data)")
    
    connection.close()
    print("\n✓ Connection closed safely")

if __name__ == "__main__":
    main()
