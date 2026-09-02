#!/usr/bin/env bash
# Non-interactive panel update: git pull from GitHub + rebuild Docker (or restart systemd).
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/agentcontrol}"
STATE_DIR="${STATE_DIR:-/var/lib/agentcontrol}"
LOCK_FILE="${STATE_DIR}/update.lock"
LOG_FILE="${STATE_DIR}/update.log"

mkdir -p "${STATE_DIR}"

if [[ -f "${LOCK_FILE}" ]]; then
  pid="$(cat "${LOCK_FILE}" 2>/dev/null || true)"
  if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
    echo "Update already running (pid ${pid})" | tee -a "${LOG_FILE}"
    exit 0
  fi
fi

echo "$$" > "${LOCK_FILE}"
trap 'rm -f "${LOCK_FILE}"' EXIT

{
  echo "=== panel update started $(date -Iseconds) ==="
  cd "${INSTALL_DIR}"
  git fetch origin main
  git reset --hard origin/main
  chmod +x bootstrap.sh install.sh install-wizard.sh scripts/*.sh scripts/*.py 2>/dev/null || true

  if systemctl is-active agentcontrol &>/dev/null; then
    systemctl restart agentcontrol
  else
    echo "No agentcontrol systemd service — run: bash ${INSTALL_DIR}/install.sh"
  fi

  echo "=== panel update finished $(date -Iseconds) ==="
} >> "${LOG_FILE}" 2>&1
