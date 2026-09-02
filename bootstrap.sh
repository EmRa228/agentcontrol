#!/usr/bin/env bash
# Idempotent entrypoint — safe to run multiple times (install or update).
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/agentcontrol}"
REPO_URL="${AGENTCONTROL_REPO:-https://github.com/EmRa228/agentcontrol.git}"

export INSTALL_DIR

log() { echo "==> $*"; }

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Run as root: sudo $0"
  exit 1
fi

if [[ -d "${INSTALL_DIR}/.git" ]]; then
  log "Updating existing repo in ${INSTALL_DIR}"
  git -C "${INSTALL_DIR}" fetch origin main
  git -C "${INSTALL_DIR}" reset --hard origin/main
elif [[ -f "${INSTALL_DIR}/install.sh" ]]; then
  log "Using existing ${INSTALL_DIR}"
else
  log "Cloning ${REPO_URL} → ${INSTALL_DIR}"
  mkdir -p "$(dirname "${INSTALL_DIR}")"
  git clone "${REPO_URL}" "${INSTALL_DIR}"
fi

cd "${INSTALL_DIR}"
chmod +x bootstrap.sh install.sh install-wizard.sh scripts/*.sh scripts/*.py 2>/dev/null || true

if [[ "${WIZARD:-}" == "1" ]] || [[ "${INTERACTIVE_WIZARD:-}" == "1" ]]; then
  exec ./install-wizard.sh
fi

exec ./install.sh
