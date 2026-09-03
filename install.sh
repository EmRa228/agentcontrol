#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/agentcontrol}"
CONFIG_DIR="${CONFIG_DIR:-/etc/agentcontrol}"
STATE_DIR="${STATE_DIR:-/var/lib/agentcontrol}"
REPO_URL="${AGENTCONTROL_REPO:-https://github.com/EmRa228/agentcontrol.git}"

export PATH="/root/.local/bin:/usr/local/bin:${PATH:-/usr/bin:/bin}"

log() { echo "==> $*"; }

need_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "Run as root: sudo $0"
    exit 1
  fi
}

remove_legacy() {
  systemctl stop agentstart 2>/dev/null || true
  systemctl disable agentstart 2>/dev/null || true
  rm -f /etc/systemd/system/agentstart.service

  if [[ -d /etc/agentstart ]] && [[ ! -d "${CONFIG_DIR}" ]]; then
    log "Migrating /etc/agentstart → ${CONFIG_DIR}"
    cp -a /etc/agentstart "${CONFIG_DIR}"
  fi
  if [[ -d /var/lib/agentstart ]] && [[ ! -d "${STATE_DIR}" ]]; then
    log "Migrating /var/lib/agentstart → ${STATE_DIR}"
    cp -a /var/lib/agentstart "${STATE_DIR}"
  fi
  if [[ -d /opt/agentstart ]] && [[ "${INSTALL_DIR}" != "/opt/agentstart" ]]; then
    log "Removing legacy /opt/agentstart"
    rm -rf /opt/agentstart
  fi
  systemctl daemon-reload
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

git_fetch_timeout() {
  local dir="$1"
  local timeout_sec="${GIT_FETCH_TIMEOUT:-180}"
  local origin_url
  origin_url="$(git -C "${dir}" remote get-url origin 2>/dev/null || true)"
  case "${origin_url}" in
    git@github.com:*|ssh://git@github.com/*)
      log "Switching origin to HTTPS for ${dir}"
      git -C "${dir}" remote set-url origin "${REPO_URL}"
      ;;
  esac
  timeout "${timeout_sec}" git -C "${dir}" fetch origin main
}

ensure_repo() {
  SCRIPT_DIR="$(cd "${BASH_SOURCE[0]%/*}" && pwd)"

  if [[ "${SCRIPT_DIR}" == "${INSTALL_DIR}" ]] && [[ -d "${INSTALL_DIR}/.git" ]]; then
    log "Pulling latest code"
    git_fetch_timeout "${INSTALL_DIR}"
    git -C "${INSTALL_DIR}" reset --hard origin/main
  elif [[ ! -f "${INSTALL_DIR}/app.py" ]]; then
    if [[ -d "${INSTALL_DIR}/.git" ]]; then
      log "Updating ${INSTALL_DIR}"
      git_fetch_timeout "${INSTALL_DIR}"
      git -C "${INSTALL_DIR}" reset --hard origin/main
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
    log "No API key yet — set later in the web UI or ${CONFIG_DIR}/api-key"
    return
  fi
  echo ""
  echo "Cursor personal API key (optional — can set in web UI on first visit)"
  echo "https://cursor.com/settings"
  read -rsp "Paste API key (Enter to skip): " key
  echo ""
  if [[ -z "${key}" ]]; then
    log "Skipping API key — configure in panel Settings on first visit"
    return
  fi
  printf '%s' "${key}" > "${CONFIG_DIR}/api-key"
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
  log "Panel password not set — configure it in the web UI on first visit"
}

install_app() {
  log "Installing agentcontrol to ${INSTALL_DIR}"

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

  cp "${INSTALL_DIR}/agentcontrol.service" /etc/systemd/system/agentcontrol.service
  systemctl daemon-reload
  systemctl enable agentcontrol
  systemctl restart agentcontrol
  write_host_only_guard
}

write_host_only_guard() {
  mkdir -p "${CONFIG_DIR}"
  cat > "${CONFIG_DIR}/HOST_ONLY" <<EOF
# AgentControl must run on host systemd (${INSTALL_DIR}), not Docker.
# Do NOT run: docker compose up (agentcontrol service)
# Interactive host setup: bash install-wizard.sh
# To update panel: cd ${INSTALL_DIR} && git pull && bash install.sh
installed_at=$(date -Iseconds)
EOF
  chmod 644 "${CONFIG_DIR}/HOST_ONLY"
}

main() {
  need_root
  remove_legacy
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
  echo "Set panel password and Cursor API key in the web UI on first visit."
  echo "API key: ${CONFIG_DIR}/api-key"
  echo "Logs:    journalctl -u agentcontrol -f"
}

main "$@"
