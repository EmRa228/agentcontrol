#!/usr/bin/env bash
# Install/refresh a fail2ban jail for panel login failures.
# The panel blocks after `max_login_attempts` on its own; fail2ban adds a network ban.
set -euo pipefail

CONFIG_FILE="${CONFIG_FILE:-/etc/agentcontrol/config.yaml}"
STATE_DIR="${STATE_DIR:-/var/lib/agentcontrol}"
MAXRETRY="${MAXRETRY:-10}"
FINDTIME="${FINDTIME:-300}"
BANTIME="${BANTIME:-300}"
AUTH_LOG="${AUTH_LOG:-${STATE_DIR}/auth.log}"

log() { echo "==> $*"; }

cfg_get() {
  local key="$1" default="$2"
  [[ -f "$CONFIG_FILE" ]] || { echo "$default"; return; }
  local value
  value="$(awk -v k="${key}:" '$1==k { $1=""; sub(/^[ \t]+/, ""); print; exit }' "$CONFIG_FILE")"
  value="${value%%[[:space:]]#*}"
  value="$(printf '%s' "$value" | tr -d '"'"'")"
  [[ -n "$value" ]] && echo "$value" || echo "$default"
}

PUBLIC_PORT="$(cfg_get port 30228)"
INTERNAL_PORT="$(cfg_get internal_port 30328)"

install_pkg() {
  if command -v fail2ban-client >/dev/null 2>&1; then
    return 0
  fi
  log "Installing fail2ban"
  if command -v apt-get >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq fail2ban >/dev/null
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y -q fail2ban >/dev/null
  elif command -v yum >/dev/null 2>&1; then
    yum install -y -q fail2ban >/dev/null
  else
    log "No supported package manager — skipping fail2ban"
    return 1
  fi
}

if ! install_pkg; then
  exit 0
fi

mkdir -p "${STATE_DIR}" /etc/fail2ban/filter.d /etc/fail2ban/jail.d
touch "${AUTH_LOG}"
chmod 640 "${AUTH_LOG}" 2>/dev/null || true

cat > /etc/fail2ban/filter.d/agentcontrol.conf <<'EOF'
[Definition]
failregex = ^.*agentcontrol auth failure ip=<HOST> .*$
ignoreregex =
datepattern = ^%%Y-%%m-%%dT%%H:%%M:%%S
EOF

cat > /etc/fail2ban/jail.d/agentcontrol.local <<EOF
[agentcontrol]
enabled = true
backend = auto
filter = agentcontrol
logpath = ${AUTH_LOG}
maxretry = ${MAXRETRY}
findtime = ${FINDTIME}
bantime = ${BANTIME}
action = iptables-multiport[name=agentcontrol, port="${PUBLIC_PORT},${INTERNAL_PORT}", protocol=tcp]
EOF

systemctl enable fail2ban >/dev/null 2>&1 || true
if systemctl is-active fail2ban >/dev/null 2>&1; then
  systemctl restart fail2ban || log "fail2ban restart failed — check jail.local"
else
  systemctl start fail2ban || log "fail2ban start failed — check journal"
fi

if fail2ban-client status agentcontrol >/dev/null 2>&1; then
  log "fail2ban jail 'agentcontrol' active (${MAXRETRY} failures / ${FINDTIME}s → ban ${BANTIME}s)"
else
  log "fail2ban installed, but jail is not active yet (check /etc/fail2ban/jail.d/agentcontrol.local)"
fi
