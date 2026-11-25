# AgentControl agent guide

AgentControl runs Cursor Cloud Agent workers per project on Linux. The optional Fleet dashboard aggregates panels. Keep API keys, panel passwords, xray UUIDs and Reality keys out of Git and logs.

| Area | Entry points |
|---|---|
| Panel | `app.py`, `templates/index.html`, `config.example.yaml` |
| Cursor workers | `app.py`, `worker_git.py`, `cursor_bridge.py` |
| OpenCode | `opencode_panel.py`, `app.py`, `scripts/build-opencode-web.sh`, versioned frontend patch |
| Authentication | `app.py`, `panel_totp.py`, gate templates |
| Fleet | `fleet/src/index.ts`, `fleet/public/index.html`, `fleet/CLOUDFLARE.md` |
| Host install/update | `bootstrap.sh`, `install-wizard.sh`, `install.sh`, `agentcontrol.service`, `scripts/panel-update.sh` |

## Panel

- Production uses host systemd on port 30228. Docker panel is disabled. See README for install and migration.
- Config and secrets are under `/etc/agentcontrol`. State, worker logs and update logs are under `/var/lib/agentcontrol` by default. `CONFIG_SEARCH` and `save_config_patch()` handle configuration.
- Scan root defaults to `/root`. A project's `.agentcontrol-ignore` hides it. Workers start as `agentcontrol-<folder>`. Preserve `WORKER_NAMESPACE` because IDs are UUID v5 per folder. Readiness prefers loopback `/readyz`. Idle release defaults to 12 hours.
- Runtime proxy settings come from `/etc/agentcontrol/env`. `xray_client.py` owns the VLESS+Reality client configuration and loopback HTTP inbound. The optional Cursor bridge applies its environment after the proxy.
- Panel API accepts `X-AgentControl-Auth`, Bearer auth or the panel cookie. Browser login sets the cookie and checks TOTP when enabled.
- The panel polls every 2 seconds and pauses during modal work. Use event delegation for buttons inside refreshed lists.

## OpenCode

- One shared `opencode serve` on loopback port 4096 by default. Preserve the service user and OpenCode data directory across restarts.
- `POST /api/opencode/open/<name>` ensures the service and returns `/oc/?directory=…`. The patched frontend opens the requested project. Reloading a saved session URL uses the same panel cookie and session ID.
- The authenticated same-origin proxy serves the frontend and forwards API, SSE and terminal WebSocket traffic with upstream Basic auth. Keep routes, assets and SDK calls under `/oc/`.
- The frontend is built locally by `scripts/build-opencode-web.sh` (OpenCode 1.19.6 with Bun 1.4.4). Match the backend, pinned source and patch. The build is ignored by Git. Follow the OpenCode section in README for deployment and verification.

## Fleet

- Read [fleet/CLOUDFLARE.md](fleet/CLOUDFLARE.md) before changing cache, polling or KV. Use hostname URLs for panels. Workers cannot fetch raw IPs.
- Fleet authenticates with `X-Fleet-Password`. Per-server credentials stay in KV and are forwarded as `X-AgentControl-Auth`.
- Initial load uses `/api/fleet/cache`. Visible tabs rotate one server every 60 seconds without routine KV writes. Hidden tabs stop polling. **Refresh** performs a full snapshot and writes the cache once.
- **Update all panels** calls `POST /api/fleet/update-all` once per confirmed click. It skips paused servers and displays each panel's request result. Accepted requests run asynchronously on each host. The action does not write KV.
- Keep server cards responsive, with two metric columns on mobile and four on desktop.

## Release and verification

- For user-visible changes, bump `version.json` and `fleet/version.json` and sync the latter to `fleet/public/version.json`. Update this guide and the relevant README when behavior or operations change.
- Panel updates fetch and reset to `origin/main`. Push release commits before triggering rollout. A panel restart can stop active workers. Verify `/health` and `/var/lib/agentcontrol/update.log` after updating.
- `.github/workflows/fleet-deploy.yml` deploys Fleet with configured Cloudflare credentials and can trigger panel rollout with `FLEET_URL` and `FLEET_PASSWORD`. Manual Fleet deployment uses `cd fleet && npm run deploy`.
- Run Python tests with `python -m unittest discover -s tests` from the panel venv. Verify Fleet actions on authenticated hostname URLs and OpenCode with a saved-session browser reload, SSE and terminal traffic when those paths change.
