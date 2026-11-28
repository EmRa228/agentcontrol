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
  PREV_SHA="$(git rev-parse HEAD 2>/dev/null || true)"
  log "previous commit: ${PREV_SHA:-unknown}"
  sync_main_branch
  chmod +x bootstrap.sh install.sh install-wizard.sh scripts/*.sh scripts/*.py 2>/dev/null || true

  if [[ -d "${INSTALL_DIR}/venv" ]]; then
    "${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt" -q
  fi

  if [[ -x "${INSTALL_DIR}/scripts/enable-caddy.sh" ]]; then
    INSTALL_DIR="${INSTALL_DIR}" CONFIG_DIR="${CONFIG_DIR:-/etc/agentcontrol}" STATE_DIR="${STATE_DIR}" \
      bash "${INSTALL_DIR}/scripts/enable-caddy.sh" >> "${LOG_FILE}" 2>&1 \
      || log "Could not enable the bundled Caddy layout"
  fi

  INTERNAL_PORT="$(awk -v k='internal_port:' '$1==k { $1=""; sub(/^[ \t]+/, ""); print; exit }' "${CONFIG_DIR:-/etc/agentcontrol}/config.yaml" 2>/dev/null || true)"
  if [[ -z "${INTERNAL_PORT}" ]]; then
    INTERNAL_PORT="$(awk -v k='port:' '$1==k { $1=""; sub(/^[ \t]+/, ""); print; exit }' "${CONFIG_DIR:-/etc/agentcontrol}/config.yaml" 2>/dev/null || true)"
  fi
  INTERNAL_PORT="${INTERNAL_PORT:-30328}"

  health_ok() {
    curl -fsS --max-time 2 "http://127.0.0.1:${INTERNAL_PORT}/health" >/dev/null 2>&1
  }

  if systemctl is-active agentcontrol &>/dev/null; then
    systemctl restart agentcontrol
    log "agentcontrol.service restarted"
    HEALTHY=0
    for _ in $(seq 1 30); do
      sleep 1
      if health_ok; then HEALTHY=1; break; fi
    done
    if [[ "$HEALTHY" != "1" ]]; then
      log "health check failed after restart"
      if [[ -n "${PREV_SHA}" ]]; then
        log "rolling back to ${PREV_SHA}"
        git reset --hard "${PREV_SHA}"
        "${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt" -q || true
        systemctl restart agentcontrol
        sleep 3
        health_ok && log "rollback succeeded" || log "rollback failed — manual intervention required"
      fi
      exit 1
    fi
    log "health check passed"
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

  if [[ -x "${INSTALL_DIR}/scripts/install-caddy.sh" ]]; then
    log "Refreshing bundled Caddy"
    INSTALL_DIR="${INSTALL_DIR}" CONFIG_DIR="${CONFIG_DIR:-/etc/agentcontrol}" STATE_DIR="${STATE_DIR}" \
      bash "${INSTALL_DIR}/scripts/install-caddy.sh" >> "${LOG_FILE}" 2>&1 \
      || log "Caddy refresh failed — see ${LOG_FILE}"
  fi

  if [[ -x "${INSTALL_DIR}/scripts/install-logrotate.sh" ]]; then
    STATE_DIR="${STATE_DIR}" bash "${INSTALL_DIR}/scripts/install-logrotate.sh" >> "${LOG_FILE}" 2>&1 || true
  fi
  if [[ -x "${INSTALL_DIR}/scripts/install-fail2ban.sh" ]]; then
    CONFIG_FILE="${CONFIG_DIR:-/etc/agentcontrol}/config.yaml" STATE_DIR="${STATE_DIR}" \
      bash "${INSTALL_DIR}/scripts/install-fail2ban.sh" >> "${LOG_FILE}" 2>&1 || true
  fi
  if [[ -x "${INSTALL_DIR}/scripts/cleanup-builds.sh" ]]; then
    bash "${INSTALL_DIR}/scripts/cleanup-builds.sh" >> "${LOG_FILE}" 2>&1 || true
  fi

  log "=== panel update finished $(date -Iseconds) ==="
} >> "${LOG_FILE}" 2>&1
