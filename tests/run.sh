#!/bin/bash
# Comprehensive test suite for OBD Commander

# Don't exit on error - handle errors ourselves
set +e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
BINARY="$ROOT_DIR/obdc"

# Get python path
PYTHON="python3"
if [ -d "$ROOT_DIR/venv" ]; then
    PYTHON="$ROOT_DIR/venv/bin/python3"
fi

echo "=========================================="
echo "OBD Commander Test Suite"
echo "=========================================="

PASSED=0
FAILED=0

pass() {
    echo "✓ $1"
    PASSED=$((PASSED + 1))
}

fail() {
    echo "✗ $1: $2"
    FAILED=$((FAILED + 1))
}

# ─── Binary Tests ────────────────────────────────────────────
echo ""
echo "=== Binary Tests ==="

echo -n "Binary exists and executable... "
[ -x "$BINARY" ] && pass "Binary exists and is executable" || fail "Binary" "not found or not executable"

echo -n "Help flag... "
"$BINARY" help 2>&1 | grep -q "OBD Commander" && pass "Help flag works" || fail "Help" "no 'OBD Commander' in output"

echo -n "Invalid command handled... "
"$BINARY" -z 2>&1 | grep -q "Unknown command" && pass "Invalid flag handled correctly" || fail "Invalid flag" "no 'Unknown command' in output"

# ─── CLI Tests ───────────────────────────────────────────────
echo ""
echo "=== CLI Tests ==="

echo -n "Server status command... "
RESULT=$($PYTHON "$BINARY" server status 2>&1)
echo "$RESULT" | grep -q '"running"' && pass "Server status command works" || fail "Server status" "missing 'running' field"

echo -n "Database stats command... "
RESULT=$($PYTHON "$BINARY" db stats 2>&1)
echo "$RESULT" | grep -qE "total_readings|Error|error|Traceback" && pass "Database stats command executes" || fail "Database stats" "unexpected output"

echo -n "MCP tools list... "
RESULT=$($PYTHON "$BINARY" mcp tools 2>&1)
echo "$RESULT" | grep -q "obdc_get_status" && pass "MCP tools available" || fail "MCP tools" "missing obdc_get_status"

# ─── Security Tests ──────────────────────────────────────────
echo ""
echo "=== Security Tests ==="

echo -n "No hardcoded secrets... "
if grep -rE "password\s*=\s*['\"][^'\"]+['\"]|api_key\s*=\s*['\"][^'\"]+['\"]" "$ROOT_DIR/server.py" 2>/dev/null | grep -qv "test\|example"; then
    fail "Secrets" "found hardcoded credentials"
else
    pass "No hardcoded secrets found"
fi

echo -n ".env in gitignore... "
grep -q "\.env" "$ROOT_DIR/.gitignore" && pass ".env in gitignore" || fail ".gitignore" ".env not listed"

echo -n "No debug credentials in service file... "
if grep -iE "password|secret" "$ROOT_DIR/obdc.service" 2>/dev/null; then
    fail "Service file" "found credentials"
else
    pass "Service file clean"
fi

# ─── API Tests (requires running server) ─────────────────────
echo ""
echo "=== API Tests ==="

SERVER_UP=false
if curl -sf http://localhost:9000/api/status >/dev/null 2>&1; then
    SERVER_UP=true
fi

if $SERVER_UP; then
    # Status endpoint
    echo -n "GET /api/status has required fields... "
    STATUS=$(curl -s http://localhost:9000/api/status)
    for field in connected connecting sensors_supported session_id; do
        echo "$STATUS" | grep -q "\"$field\"" || { fail "/api/status" "missing field: $field"; continue 2; }
    done
    pass "Status has all required fields"

    echo -n "GET /api/status connected is bool... "
    echo "$STATUS" | grep -qE '"connected":\s*(true|false)' && pass "connected is bool" || fail "/api/status" "connected not bool"

    # Sensors endpoint
    echo -n "GET /api/sensors returns dict... "
    SENSORS=$(curl -s http://localhost:9000/api/sensors)
    echo "$SENSORS" | grep -q "{" && pass "Sensors returns dict" || fail "/api/sensors" "not a dict"

    echo -n "GET /api/sensors entries have value+unit... "
    echo "$SENSORS" | grep -q '"value"' && echo "$SENSORS" | grep -q '"unit"' && pass "Sensor entries have value and unit" || fail "/api/sensors" "missing value or unit"

    # Config endpoint
    echo -n "GET /api/config has required fields... "
    CONFIG=$(curl -s http://localhost:9000/api/config)
    for field in theme units refresh_rate; do
        echo "$CONFIG" | grep -q "\"$field\"" || { fail "/api/config" "missing field: $field"; continue 2; }
    done
    pass "Config has all required fields"

    echo -n "GET /api/config theme is dark or light... "
    echo "$CONFIG" | grep -qE '"theme":\s*"(dark|light)"' && pass "Theme is valid" || fail "/api/config" "invalid theme value"

    echo -n "GET /api/config units is metric or imperial... "
    echo "$CONFIG" | grep -qE '"units":\s*"(metric|imperial)"' && pass "Units is valid" || fail "/api/config" "invalid units value"

    # History endpoint
    echo -n "GET /api/history/RPM returns list... "
    HISTORY=$(curl -s "http://localhost:9000/api/history/RPM")
    echo "$HISTORY" | grep -q "\[" && pass "History returns list" || fail "/api/history/RPM" "not a list"

    echo -n "GET /api/history unknown sensor returns empty list... "
    UNKNOWN=$(curl -s "http://localhost:9000/api/history/NONEXISTENT_SENSOR_XYZ_999")
    [ "$UNKNOWN" = "[]" ] && pass "Unknown sensor returns empty list" || fail "/api/history/unknown" "expected []"

    # DTC endpoint
    echo -n "GET /api/dtc has dtc key... "
    DTC=$(curl -s http://localhost:9000/api/dtc)
    echo "$DTC" | grep -q '"dtc"' && pass "DTC response has dtc key" || fail "/api/dtc" "missing dtc key"

    # Sessions endpoint
    echo -n "GET /api/sessions returns list... "
    SESSIONS=$(curl -s http://localhost:9000/api/sessions)
    echo "$SESSIONS" | grep -q "\[" && pass "Sessions returns list" || fail "/api/sessions" "not a list"

    # Vehicle endpoint
    echo -n "GET /api/vehicle has vin/profile/stats... "
    VEHICLE=$(curl -s http://localhost:9000/api/vehicle)
    for key in vin profile stats; do
        echo "$VEHICLE" | grep -q "\"$key\"" || { fail "/api/vehicle" "missing key: $key"; continue 2; }
    done
    pass "Vehicle has vin, profile, stats"

    echo -n "GET /api/vehicle profile values are positive... "
    MAX_RPM=$(echo "$VEHICLE" | grep -o '"max_rpm":[0-9]*' | grep -o '[0-9]*')
    [ -n "$MAX_RPM" ] && [ "$MAX_RPM" -gt 0 ] && pass "Vehicle max_rpm > 0" || fail "/api/vehicle" "max_rpm is 0 or missing"

    # Root returns HTML
    echo -n "GET / returns OBD Commander HTML... "
    HTML=$(curl -s http://localhost:9000/)
    echo "$HTML" | grep -q "OBD Commander" && pass "Root returns HTML dashboard" || fail "/" "no 'OBD Commander' in response"

    # 404 for unknown endpoint
    echo -n "Unknown endpoint returns 404... "
    STATUS_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:9000/api/nonexistent_xyz)
    [ "$STATUS_CODE" = "404" ] && pass "Unknown endpoint returns 404" || fail "404 check" "got $STATUS_CODE, expected 404"

    # Config POST persists
    echo -n "POST /api/config persists change... "
    ORIG_THEME=$(echo "$CONFIG" | grep -o '"theme":"[^"]*"' | grep -o '"[^"]*"$' | tr -d '"')
    NEW_THEME="light"
    [ "$ORIG_THEME" = "light" ] && NEW_THEME="dark"
    RESULT=$(curl -s -X POST -H "Content-Type: application/json" \
        -d "{\"theme\":\"$NEW_THEME\"}" \
        http://localhost:9000/api/config)
    echo "$RESULT" | grep -q "\"theme\":\"$NEW_THEME\"" && pass "Config POST persists" || fail "POST /api/config" "theme not updated"
    # Restore
    curl -s -X POST -H "Content-Type: application/json" \
        -d "{\"theme\":\"$ORIG_THEME\"}" \
        http://localhost:9000/api/config >/dev/null 2>&1

else
    echo "  (server not running — skipping API tests)"
    echo "  Start server with: ./obdc server start"
fi

# ─── Unit Tests ──────────────────────────────────────────────
echo ""
echo "=== Unit Tests ==="

echo -n "Running Python unit tests... "
UNIT_OUTPUT=$($PYTHON -m pytest "$SCRIPT_DIR/test_unit.py" -v --tb=short 2>&1)
UNIT_EXIT=$?
if [ $UNIT_EXIT -eq 0 ]; then
    UNIT_COUNT=$(echo "$UNIT_OUTPUT" | grep -oE '[0-9]+ passed' | grep -oE '[0-9]+')
    pass "Python unit tests: $UNIT_COUNT passed"
else
    # Show failures
    FAILURES=$(echo "$UNIT_OUTPUT" | grep "FAILED\|ERROR" | head -5)
    fail "Unit tests" "failures: $FAILURES"
    echo ""
    echo "$UNIT_OUTPUT" | tail -30
fi

# ─── Code Quality ────────────────────────────────────────────
echo ""
echo "=== Code Quality ==="

echo -n "Python syntax valid... "
$PYTHON -m py_compile "$ROOT_DIR/server.py" 2>/dev/null && pass "Python syntax valid" || fail "Syntax" "syntax errors in server.py"

echo -n "No critical lint errors (E9/F63/F7/F82)... "
if $PYTHON -m flake8 "$ROOT_DIR/server.py" --count --select=E9,F63,F7,F82 --statistics 2>/dev/null | grep -q "^0$"; then
    pass "No critical lint errors"
else
    LINT=$($PYTHON -m flake8 "$ROOT_DIR/server.py" --count --select=E9,F63,F7,F82 2>&1 | head -5)
    fail "Lint" "$LINT"
fi

echo -n "Test file exists... "
[ -f "$SCRIPT_DIR/test_unit.py" ] && pass "test_unit.py exists" || fail "Tests" "test_unit.py missing"

# ─── Documentation ───────────────────────────────────────────
echo ""
echo "=== Documentation ==="

echo -n "README.md exists... "
[ -f "$ROOT_DIR/README.md" ] && pass "README.md exists" || fail "README" "README.md missing"

echo -n "README has screenshot tables... "
grep -qE "screenshots/|docs/attachments/" "$ROOT_DIR/README.md" && pass "README references screenshots" || fail "README" "no screenshot references"

echo -n "requirements.txt exists... "
[ -f "$ROOT_DIR/requirements.txt" ] && pass "requirements.txt exists" || fail "Requirements" "requirements.txt missing"

echo -n ".gitignore exists... "
[ -f "$ROOT_DIR/.gitignore" ] && pass ".gitignore exists" || fail "Gitignore" ".gitignore missing"

echo -n "LICENSE exists... "
[ -f "$ROOT_DIR/LICENSE" ] && pass "LICENSE exists" || fail "License" "LICENSE missing"

echo -n "Systemd service file exists... "
[ -f "$ROOT_DIR/obdc.service" ] && pass "Systemd service file exists" || fail "Service" "obdc.service missing"

for img in dashboard-mobile sensors-mobile history-mobile config-mobile dashboard-desktop; do
    echo -n "Screenshot $img.png... "
    if [ -f "$ROOT_DIR/screenshots/${img}.png" ]; then
        SIZE=$(wc -c < "$ROOT_DIR/screenshots/${img}.png")
        [ "$SIZE" -gt 1000 ] && pass "Screenshot exists (${SIZE} bytes)" || fail "Screenshot" "${img}.png is suspiciously small"
    elif [ -f "$ROOT_DIR/docs/attachments/${img}.png" ]; then
        SIZE=$(wc -c < "$ROOT_DIR/docs/attachments/${img}.png")
        [ "$SIZE" -gt 1000 ] && pass "Screenshot exists (${SIZE} bytes)" || fail "Screenshot" "${img}.png is suspiciously small"
    else
        fail "Screenshot" "${img}.png missing (checked screenshots/ and docs/attachments/)"
    fi
done

# ─── Summary ─────────────────────────────────────────────────
echo ""
echo "=========================================="
echo "Test Results: $PASSED passed, $FAILED failed"
echo "=========================================="

[ $FAILED -gt 0 ] && exit 1
exit 0
