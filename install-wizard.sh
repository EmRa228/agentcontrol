#!/usr/bin/env bash
# Interactive host install wizard for AgentControl (systemd — never Docker).
# Workers must inherit host /var/run/docker.sock; the panel never runs in a container.
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/agentcontrol}"
CONFIG_DIR="${CONFIG_DIR:-/etc/agentcontrol}"
STATE_DIR="${STATE_DIR:-/var/lib/agentcontrol}"
DEFAULT_PROXY_PORT="${DEFAULT_PROXY_PORT:-30229}"
DEFAULT_SCAN_ROOT="${DEFAULT_SCAN_ROOT:-/root}"

log() { echo "==> $*"; }

need_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "Run as root: sudo $0"
    exit 1
  fi
}

stop_docker_panel() {
  if ! command -v docker &>/dev/null; then
    return
  fi
  if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx agentcontrol; then
    log "Stopping legacy AgentControl Docker container"
    docker stop agentcontrol 2>/dev/null || true
    docker rm agentcontrol 2>/dev/null || true
  fi
  if [[ -f "${INSTALL_DIR}/docker-compose.yml" ]]; then
    (cd "${INSTALL_DIR}" && docker compose down --remove-orphans 2>/dev/null) || true
  fi
}

write_config() {
  local scan_root="$1"
  mkdir -p "${CONFIG_DIR}" "${STATE_DIR}"
  if [[ ! -f "${CONFIG_DIR}/config.yaml" ]]; then
    cp "${INSTALL_DIR}/config.example.yaml" "${CONFIG_DIR}/config.yaml"
  fi
  python3 - <<PY
import yaml
from pathlib import Path

path = Path("${CONFIG_DIR}/config.yaml")
cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
cfg["scan_root"] = "${scan_root}"
cfg["state_dir"] = "${STATE_DIR}"
path.write_text(yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False), encoding="utf-8")
PY
}

normalize_network_mode() {
  local raw="$1"
  raw="$(echo "${raw}" | tr -d '[:space:]')"
  case "${raw}" in
    1|direct|Direct|DIRECT) echo "1" ;;
    2|proxy|Proxy|PROXY|xray|x2ray) echo "2" ;;
    "") echo "1" ;;
    *) echo "1" ;;
  esac
}

prompt_network_mode() {
  local choice
  echo "" >&2
  echo "=== AgentControl install wizard (host systemd) ===" >&2
  echo "" >&2
  echo "Network mode for Cursor agent traffic:" >&2
  echo "  1) Direct (no proxy) — default" >&2
  echo "  2) Via xray HTTP proxy (restricted networks)" >&2
  if [[ -n "${AGENTCONTROL_NETWORK_MODE:-}" ]]; then
    choice="$(normalize_network_mode "${AGENTCONTROL_NETWORK_MODE}")"
    echo "Using AGENTCONTROL_NETWORK_MODE=${choice}" >&2
  elif [[ -t 0 ]]; then
    read -rp "Choice [1/2] (default 1): " choice
    choice="$(normalize_network_mode "${choice:-1}")"
  else
    choice="1"
    log "Non-interactive mode: defaulting to direct (1)"
  fi
  echo "${choice}"
}

configure_direct_mode() {
  log "Direct mode — disabling xray proxy for Cursor traffic"
  python3 - <<PY
import json, sys
sys.path.insert(0, "${INSTALL_DIR}")
from xray_client import set_proxy_enabled

result = set_proxy_enabled(False, restart=False)
print(json.dumps(result, indent=2))
if not result.get("ok"):
    sys.exit(1)
PY
}

prompt_proxy_port() {
  local port
  if [[ -n "${AGENTCONTROL_PROXY_PORT:-}" ]]; then
    echo "${AGENTCONTROL_PROXY_PORT}"
    return
  fi
  if [[ -t 0 ]]; then
    read -rp "Proxy port [${DEFAULT_PROXY_PORT}]: " port
    echo "${port:-${DEFAULT_PROXY_PORT}}"
  else
    echo "${DEFAULT_PROXY_PORT}"
  fi
}

prompt_scan_root() {
  local root
  if [[ -n "${SCAN_ROOT:-}" ]]; then
    echo "${SCAN_ROOT}"
    return
  fi
  if [[ -t 0 ]]; then
    read -rp "Project scan root [${DEFAULT_SCAN_ROOT}]: " root
    echo "${root:-${DEFAULT_SCAN_ROOT}}"
  else
    echo "${DEFAULT_SCAN_ROOT}"
  fi
}

optional_secret() {
  local var_name="$1"
  local prompt="$2"
  local file_path="$3"
  local value="${!var_name:-}"
  if [[ -n "${value}" ]]; then
    printf '%s' "${value}" > "${file_path}"
    chmod 600 "${file_path}"
    log "Wrote ${file_path}"
    return
  fi
  if [[ -s "${file_path}" ]]; then
    log "Keeping existing ${file_path}"
    return
  fi
  if [[ -t 0 ]]; then
    echo ""
    read -rsp "${prompt} (optional, Enter to skip): " value
    echo ""
    if [[ -n "${value}" ]]; then
      printf '%s' "${value}" > "${file_path}"
      chmod 600 "${file_path}"
      log "Wrote ${file_path}"
    fi
  fi
}

prompt_field() {
  local label="$1"
  local default="$2"
  local var_name="$3"
  local value="${!var_name:-}"
  if [[ -n "${value}" ]]; then
    echo "${value}"
    return
  fi
  if [[ -t 0 ]]; then
    read -rp "${label} [${default}]: " value
    echo "${value:-${default}}"
  else
    echo "${default}"
  fi
}

configure_xray_client() {
  local proxy_port="$1"
  log "Configuring xray client (VLESS+Reality outbound + local HTTP proxy)"

  python3 "${INSTALL_DIR}/scripts/apply-xray-client.py" --import >/dev/null 2>&1 || true

  if [[ -n "${XRAY_SHARE_URL:-}" || -n "${XRAY_VLESS_URL:-}" ]]; then
    local share_url="${XRAY_SHARE_URL:-${XRAY_VLESS_URL}}"
    log "Applying xray client from share URL"
    python3 - <<PY
import json, sys
sys.path.insert(0, "${INSTALL_DIR}")
from xray_client import apply_client_settings, merge_share_url, load_client_settings

settings = merge_share_url(load_client_settings(), """${share_url}""")
settings["enabled"] = True
settings["proxy_port"] = int("${proxy_port}")
result = apply_client_settings(settings, restart=True, write_env=True)
print(json.dumps(result, indent=2))
if not result.get("listening"):
    sys.exit(1)
PY
    return
  fi

  if [[ "${XRAY_IMPORT_ONLY:-}" == "1" ]] || { [[ ! -t 0 ]] && [[ -z "${XRAY_ADDRESS:-}" ]]; }; then
    log "Applying xray client from existing config / saved settings"
    python3 - <<PY
import json, sys
sys.path.insert(0, "${INSTALL_DIR}")
from xray_client import apply_client_settings, load_client_settings

settings = load_client_settings()
settings["enabled"] = True
settings["proxy_port"] = int("${proxy_port}")
result = apply_client_settings(settings, restart=True, write_env=True)
print(json.dumps(result, indent=2))
if not result.get("listening"):
    sys.exit(1)
PY
    return
  fi

  local address port uuid server_name public_key short_id fingerprint flow share_url
  if [[ -t 0 ]] && [[ -z "${XRAY_ADDRESS:-}" ]]; then
    echo "" >&2
    echo "xray client outbound (upstream proxy server):" >&2
    echo "Paste a share link (vless://, trojan://, vmess://, ss://), or press Enter for manual fields." >&2
    read -r -p "Share URL: " share_url || true
    share_url="$(echo "${share_url}" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    if [[ -n "${share_url}" ]]; then
      python3 - <<PY
import json, sys
sys.path.insert(0, "${INSTALL_DIR}")
from xray_client import apply_client_settings, merge_share_url, load_client_settings

settings = merge_share_url(load_client_settings(), """${share_url}""")
settings["enabled"] = True
settings["proxy_port"] = int("${proxy_port}")
result = apply_client_settings(settings, restart=True, write_env=True)
print(json.dumps(result, indent=2))
if not result.get("listening"):
    sys.exit(1)
PY
      return
    fi
  fi

  local current_json
  current_json="$(python3 - <<PY
import json, sys
sys.path.insert(0, "${INSTALL_DIR}")
from xray_client import load_client_settings
print(json.dumps(load_client_settings()))
PY
)"

  local address port uuid server_name public_key short_id fingerprint flow
  address="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('address',''))" "${current_json}")"
  port="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('port',443))" "${current_json}")"
  uuid="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('uuid',''))" "${current_json}")"
  server_name="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('server_name',''))" "${current_json}")"
  public_key="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('public_key',''))" "${current_json}")"
  short_id="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('short_id',''))" "${current_json}")"
  fingerprint="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('fingerprint','chrome'))" "${current_json}")"
  flow="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('flow',''))" "${current_json}")"

  if [[ -t 0 ]] && [[ -z "${XRAY_ADDRESS:-}" ]]; then
    echo "Press Enter to keep the value in brackets." >&2
  fi

  address="$(prompt_field "Server address" "${address}" XRAY_ADDRESS)"
  port="$(prompt_field "Server port" "${port}" XRAY_PORT)"
  uuid="$(prompt_field "UUID" "${uuid}" XRAY_UUID)"
  server_name="$(prompt_field "Reality SNI (serverName)" "${server_name}" XRAY_SERVER_NAME)"
  public_key="$(prompt_field "Reality public key (pbk)" "${public_key}" XRAY_PUBLIC_KEY)"
  short_id="$(prompt_field "Reality shortId" "${short_id}" XRAY_SHORT_ID)"
  fingerprint="$(prompt_field "TLS fingerprint" "${fingerprint:-chrome}" XRAY_FINGERPRINT)"
  flow="$(prompt_field "Flow (optional)" "${flow}" XRAY_FLOW)"

  python3 - <<PY
import json, sys
sys.path.insert(0, "${INSTALL_DIR}")
from xray_client import apply_client_settings

settings = {
    "enabled": True,
    "proxy_port": int("${proxy_port}"),
    "address": """${address}""",
    "port": int("""${port}"""),
    "uuid": """${uuid}""",
    "server_name": """${server_name}""",
    "public_key": """${public_key}""",
    "short_id": """${short_id}""",
    "fingerprint": """${fingerprint}""",
    "flow": """${flow}""",
}
result = apply_client_settings(settings, restart=True, write_env=True)
print(json.dumps(result, indent=2))
if not result.get("listening"):
    sys.exit(1)
PY
}

main() {
  need_root
  stop_docker_panel

  cd "${INSTALL_DIR}"
  chmod +x "${INSTALL_DIR}/install-wizard.sh" "${INSTALL_DIR}/scripts/setup-xray-proxy.sh" "${INSTALL_DIR}/scripts/apply-xray-client.py" 2>/dev/null || true

  local net_mode proxy_port scan_root proxy_url=""
  net_mode="$(prompt_network_mode)"

  if [[ "${net_mode}" == "2" ]]; then
    proxy_port="$(prompt_proxy_port)"
    configure_xray_client "${proxy_port}"
    proxy_url="http://127.0.0.1:${proxy_port}"
  else
    configure_direct_mode
  fi

  scan_root="$(prompt_scan_root)"
  if [[ ! -d "${scan_root}" ]]; then
    log "Creating scan root ${scan_root}"
    mkdir -p "${scan_root}"
  fi

  write_config "${scan_root}"

  optional_secret CURSOR_API_KEY "Cursor API key" "${CONFIG_DIR}/api-key"
  optional_secret PANEL_PASSWORD "Panel password" "${CONFIG_DIR}/auth-password"

  export SCAN_ROOT="${scan_root}"
  log "Installing panel on host (systemd) — workers will use host docker.sock"
  bash "${INSTALL_DIR}/install.sh"

  local ip
  ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo ""
  echo "Done."
  echo "Panel:  http://${ip:-localhost}:30228"
  echo "Config: ${CONFIG_DIR}/config.yaml"
  echo "Network: ${proxy_url:-direct}"
  echo "Logs:   journalctl -u agentcontrol -f"
}

main "$@"
