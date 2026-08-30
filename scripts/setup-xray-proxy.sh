#!/usr/bin/env bash
# Add or update an HTTP inbound on the host xray for AgentControl install/runtime proxy.
set -euo pipefail

PROXY_PORT="${1:-30229}"
XRAY_CONFIG="${XRAY_CONFIG:-/usr/local/etc/xray/config.json}"
TAG="agentcontrol-http-in"
LISTEN="${XRAY_LISTEN:-127.0.0.1}"

log() { echo "==> $*"; }

if [[ ! -f "${XRAY_CONFIG}" ]]; then
  echo "xray config not found: ${XRAY_CONFIG}" >&2
  echo "Set XRAY_CONFIG to your config path and retry." >&2
  exit 1
fi

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi

BACKUP="${XRAY_CONFIG}.before-agentcontrol-$(date +%Y%m%d-%H%M%S)"
cp -a "${XRAY_CONFIG}" "${BACKUP}"
log "Backed up xray config to ${BACKUP}"

python3 - <<PY
import json
from pathlib import Path

config_path = Path("${XRAY_CONFIG}")
port = int("${PROXY_PORT}")
tag = "${TAG}"
listen = "${LISTEN}"

config = json.loads(config_path.read_text(encoding="utf-8"))
inbounds = config.setdefault("inbounds", [])

updated = False
for inbound in inbounds:
    if inbound.get("tag") == tag:
        inbound["listen"] = listen
        inbound["port"] = port
        inbound["protocol"] = "http"
        inbound.setdefault("settings", {})
        inbound["settings"].update({"allowTransparent": False, "userLevel": 0})
        updated = True
        break

if not updated:
    inbounds.append(
        {
            "tag": tag,
            "listen": listen,
            "port": port,
            "protocol": "http",
            "settings": {"allowTransparent": False, "userLevel": 0},
        }
    )

config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
print(f"xray inbound {tag} -> {listen}:{port}")
PY

restart_xray() {
  if systemctl is-active --quiet xray 2>/dev/null; then
    systemctl restart xray
    return
  fi
  if systemctl is-active --quiet xray.service 2>/dev/null; then
    systemctl restart xray.service
    return
  fi
  if command -v xray &>/dev/null; then
    pkill -HUP xray 2>/dev/null || true
  fi
}

log "Restarting xray"
restart_xray
sleep 1

if ! ss -tln | grep -q ":${PROXY_PORT} "; then
  echo "WARNING: port ${PROXY_PORT} is not listening yet. Check xray logs." >&2
else
  log "Proxy listening on ${LISTEN}:${PROXY_PORT}"
fi
