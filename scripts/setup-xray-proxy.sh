#!/usr/bin/env bash
# Add or update AgentControl xray HTTP inbound (legacy wrapper).
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/agentcontrol}"
PROXY_PORT="${1:-30229}"

python3 "${INSTALL_DIR}/scripts/apply-xray-client.py" --import >/dev/null 2>&1 || true

python3 - <<PY
import json
import sys
from pathlib import Path

sys.path.insert(0, "${INSTALL_DIR}")
from xray_client import apply_client_settings, load_client_settings

settings = load_client_settings()
settings["enabled"] = True
settings["proxy_port"] = int("${PROXY_PORT}")
result = apply_client_settings(settings, restart=True, write_env=True)
print(json.dumps(result.get("settings", {}), indent=2))
PY
