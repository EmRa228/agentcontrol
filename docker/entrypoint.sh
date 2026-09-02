#!/usr/bin/env bash
set -euo pipefail

echo "ERROR: AgentControl Docker image is disabled." >&2
echo "Workers started from a container cannot use host /var/run/docker.sock." >&2
echo "" >&2
echo "On the host (SSH):" >&2
echo "  git clone https://github.com/EmRa228/agentcontrol.git /opt/agentcontrol" >&2
echo "  bash /opt/agentcontrol/scripts/migrate-from-docker.sh" >&2
exit 1
