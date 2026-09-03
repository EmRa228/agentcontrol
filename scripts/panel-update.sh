#!/usr/bin/env bash
# Non-interactive panel update: git pull from GitHub + restart systemd service.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_DIR="${INSTALL_DIR:-/opt/agentcontrol}"
STATE_DIR="${STATE_DIR:-/var/lib/agentcontrol}"
LOCK_FILE="${STATE_DIR}/update.lock"
LOG_FILE="${STATE_DIR}/update.log"
REPO_URL="${AGENTCONTROL_REPO:-https://github.com/EmRa228/agentcontrol.git}"

if [[ ! -d "${INSTALL_DIR}/.git" ]] && [[ -d "${SCRIPT_DIR}/.git" ]]; then
  INSTALL_DIR="${SCRIPT_DIR}"
fi

mkdir -p "${STATE_DIR}"

if [[ -f "${LOCK_FILE}" ]]; then
  pid="$(cat "${LOCK_FILE}" 2>/dev/null || true)"
  if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
    echo "Update already running (pid ${pid})" | tee -a "${LOG_FILE}"
    exit 0
  fi
  rm -f "${LOCK_FILE}"
fi

echo "$$" > "${LOCK_FILE}"
trap 'rm -f "${LOCK_FILE}"' EXIT

log() { echo "$*" | tee -a "${LOG_FILE}"; }

GIT_FETCH_TIMEOUT="${GIT_FETCH_TIMEOUT:-180}"

ensure_https_origin() {
  local origin_url
  origin_url="$(git remote get-url origin 2>/dev/null || true)"
  case "${origin_url}" in
    git@github.com:*|ssh://git@github.com/*)
      log "origin uses GitHub SSH — switching to HTTPS (SSH is often blocked or slow)"
      git remote set-url origin "${REPO_URL}"
      ;;
  esac
}

sync_main_branch() {
  ensure_https_origin
  if ! timeout "${GIT_FETCH_TIMEOUT}" git fetch origin main; then
    log "git fetch failed or timed out after ${GIT_FETCH_TIMEOUT}s"
    exit 1
  fi
  git reset --hard origin/main
}

{
  log "=== panel update started $(date -Iseconds) ==="
  log "INSTALL_DIR=${INSTALL_DIR}"

  if [[ ! -d "${INSTALL_DIR}/.git" ]]; then
    log "No git repo at ${INSTALL_DIR} — cloning"
    mkdir -p "$(dirname "${INSTALL_DIR}")"
    git clone "${REPO_URL}" "${INSTALL_DIR}"
  fi

  cd "${INSTALL_DIR}"
  sync_main_branch
  chmod +x bootstrap.sh install.sh install-wizard.sh scripts/*.sh scripts/*.py 2>/dev/null || true

  if [[ -d "${INSTALL_DIR}/venv" ]]; then
    "${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt" -q
  fi

  if systemctl is-active agentcontrol &>/dev/null; then
    systemctl restart agentcontrol
    log "agentcontrol.service restarted"
  elif [[ -f /.dockerenv ]]; then
    log "ERROR: panel runs in Docker — migrate to host: bash scripts/migrate-from-docker.sh"
    exit 1
  elif [[ -x "${INSTALL_DIR}/install.sh" ]]; then
    log "No agentcontrol systemd service — running install.sh"
    CONFIG_DIR="${CONFIG_DIR:-/etc/agentcontrol}" \
    STATE_DIR="${STATE_DIR}" \
    INSTALL_DIR="${INSTALL_DIR}" \
      bash "${INSTALL_DIR}/install.sh" >> "${LOG_FILE}" 2>&1
  else
    log "No agentcontrol systemd service — run: bash ${INSTALL_DIR}/install.sh"
    exit 1
  fi

  log "=== panel update finished $(date -Iseconds) ==="
} >> "${LOG_FILE}" 2>&1
