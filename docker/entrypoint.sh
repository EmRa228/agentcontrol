#!/usr/bin/env bash
set -euo pipefail

CONFIG_DIR=/etc/agentcontrol
STATE_DIR=/var/lib/agentcontrol

mkdir -p "${CONFIG_DIR}" "${STATE_DIR}"

if [[ ! -f "${CONFIG_DIR}/config.yaml" ]]; then
  cp /app/config.example.yaml "${CONFIG_DIR}/config.yaml"
fi

AGENT_PATH="$(command -v agent 2>/dev/null || true)"
if [[ -n "${AGENT_PATH}" ]]; then
  python3 - <<PY
import yaml
from pathlib import Path

path = Path("${CONFIG_DIR}/config.yaml")
cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
cfg["agent_bin"] = "${AGENT_PATH}"
path.write_text(yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False), encoding="utf-8")
PY
fi

exec python /app/app.py
