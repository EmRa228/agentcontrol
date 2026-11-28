# Operations

Host-only systemd install. Docker files exist but are deprecated; the panel refuses
to start in a container (`HOST_ONLY` guard) because workers need the host Docker
socket and process tree.

## Install

```bash
sudo bash -c 'git clone https://github.com/EmRa228/agentcontrol.git /opt/agentcontrol && /opt/agentcontrol/bootstrap.sh'
sudo CURSOR_API_KEY=... PANEL_PASSWORD=... /opt/agentcontrol/install.sh   # non-interactive
sudo /opt/agentcontrol/install-wizard.sh                                  # interactive (direct/xray)
```

`install.sh` removes the legacy `agentstart` unit, installs `python3-venv rsync git
curl`, installs the Cursor `agent` CLI when missing, resets `/opt/agentcontrol` to
`origin/main`, creates the venv + deps, seeds `/etc/agentcontrol/config.yaml`,
installs the unit, runs `enable-caddy.sh`, restarts the panel, writes `HOST_ONLY`,
then runs `install-caddy.sh`.

Firewall: allow the public `port` (30228) and **80** for ACME HTTP-01. Keep
`internal_port`, `https_internal_port` and 4096 loopback-only.

## Update

Three equivalent paths (all run `scripts/panel-update.sh`):

- UI **Update panel** / `POST /api/update/apply`
- `sudo /opt/agentcontrol/bootstrap.sh` (fetch + reset + install)
- Fleet **Update all panels** (calls the API on each non-paused server)

`panel-update.sh` fetches `origin/main` with a timeout, `git reset --hard`, upgrades
pip deps, runs `enable-caddy.sh`, restarts the unit, waits for `/health`, then
`install-caddy.sh` and refreshes logrotate/fail2ban. On a failed health check it
resets to the previous commit and restarts again. The API starts it through
`systemd-run` (outside the panel cgroup) so the restart cannot kill the updater.
Lock/PID: `state_dir/update.lock`; output: `state_dir/update.log`. It rewrites SSH
origins to HTTPS because SSH is often blocked.

Fleet “Update all panels” is **canary-first**: the first server must come back
healthy with a new version before the remaining servers are updated.

> `git reset --hard` discards local changes. Commit and push before rolling out.

### Rollback

There is no automated rollback. Manual:

```bash
cd /opt/agentcontrol
git fetch origin main
git reset --hard <previous-sha>        # or a tag/release commit
./venv/bin/pip install -r requirements.txt
sudo bash scripts/enable-caddy.sh
sudo systemctl restart agentcontrol
sudo bash scripts/install-caddy.sh
```

Back up `/etc/agentcontrol` and `state_dir/sessions.db` before a risky change.

## TLS / Caddy layout

```text
30228 tls_front.py
  TLS        -> 127.0.0.1:30443 Caddy -> 127.0.0.1:30328 Flask
  HTTP+host  -> 308 https://HOST:30228
  HTTP+bare IP -> 127.0.0.1:30328 (no ACME for IPs)
```

- `enable-caddy.sh` only enables the layout when port 80 is free (or `tls_cert_file`
  is set) and assigns loopback ports if `internal_port` is empty.
- `public_hostname` → fixed ACME cert. Empty → On-Demand TLS: the first handshake
  for a DNS-matched name triggers issuance; Caddy asks
  `/api/caddy/allow` (loopback only), which checks that the name resolves here.
- **HTTP redirect:** the same-port front answers plain HTTP for a hostname with
  `308 https://HOST:30228/…`; bare IPs stay on plain HTTP because ACME does not
  issue certificates for IPs.
- **Auto-renew:** Caddy renews the ACME certificate automatically about 30 days
  before expiry (ARI-aware). It uses the HTTP-01 challenge, so **port 80 must stay
  free**; do not run another service there. TLS-ALPN is disabled because the site
  listens on a custom port. Caddy binds port 80 only during the challenge and
  closes it afterwards. `scripts/maintenance.py` alerts if the certificate is close
  to expiry while port 80 is occupied.
- This was verified with a staging CA: Caddy served the HTTP-01 key authorization
  and obtained a certificate. Keep `auto_https disable_redirects` in the Caddyfile —
  the same-port front performs the redirect; a persistent listener on 80 would
  break renewal.
- If you need `http://HOST` (port 80) to redirect instead, an external ACME setup
  (for example certbot webroot + static `tls_cert_file`) is required, because Caddy
  must own port 80 for HTTP-01.
- Port 80 busy and no cert → the installer logs a warning and leaves the panel
  directly on `port` (HTTP only). Provide `tls_cert_file`/`tls_key_file` instead.
- Caddy binary is pinned (`CADDY_VERSION`, default 2.11.9) and downloaded with
  checksums to `state_dir/bin/caddy`.

## Diagnostics

```bash
curl -s http://127.0.0.1:30328/health
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' http://127.0.0.1:30228/health
ss -ltnp | grep -E '30228|30328|30443|4096'
systemctl status agentcontrol agentcontrol-caddy agentcontrol-tls-front
journalctl -u agentcontrol -u agentcontrol-caddy -u agentcontrol-tls-front -n 100
tail -n 100 /var/lib/agentcontrol/update.log
tail -n 100 /var/lib/agentcontrol/opencode-install.log
curl -s -H "X-AgentControl-Token: $TOKEN" \
  http://127.0.0.1:30328/api/status
```

## Common failures

| Symptom | Check |
|---|---|
| 502 / connection refused on 30228 | `agentcontrol-tls-front` up? Caddy up? Flask on 30328? journal |
| Certificate error | DNS points here, inbound 80 open, Caddy journal (`journalctl -u agentcontrol-caddy`) |
| Panel loops on login | password file readable, cookie not blocked, TOTP clock drift |
| TOTP lost | root: remove/rename `/etc/agentcontrol/totp.json` (disables 2FA), then re-enrol |
| Update busy | PID in `state_dir/update.lock`; stale lock is ignored if the PID is gone |
| Update fails | `update.log`; origin must reach `origin/main`; venv exists |
| OpenCode button returns `install_required` repeatedly | `opencode-install.log`; needs GitHub + package registry; pinned version/build mismatch |
| `/oc/` is blank | web build missing/mismatched; run `scripts/install-opencode.sh`; hard reload assets |
| Worker fails to start | `agent` binary, `/etc/agentcontrol/api-key`, Docker socket for compose repos, proxy listening |
| Disk fills | worker logs in `state_dir/*.log` (no rotation), `caddy/`, opencode logs |
| Fleet returns 429 | KV free-tier writes; read `fleet/CLOUDFLARE.md` |
| Fleet cannot reach panel | hostname (not IP), grey-cloud DNS, port open, wrong/missing server password |

## Security operations

- **First-run race:** non-loopback setup needs `/etc/agentcontrol/setup-token`
  (`https://HOST:30228/?setup=TOKEN`). Configure before exposing the port.
- Reset the password with
  `sudo /opt/agentcontrol/venv/bin/python /opt/agentcontrol/scripts/set-password.py`
  (Argon2; `cat auth-password` shows only a hash). Then revoke sessions.
- Failed logins: 10 per client → 5-minute in-process lockout; `auth.log` feeds the
  fail2ban jail (`maxretry=10`, `bantime=300`). Check with
  `fail2ban-client status agentcontrol`.
- Prefer scoped API tokens (Settings → API tokens) for Fleet/automation instead of
  the panel password.
- OpenCode updates: `scripts/update-opencode.sh --check` (no changes) or
  `--version X` (digest verification, 3-way patch, build/smoke, atomic swap,
  rollback). The UI exposes the same via Settings → OpenCode.
- `agentcontrol-maintenance.timer` runs daily at 04:00: retention, `/etc` + SQLite
  backups (`state_dir/backups`), old build cleanup, disk/cert/version alerts.
- Terminal: admin-only, disabled with `terminal_enabled: false`; every open is
  audited.
- Rotate `FLEET_ENC_KEY` only with a migration plan: server passwords encrypted with
  the old key cannot be read after the key changes.
