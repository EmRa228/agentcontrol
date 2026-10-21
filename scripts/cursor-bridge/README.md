# Cursor api2 bridge (region bypass)

Iran blocks direct `api2.cursor.sh` (HTTP 403 region). Worker auth then fails with a misleading "invalid API key".

## Architecture

1. **Foreign host**: nginx `stream` listens on `127.0.0.1:18443` and TCP-forwards to `api2.cursor.sh:443` (TLS passthrough; egress from foreign IP).
2. **SSH reverse tunnel** (foreign → Iran): `-R 127.0.0.1:18443:127.0.0.1:18443` so Iran reaches the foreign nginx via localhost.
3. **Iran**: `/etc/hosts` line `127.0.0.1 api2.cursor.sh` and `/etc/agentcontrol/env` sets `CURSOR_API_ENDPOINT=https://api2.cursor.sh:18443`.

AgentControl workers inherit env from the systemd unit (`EnvironmentFile=/etc/agentcontrol/env`).

## Verify on Iran

```bash
curl -sS -o /dev/null -w "HTTP:%{http_code}\n" https://api2.cursor.sh:18443/
# expect HTTP:200 and "Welcome to Cursor"
```

## Foreign nginx (Ubuntu)

```bash
apt install nginx libnginx-mod-stream
# include stream config in /etc/nginx/nginx.conf (outside http {})
nginx -t && systemctl restart nginx
```

## Persistent tunnel (on foreign host)

```bash
autossh -M 0 -N -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes \
  -R 127.0.0.1:18443:127.0.0.1:18443 \
  -p 22822 -i /path/to/key root@62.106.95.59
```

Current temporary tunnel: Cloud Agent VM → this server (do not rely on it after the agent run ends).
