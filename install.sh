#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/agentstart}"
CONFIG_DIR="${CONFIG_DIR:-/etc/agentstart}"
STATE_DIR="${STATE_DIR:-/var/lib/agentstart}"
REPO_URL="${AGENTSTART_REPO:-https://github.com/EmRa228/agentcontrol.git}"

export PATH="/root/.local/bin:/usr/local/bin:${PATH:-/usr/bin:/bin}"

log() { echo "==> $*"; }

need_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "Run as root: sudo $0"
    exit 1
  fi
}

install_packages() {
  if command -v apt-get &>/dev/null; then
    apt-get update -qq
    apt-get install -y -qq python3-venv rsync git curl ca-certificates
  fi
}

install_cursor_agent() {
  if command -v agent &>/dev/null; then
    log "agent CLI already installed: $(command -v agent)"
    return
  fi
  log "Installing Cursor agent CLI…"
  curl https://cursor.com/install -fsS | bash
  export PATH="/root/.local/bin:${PATH}"
}

ensure_repo() {
  SCRIPT_DIR="$(cd "${BASH_SOURCE[0]%/*}" && pwd)"

  if [[ "${SCRIPT_DIR}" == "${INSTALL_DIR}" ]] && [[ -d "${INSTALL_DIR}/.git" ]]; then
    log "Pulling latest code"
    git -C "${INSTALL_DIR}" pull --ff-only 2>/dev/null || git -C "${INSTALL_DIR}" pull || true
  elif [[ ! -f "${INSTALL_DIR}/app.py" ]]; then
    if [[ -d "${INSTALL_DIR}/.git" ]]; then
      log "Updating ${INSTALL_DIR}"
      git -C "${INSTALL_DIR}" pull --ff-only 2>/dev/null || git -C "${INSTALL_DIR}" pull
    else
      log "Cloning ${REPO_URL} → ${INSTALL_DIR}"
      mkdir -p "$(dirname "${INSTALL_DIR}")"
      git clone "${REPO_URL}" "${INSTALL_DIR}"
    fi
  fi

  if [[ "${SCRIPT_DIR}" != "${INSTALL_DIR}" ]] && [[ -f "${SCRIPT_DIR}/app.py" ]]; then
    mkdir -p "${INSTALL_DIR}"
    rsync -a --exclude venv --exclude .git "${SCRIPT_DIR}/" "${INSTALL_DIR}/"
  fi
}

setup_api_key() {
  mkdir -p "${CONFIG_DIR}"
  if [[ -n "${CURSOR_API_KEY:-}" ]]; then
    printf '%s' "${CURSOR_API_KEY}" > "${CONFIG_DIR}/api-key"
    chmod 600 "${CONFIG_DIR}/api-key"
    log "API key updated in ${CONFIG_DIR}/api-key"
    return
  fi
  if [[ -s "${CONFIG_DIR}/api-key" ]]; then
    log "Keeping existing API key: ${CONFIG_DIR}/api-key"
    return
  fi
  if [[ ! -t 0 ]]; then
    echo "WARNING: no API key. Set CURSOR_API_KEY=... or add ${CONFIG_DIR}/api-key"
    touch "${CONFIG_DIR}/api-key"
    chmod 600 "${CONFIG_DIR}/api-key"
    return
  fi
  echo ""
  echo "Cursor personal API key required (Dashboard → API Keys)"
  echo "https://cursor.com/settings"
  read -rsp "Paste API key: " key
  echo ""
  if [[ -z "${key}" ]]; then
    echo "WARNING: no API key set. Start will fail until you add one:"
    echo "  echo YOUR_KEY | sudo tee ${CONFIG_DIR}/api-key"
    touch "${CONFIG_DIR}/api-key"
  else
    printf '%s' "${key}" > "${CONFIG_DIR}/api-key"
  fi
  chmod 600 "${CONFIG_DIR}/api-key"
}

setup_panel_password() {
  AUTH_FILE="${CONFIG_DIR}/auth-password"
  mkdir -p "${CONFIG_DIR}"
  if [[ -n "${PANEL_PASSWORD:-}" ]]; then
    printf '%s' "${PANEL_PASSWORD}" > "${AUTH_FILE}"
    chmod 600 "${AUTH_FILE}"
    log "Panel password updated in ${AUTH_FILE}"
    return
  fi
  if [[ -s "${AUTH_FILE}" ]]; then
    log "Keeping existing panel password: ${AUTH_FILE}"
    return
  fi
  short="$(hostname -s 2>/dev/null | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9' | head -c 5)"
  [[ -z "${short}" ]] && short="srv"
  rand="$(tr -dc 'a-z0-9' </dev/urandom | head -c 5)"
  pw="${short}-${rand}"
  printf '%s' "${pw}" > "${AUTH_FILE}"
  chmod 600 "${AUTH_FILE}"
  PANEL_PASSWORD_CREATED="${pw}"
  log "Panel password created: ${pw}"
}

install_app() {
  log "Installing agentstart to ${INSTALL_DIR}"

  if [[ ! -d "${INSTALL_DIR}/venv" ]]; then
    python3 -m venv "${INSTALL_DIR}/venv"
  fi
  "${INSTALL_DIR}/venv/bin/pip" install --upgrade pip -q
  "${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt" -q

  mkdir -p "${CONFIG_DIR}" "${STATE_DIR}"

  if [[ ! -f "${CONFIG_DIR}/config.yaml" ]]; then
    cp "${INSTALL_DIR}/config.example.yaml" "${CONFIG_DIR}/config.yaml"
  fi
  ln -sf "${CONFIG_DIR}/config.yaml" "${INSTALL_DIR}/config.yaml"

  AGENT_PATH="$(command -v agent 2>/dev/null || true)"
  if [[ -n "${AGENT_PATH}" ]] && [[ -f "${CONFIG_DIR}/config.yaml" ]]; then
    sed -i "s|^agent_bin:.*|agent_bin: ${AGENT_PATH}|" "${CONFIG_DIR}/config.yaml"
  fi

  cp "${INSTALL_DIR}/agentstart.service" /etc/systemd/system/agentstart.service
  systemctl daemon-reload
  systemctl enable agentstart
  systemctl restart agentstart
}

main() {
  need_root
  install_packages
  install_cursor_agent
  ensure_repo
  setup_api_key
  setup_panel_password
  install_app

  IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo ""
  echo "Done."
  echo "Panel: http://${IP:-localhost}:30228"
  if [[ -n "${PANEL_PASSWORD_CREATED:-}" ]]; then
    echo "Panel password: ${PANEL_PASSWORD_CREATED}"
  elif [[ -s "${CONFIG_DIR}/auth-password" ]]; then
    echo "Panel password: (see ${CONFIG_DIR}/auth-password)"
  fi
  echo "API key: ${CONFIG_DIR}/api-key"
  echo "Logs:    journalctl -u agentstart -f"
}

main "$@"
