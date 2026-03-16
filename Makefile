# OBD Commander — Makefile
# Supports: x86_64, aarch64 (RPi4/5), armv7l (RPi3)
# FOSS (MIT License) — no proprietary dependencies

.PHONY: all install install-deps install-user install-service uninstall uninstall-user \
        venv test man audit clean help

SHELL       := /bin/bash
PYTHON      ?= python3
PIP         ?= $(VENV_DIR)/bin/pip
PYTEST      ?= $(VENV_DIR)/bin/pytest
PREFIX      ?= /usr/local
BINDIR      := $(PREFIX)/bin
MANDIR      := $(PREFIX)/share/man/man1
VENV_DIR    ?= venv

# User-local install paths (no sudo needed)
USER_BINDIR  := $(HOME)/.local/bin
USER_MANDIR  := $(HOME)/.local/share/man/man1

# Detect architecture
ARCH := $(shell uname -m)
OS   := $(shell uname -s)

# ── Targets ──────────────────────────────────────────────────────────────────

all: venv
	@echo "OBD Commander ready. Run: source $(VENV_DIR)/bin/activate && ./obdc server start"
	@echo "Architecture: $(ARCH)"

## install-deps — Install system packages (requires apt/pacman/dnf/brew)
install-deps:
	@echo "Detected OS: $(OS)  Arch: $(ARCH)"
	@if [ "$(OS)" = "Darwin" ]; then \
	    echo "macOS: installing via brew..."; \
	    brew install python3 libusb; \
	elif command -v apt-get >/dev/null 2>&1; then \
	    echo "Debian/Ubuntu/Raspberry Pi OS: installing via apt..."; \
	    sudo apt-get update -qq && sudo apt-get install -y \
	        python3 python3-pip python3-venv python3-dev \
	        libusb-1.0-0 libusb-1.0-0-dev \
	        udev \
	        build-essential; \
	elif command -v pacman >/dev/null 2>&1; then \
	    echo "Arch Linux: installing via pacman..."; \
	    sudo pacman -S --noconfirm python python-pip python-virtualenv \
	        libusb udev base-devel; \
	elif command -v dnf >/dev/null 2>&1; then \
	    echo "Fedora/RHEL: installing via dnf..."; \
	    sudo dnf install -y python3 python3-pip python3-venv python3-devel \
	        libusb1 libusb1-devel udev; \
	else \
	    echo "Unknown package manager. Install manually: python3, python3-pip, python3-venv, libusb-1.0-0"; \
	    exit 1; \
	fi
	@echo ""
	@echo "Add yourself to the dialout group (required for USB OBD adapter):"
	@echo "  sudo usermod -aG dialout $$USER"
	@echo "Then log out and back in (or run: newgrp dialout)"

## venv — Create Python virtual environment and install deps
venv: $(VENV_DIR)/bin/python

$(VENV_DIR)/bin/python: requirements.txt
	@echo "Creating venv..."
	$(PYTHON) -m venv $(VENV_DIR)
	$(PIP) install --upgrade pip -q
	$(PIP) install -r requirements.txt
	@echo "Venv ready at $(VENV_DIR)/"

## install — Install system-wide (requires sudo)
install: venv man/obdc.1
	@echo "Installing to $(PREFIX)..."
	install -d $(BINDIR)
	install -m 755 obdc $(BINDIR)/obdc
	install -d $(MANDIR)
	install -m 644 man/obdc.1 $(MANDIR)/obdc.1
	gzip -f $(MANDIR)/obdc.1
	@echo "Installed. Run: obdc server start"

## install-user — Install to ~/.local (no sudo)
install-user: venv man/obdc.1
	@echo "Installing to $(USER_BINDIR)..."
	install -d $(USER_BINDIR)
	install -m 755 obdc $(USER_BINDIR)/obdc
	install -d $(USER_MANDIR)
	install -m 644 man/obdc.1 $(USER_MANDIR)/obdc.1
	gzip -f $(USER_MANDIR)/obdc.1
	@echo "Installed to $(USER_BINDIR)/obdc"
	@echo "Make sure $(USER_BINDIR) is in your PATH"

## install-service — Install systemd service system-wide (requires sudo)
install-service: install
	@echo "Installing systemd service (system-wide)..."
	@sed "s|ExecStart=.*|ExecStart=$(BINDIR)/obdc server start|g" obdc.service \
	    > /tmp/obdc.service.tmp
	sudo install -m 644 /tmp/obdc.service.tmp /etc/systemd/system/obdc.service
	sudo systemctl daemon-reload
	@echo "Enable with: sudo systemctl enable --now obdc"

## install-service-user — Install user-level systemd service (NO sudo)
install-service-user: install-user
	@echo "Installing user systemd service..."
	@mkdir -p ~/.config/systemd/user
	@echo "[Unit]" > ~/.config/systemd/user/obdc.service
	@echo "Description=OBD Commander Car Computer" >> ~/.config/systemd/user/obdc.service
	@echo "After=network.target" >> ~/.config/systemd/user/obdc.service
	@echo "" >> ~/.config/systemd/user/obdc.service
	@echo "[Service]" >> ~/.config/systemd/user/obdc.service
	@echo "Type=simple" >> ~/.config/systemd/user/obdc.service
	@echo "WorkingDirectory=$(PWD)" >> ~/.config/systemd/user/obdc.service
	@echo "Environment=PATH=$(PWD)/venv/bin:$(PATH)" >> ~/.config/systemd/user/obdc.service
	@echo "ExecStart=$(PWD)/venv/bin/python server.py --host 0.0.0.0 --port 9000" >> ~/.config/systemd/user/obdc.service
	@echo "Restart=on-failure" >> ~/.config/systemd/user/obdc.service
	@echo "RestartSec=5" >> ~/.config/systemd/user/obdc.service
	@echo "" >> ~/.config/systemd/user/obdc.service
	@echo "[Install]" >> ~/.config/systemd/user/obdc.service
	@echo "WantedBy=default.target" >> ~/.config/systemd/user/obdc.service
	systemctl --user daemon-reload
	@echo ""
	@echo "Enable with: systemctl --user enable --now obdc"
	@echo ""
	@echo "Note: User services run after login. For auto-start on boot,"
	@echo "enable lingering: loginctl enable-linger $$USER"

## man — View the man page locally
man: man/obdc.1
	man ./man/obdc.1 2>/dev/null || groff -man -Tascii man/obdc.1 | less

## test — Run the full test suite
test: venv
	$(PYTEST) tests/test_unit.py -v --tb=short
	@chmod +x tests/run.sh && ./tests/run.sh

## audit — Check for secrets, insecure patterns, and validate structure
audit:
	@echo "=== Security Audit ==="
	@echo ""
	@echo "--- Checking for hardcoded secrets ---"
	@! grep -rn \
	    -e "password\s*=\s*['\"][^'\"]\+['\"]" \
	    -e "api_key\s*=\s*['\"][^'\"]\+['\"]" \
	    -e "secret\s*=\s*['\"][^'\"]\+['\"]" \
	    -e "token\s*=\s*['\"][^'\"]\+['\"]" \
	    --include="*.py" --include="*.json" --include="*.yml" \
	    --exclude-dir=venv --exclude-dir=.git \
	    . 2>/dev/null && echo "  OK — no hardcoded secrets" || echo "  WARN — review above"
	@echo ""
	@echo "--- Checking .gitignore for sensitive files ---"
	@for f in .env secrets.json *.pem *.key venv/ __pycache__/; do \
	    grep -q "$$f" .gitignore && echo "  ✓ $$f" || echo "  MISSING in .gitignore: $$f"; \
	done
	@echo ""
	@echo "--- Checking for no .env in repo ---"
	@if git ls-files | grep -q "\.env$$"; then \
	    echo "  FAIL — .env is tracked!"; exit 1; \
	else echo "  OK — no .env tracked"; fi
	@echo ""
	@echo "--- Architecture compatibility ---"
	@echo "  Current: $(ARCH)"
	@echo "  Supported: x86_64  aarch64  armv7l"
	@if echo "x86_64 aarch64 armv7l" | grep -qw "$(ARCH)"; then \
	    echo "  ✓ Architecture supported"; \
	else \
	    echo "  WARN — untested architecture $(ARCH)"; \
	fi
	@echo ""
	@echo "--- Offline capability check ---"
	@! grep -rn "requests\.\|urllib\.request\|http\.client" \
	    --include="*.py" --exclude-dir=venv server.py 2>/dev/null \
	    && echo "  ✓ No external HTTP calls in server.py" \
	    || echo "  INFO — HTTP calls present (ensure offline fallback)"
	@echo ""
	@echo "=== Audit complete ==="

## uninstall — Remove system-wide install
uninstall:
	rm -f $(BINDIR)/obdc
	rm -f $(MANDIR)/obdc.1.gz
	@echo "Uninstalled"

## uninstall-user — Remove user-local install
uninstall-user:
	rm -f $(USER_BINDIR)/obdc
	rm -f $(USER_MANDIR)/obdc.1.gz
	@echo "Uninstalled from $(USER_BINDIR)"

## clean — Remove build artifacts
clean:
	rm -rf $(VENV_DIR) __pycache__ dist build *.egg-info
	find . -name "*.pyc" -delete
	find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null; true

## help — Show available targets
help:
	@grep -E '^## ' Makefile | sed 's/## /  make /'
