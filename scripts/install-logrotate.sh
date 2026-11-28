#!/usr/bin/env bash
# Install logrotate rules for panel/worker/OpenCode logs.
set -euo pipefail

STATE_DIR="${STATE_DIR:-/var/lib/agentcontrol}"
ROTATE_DAYS="${ROTATE_DAYS:-7}"
TARGET="/etc/logrotate.d/agentcontrol"

mkdir -p "${STATE_DIR}"

cat > "${TARGET}" <<EOF
${STATE_DIR}/*.log {
    daily
    rotate ${ROTATE_DAYS}
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    su root root
}
EOF

chmod 644 "${TARGET}"
echo "==> logrotate rules installed: ${TARGET} (${ROTATE_DAYS} days)"
