# AgentControl agent guide

AgentControl runs Cursor Cloud Agent workers per project on Linux. The optional Fleet dashboard aggregates panels. Keep API keys, panel passwords, xray UUIDs and Reality keys out of Git and logs.

| Area | Entry points |
|---|---|
| Panel | `app.py`, `templates/index.html`, `config.example.yaml` |
| Cursor workers | `app.py`, `worker_git.py`, `cursor_bridge.py` |
| OpenCode | `opencode_panel.py`, `app.py`, `scripts/install-opencode.sh`, versioned frontend patch |
| Authentication | `app.py`, `panel_totp.py`, gate templates |
| Fleet | `fleet/src/index.ts`, `fleet/public/index.html`, `fleet/CLOUDFLARE.md` |
| Host install/update | `bootstrap.sh`, `install-wizard.sh`, `install.sh`, `agentcontrol.service`, `agentcontrol-caddy.service`, `scripts/install-caddy.sh`, `scripts/panel-update.sh` |
| Sessions | `panel_sessions.py`, `sessions.db` (state dir), `/api/auth/sessions/revoke-all` |

## Panel

- Production uses host systemd on port 30228. Docker panel is disabled. See README for install and migration.
- Config and secrets are under `/etc/agentcontrol`. State, worker logs and update logs are under `/var/lib/agentcontrol` by default. `CONFIG_SEARCH` and `save_config_patch()` handle configuration.
- Scan root defaults to `/root`. A project's `.agentcontrol-ignore` hides it. Workers start as `agentcontrol-<folder>`. Preserve `WORKER_NAMESPACE` because IDs are UUID v5 per folder. Readiness prefers loopback `/readyz`. Idle release defaults to 12 hours.
- Runtime proxy settings come from `/etc/agentcontrol/env`. `xray_client.py` owns the VLESS+Reality client configuration and loopback HTTP inbound. The optional Cursor bridge applies its environment after the proxy.
- Browser auth uses server-side sessions in `/var/lib/agentcontrol/sessions.db` (`panel_sessions.py`, only SHA-256 token hashes stored). TOTP is checked when the session is created. `POST /api/auth/logout` revokes the current session; `POST /api/auth/sessions/revoke-all` revokes all browsers. The panel cookie is HttpOnly/SameSite=Lax (Secure behind TLS) and never contains the password. Automation and Fleet keep using `X-AgentControl-Auth` with the panel password; Bearer auth is also accepted.
- The browser UI no longer stores the password in `localStorage`.
- Bundled Caddy: `scripts/install-caddy.sh` + `agentcontrol-caddy.service` serve TLS on the public `port` and proxy to `127.0.0.1:<internal_port>` (default example 30328). It is a managed binary under the state dir with its own config/unit, so it never conflicts with a system nginx/Caddy and never binds 80/443 for the site. Certificate files can be supplied via `tls_cert_file`/`tls_key_file`; otherwise Let's Encrypt HTTP-01 is used, which needs inbound 80 only for the challenge. The panel must be enabled with `public_hostname` + `internal_port` + `bind_host: 127.0.0.1`; `install.sh` and `panel-update.sh` refresh Caddy when those keys are present.
- The panel polls every 2 seconds and pauses during modal work. Use event delegation for buttons inside refreshed lists.

## OpenCode

- One shared `opencode serve` on loopback port 4096 by default. Preserve the service user and OpenCode data directory across restarts.
- `POST /api/opencode/open/<name>` ensures the service and returns `/oc/?directory=…`. The patched frontend opens the requested project. Reloading a saved session URL uses the same panel cookie and session ID.
- When the binary or web build is missing or mismatched, the project API returns `install_required`. The panel starts installation with `POST /api/opencode/install`, polls its GET, then retries the project. The installer reuses matching binaries and stores a managed binary and install log under the panel state directory. Fleet proxies installation and polls status without KV writes.
- The authenticated same-origin proxy serves the frontend and forwards API, SSE and terminal WebSocket traffic with upstream Basic auth. Keep routes, assets and SDK calls under `/oc/`.
- The frontend is built locally by `scripts/build-opencode-web.sh` (OpenCode 2.4.1 with Bun 1.4.4). Match the backend, pinned source and patch. The build is ignored by Git. Follow the OpenCode section in README for deployment and verification.

## Fleet

- Read [fleet/CLOUDFLARE.md](fleet/CLOUDFLARE.md) before changing cache, polling or KV. Use hostname URLs for panels. Workers cannot fetch raw IPs.
- Fleet authenticates with `X-Fleet-Password`. Per-server credentials stay in KV and are forwarded as `X-AgentControl-Auth`.
- Stored panel URLs may still be `http://`. `proxyAgent` retries `https://` on a TLS-only port (plaintext gets 400) so certificates can be enabled before editing every Fleet entry. Browser links use the panel-provided `public_base` (from `api/opencode/open`) instead of rebuilding from the stored URL.
- Initial load uses `/api/fleet/cache`. Visible tabs rotate one server every 60 seconds without routine KV writes. Hidden tabs stop polling. **Refresh** performs a full snapshot and writes the cache once.
- **Update all panels** calls `POST /api/fleet/update-all` once per confirmed click. It skips paused servers and displays each panel's request result. Accepted requests run asynchronously on each host. The action does not write KV.
- Keep server cards responsive, with two metric columns on mobile and four on desktop.

## Release and verification

- For user-visible changes, bump `version.json` and `fleet/version.json` and sync the latter to `fleet/public/version.json`. Update this guide and the relevant README when behavior or operations change.
- Panel updates fetch and reset to `origin/main`. Push release commits before triggering rollout. A panel restart can stop active workers. Verify `/health` and `/var/lib/agentcontrol/update.log` after updating.
- `.github/workflows/fleet-deploy.yml` deploys Fleet with configured Cloudflare credentials and can trigger panel rollout with `FLEET_URL` and `FLEET_PASSWORD`. Manual Fleet deployment uses `cd fleet && npm run deploy`.
- Run Python tests with `python -m unittest discover -s tests` from the panel venv. Verify Fleet actions on authenticated hostname URLs and OpenCode with a saved-session browser reload, SSE and terminal traffic when those paths change.
