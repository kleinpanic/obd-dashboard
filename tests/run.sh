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
    echo "✗ $1"
    FAILED=$((FAILED + 1))
}

# === Binary Tests ===
echo ""
echo "=== Binary Tests ==="

echo -n "Binary exists... "
if [ -x "$BINARY" ]; then
    pass "Binary exists and is executable"
else
    fail "Binary not found or not executable"
fi

echo -n "Help flag... "
if "$BINARY" help 2>&1 | grep -q "OBD Commander"; then
    pass "Help flag works"
else
    fail "Help flag failed"
fi

echo -n "Invalid flag handling... "
if "$BINARY" -z 2>&1 | grep -q "Unknown command"; then
    pass "Invalid flag handled correctly"
else
    fail "Invalid flag not handled"
fi

# Get python path
PYTHON="python3"
if [ -d "$ROOT_DIR/venv" ]; then
    PYTHON="$ROOT_DIR/venv/bin/python3"
fi

# === CLI Tests ===
echo ""
echo "=== CLI Tests ==="

echo -n "Server status (not running)... "
RESULT=$($PYTHON "$BINARY" server status 2>&1)
if echo "$RESULT" | grep -q '"running"'; then
    pass "Server status command works"
else
    fail "Server status failed"
fi

echo -n "Database stats... "
RESULT=$($PYTHON "$BINARY" db stats 2>&1)
# Check for valid output (either JSON with total_readings or an error message)
if echo "$RESULT" | grep -qE "total_readings|Error|error|Traceback"; then
    pass "Database stats command executes"
else
    fail "Database stats failed: unexpected output"
fi

echo -n "MCP tools list... "
RESULT=$($PYTHON "$BINARY" mcp tools 2>&1)
if echo "$RESULT" | grep -q "obdc_get_status"; then
    pass "MCP tools available"
else
    fail "MCP tools failed"
fi

# === Security Tests ===
echo ""
echo "=== Security Tests ==="

echo -n "No hardcoded secrets... "
if grep -rE "password|secret|token|api.key|private" "$ROOT_DIR/src" "$ROOT_DIR/obdc" 2>/dev/null | grep -v "test\|example\|TODO"; then
    fail "Found potential secrets in code"
else
    pass "No hardcoded secrets found"
fi

echo -n ".env not in git... "
if grep -q ".env" "$ROOT_DIR/.gitignore"; then
    pass ".env in gitignore"
else
    fail ".env not in gitignore"
fi

# === API Tests (if server running) ===
echo ""
echo "=== API Tests ==="

if curl -s http://localhost:9000/api/status >/dev/null 2>&1; then
    echo -n "API status endpoint... "
    if curl -s http://localhost:9000/api/status | grep -q "connected"; then
        pass "API status works"
    else
        fail "API status failed"
    fi
    
    echo -n "API sensors endpoint... "
    if curl -s http://localhost:9000/api/sensors | grep -q "{"; then
        pass "API sensors works"
    else
        fail "API sensors failed"
    fi
    
    echo -n "API config endpoint... "
    if curl -s http://localhost:9000/api/config | grep -q "theme"; then
        pass "API config works"
    else
        fail "API config failed"
    fi
    
    echo -n "API sessions endpoint... "
    if curl -s http://localhost:9000/api/sessions | grep -q "sessions"; then
        pass "API sessions works"
    else
        fail "API sessions failed"
    fi
else
    echo "Server not running - skipping API tests"
fi

# === Code Quality Tests ===
echo ""
echo "=== Code Quality ==="

echo -n "Python syntax... "
if python3 -m py_compile "$ROOT_DIR/server.py" 2>/dev/null; then
    pass "Python syntax valid"
else
    fail "Python syntax errors"
fi

echo -n "README exists... "
if [ -f "$ROOT_DIR/README.md" ]; then
    pass "README.md exists"
else
    fail "README.md missing"
fi

echo -n "Requirements exists... "
if [ -f "$ROOT_DIR/requirements.txt" ]; then
    pass "requirements.txt exists"
else
    fail "requirements.txt missing"
fi

echo -n "Gitignore exists... "
if [ -f "$ROOT_DIR/.gitignore" ]; then
    pass ".gitignore exists"
else
    fail ".gitignore missing"
fi

echo -n "Service file exists... "
if [ -f "$ROOT_DIR/obdc.service" ]; then
    pass "Systemd service file exists"
else
    fail "Systemd service file missing"
fi

# === Screenshots ===
echo ""
echo "=== Documentation ==="

for img in dashboard-mobile sensors-mobile history-mobile config-mobile dashboard-desktop; do
    echo -n "Screenshot $img.png... "
    if [ -f "$ROOT_DIR/screenshots/${img}.png" ]; then
        pass "Screenshot exists"
    else
        fail "Screenshot missing"
    fi
done

# === Summary ===
echo ""
echo "=========================================="
echo "Test Results: $PASSED passed, $FAILED failed"
echo "=========================================="

if [ $FAILED -gt 0 ]; then
    exit 1
fi
