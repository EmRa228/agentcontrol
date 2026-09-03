#!/usr/bin/env bash
# Move AgentControl from Docker container → host systemd at /opt/agentcontrol.
# Run on the Linux host as root (SSH), not from a Cursor agent sandbox.
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/agentcontrol}"
CONFIG_DIR="${CONFIG_DIR:-/etc/agentcontrol}"
STATE_DIR="${STATE_DIR:-/var/lib/agentcontrol}"
REPO_URL="${AGENTCONTROL_REPO:-https://github.com/EmRa228/agentcontrol.git}"
PANEL_PORT="${PANEL_PORT:-30228}"

log() { echo "==> $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

need_root() {
  [[ "${EUID:-$(id -u)}" -eq 0 ]] || die "Run as root on the host (SSH)."
}

need_systemd() {
  if [[ -f /.dockerenv ]]; then
    die "You are inside a container — run this from host SSH (not docker exec)."
  fi
  command -v systemctl >/dev/null || die "systemctl not found — is this a systemd host?"
  if [[ -d /run/systemd/system ]] || [[ "$(readlink -f /proc/1/exe 2>/dev/null)" == */systemd ]]; then
    return 0
  fi
  die "systemd does not appear to be PID 1 on this host."
}

need_docker() {
  command -v docker >/dev/null || die "docker not found"
  docker info >/dev/null 2>&1 || die "docker daemon not reachable"
}

stop_panel_workers() {
  [[ -f "${STATE_DIR}/workers.json" ]] || return 0
  python3 - <<'PY' || true
import json, os, signal
from pathlib import Path

state_path = Path(os.environ["STATE_DIR"]) / "workers.json"
if not state_path.exists():
    raise SystemExit(0)
state = json.loads(state_path.read_text())
for name, info in state.items():
    pid = info.get("pid")
    if not pid:
        continue
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        print(f"stopped worker {name} pid={pid}")
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
state_path.write_text("{}\n")
PY
}

stop_agentcontrol_container() {
  if docker ps -a --format '{{.Names}}' | grep -qx agentcontrol; then
    log "Stopping agentcontrol Docker container"
    docker stop agentcontrol 2>/dev/null || true
    docker rm agentcontrol 2>/dev/null || true
  fi
  if [[ -f "${INSTALL_DIR}/docker-compose.yml" ]]; then
    (cd "${INSTALL_DIR}" && docker compose down --remove-orphans 2>/dev/null) || true
  fi
}

install_host_panel() {
  log "Installing AgentControl to ${INSTALL_DIR} (host systemd)"
  mkdir -p "$(dirname "${INSTALL_DIR}")"
  if [[ ! -d "${INSTALL_DIR}/.git" ]]; then
    git clone "${REPO_URL}" "${INSTALL_DIR}"
  else
    git -C "${INSTALL_DIR}" fetch origin main
    git -C "${INSTALL_DIR}" reset --hard origin/main
  fi
  bash "${INSTALL_DIR}/install.sh"
}

verify() {
  log "Verification"
  systemctl is-active --quiet agentcontrol || die "agentcontrol.service not active"
  curl -sf --max-time 5 "http://127.0.0.1:${PANEL_PORT}/health" >/dev/null \
    || die "panel health check failed on :${PANEL_PORT}"
  test -S /var/run/docker.sock || die "/var/run/docker.sock missing on host"
  log "OK: panel on host systemd, docker.sock present"
  echo ""
  echo "Next: open http://$(hostname -I | awk '{print $1}'):${PANEL_PORT} and Start your workers again."
}

main() {
  need_root
  need_systemd
  need_docker
  export CONFIG_DIR STATE_DIR INSTALL_DIR

  log "Stopping panel-managed Cursor workers"
  stop_panel_workers

  log "Removing AgentControl Docker deployment"
  stop_agentcontrol_container

  install_host_panel
  verify
}

main "$@"
