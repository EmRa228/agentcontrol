# AgentControl — agent guide

Host-systemd panel (Flask) that starts/stops Cursor Cloud Agent workers, fronts a
shared OpenCode server, and pairs with an optional Cloudflare Workers **Fleet** UI.

## Efficient Coding Guidelines

- Prioritize task completion speed without
  sacrificing correctness.
- Make the smallest correct change that
  satisfies the request.
- Start implementation as soon as sufficient
  context is available.
- Read only files directly relevant to
  the current task.
- Avoid unnecessary repository-wide exploration.
- Batch independent file reads and searches
  when possible.
- Do not repeatedly inspect unchanged files.
- Do not perform unsolicited audits,
  refactoring, or optimizations.
- Keep reasoning focused and concise.
- Run targeted tests for modified functionality.
- Run broader tests when changes affect shared
  components, security, migrations, or critical paths.
- Avoid repeating successful tests unless
  relevant code has changed.
- Review the final diff once, unless new
  issues require further review.
- Stop when acceptance criteria are satisfied.
- Report tests actually executed and remaining risks.
- Never claim success if relevant tests fail.

**Read only what you need.** This file is the index; open one `docs/*.md` when the
task needs depth. Never read `templates/index.html` (1.9k lines) or
`fleet/public/index.html` (1.3k lines) whole — grep by id/function name.

| Need | Open |
|---|---|
| Install / update / TLS / rollback / outages | `docs/OPERATIONS.md` |
| Endpoint list + auth model | `docs/API.md` |
| Config keys, env vars, state files | `docs/CONFIG.md` |
| Process tree, worker/OpenCode/Fleet flows | `docs/ARCHITECTURE.md` |
| Local dev, tests, release checklist | `docs/DEVELOPMENT.md` |
| Fleet KV budget + polling rules | `fleet/CLOUDFLARE.md` |
| Fleet deploy steps | `fleet/README.md` |

## Files (verify line counts before reading)

| Area | Files |
|---|---|
| Panel helpers / app factory / hooks | `app.py` (~2.2k lines) |
| Route groups (blueprints) | `panel/routes/{auth,workers,opencode,proxy,settings,system,ops}.py` |
| Typed config + notifications config | `panel/config.py` |
| SQLite (workers, audit, tokens, metrics, health, incidents) | `panel/db.py` |
| Argon2 passwords, API tokens, rate limiting, fail2ban log | `panel/security.py` |
| Audit trail | `panel/audit.py` |
| Telegram/webhook/email/ntfy notifications | `panel/notify.py` |
| Health/cert/disk checks | `panel/health.py` |
| Prometheus + metric samples | `panel/metrics.py` |
| Background monitor + alert transitions | `panel/monitor.py` |
| Admin browser PTY | `panel/terminal.py` |
| Browser sessions (SQLite) | `panel_sessions.py` |
| TOTP state, QR | `panel_totp.py` (`/etc/agentcontrol/totp.json`) |
| Shared OpenCode serve, `/oc/` proxy | `opencode_panel.py` |
| Edge bridge / proxy pool / xray | `cursor_bridge.py`, `proxy_pool.py`, `xray_client.py` |
| Git bootstrap / on-demand git details | `worker_git.py` |
| UI | `templates/index.html`, `templates/gate_*.html`, `static/panel.js` |
| Public status page | `templates/status.html` |
| HTTPS front + Caddy | `scripts/tls_front.py`, `scripts/install-caddy.sh`, `scripts/enable-caddy.sh` |
| Install / update / maintenance | `install.sh`, `scripts/panel-update.sh`, `bootstrap.sh`, `scripts/maintenance.py`, `scripts/install-fail2ban.sh`, `scripts/install-logrotate.sh`, `agentcontrol-maintenance.*` |
| OpenCode install/update (pinned) | `scripts/install-opencode.sh`, `scripts/update-opencode.sh`, `scripts/build-opencode-web.sh`, `scripts/opencode-versions.sh`, `scripts/cleanup-builds.sh` |
| Version sync / password reset | `scripts/sync-versions.py`, `scripts/set-password.py` |
| Fleet | `fleet/src/index.ts`, `fleet/public/index.html`, `fleet/wrangler.jsonc` |
| Tests / CI | `tests/`, `.github/workflows/ci.yml` |

## Runtime (do not change without updating docs/OPERATIONS.md)

```text
30228 public  agentcontrol-tls-front (scripts/tls_front.py)
   TLS        -> 127.0.0.1:30443  Caddy -> 127.0.0.1:30328 Flask
   plain HTTP -> 308 https://HOST:30228   (bare IP -> 30328, no ACME)
4096 loopback shared `opencode serve`
```

- Flask binds `bind_host: 127.0.0.1` on `internal_port`; Caddy never binds 80/443
  for the site (port 80 only for ACME HTTP-01).
- `public_hostname` set -> fixed ACME cert. Empty -> Caddy On-Demand TLS, approved
  per hostname by `GET /api/caddy/allow` (loopback only).
- Install/update order: `enable-caddy.sh` **before** restarting Flask,
  `install-caddy.sh` **after**.
- Never restart `agentcontrol` blindly: the shared OpenCode serve and Cursor
  workers run in its cgroup. Check running workers first.

## Hard invariants

- Host systemd only. `/etc/agentcontrol/HOST_ONLY` guards against Docker; the
  `Dockerfile`/`docker-compose.yml` are legacy and deprecated.
- Worker name `agentcontrol-<folder>`; ID is UUID v5 under a fixed
  `WORKER_NAMESPACE` — changing it breaks existing Cursor workers.
- Scan root defaults to `/root`; `.agentcontrol-ignore` (or `POST /api/hide/<name>`)
  hides a folder; `exclude_prefixes`/`exclude_dirs` filter names.
- Readiness prefers loopback `/readyz`; idle release defaults to 12 h.
- `panel-update.sh` runs `git reset --hard origin/main`: **push before rollout**.
- Browser UI polls every 2 s and pauses during modals; action buttons inside
  refreshed lists must use event delegation.
- Panel password is an **Argon2 hash** in `auth-password`; reset with
  `scripts/set-password.py`. The API key stays plaintext in `api-key`.
  Never log or commit secrets (`api-key`, `auth-password`, `totp.json`,
  `notifications.yaml`, `opencode-secrets.json`, `env`, `.env`).

## Auth

- `POST /api/auth/login` (password, then TOTP when enabled) creates a server-side
  session cookie (default 30 days, hashed tokens in `state_dir/sessions.db`).
- 10 failed attempts per client → 5-minute lockout (in-process) plus a fail2ban
  jail reading `state_dir/auth.log` (installed by `scripts/install-fail2ban.sh`).
- Automation/Fleet should use scoped tokens: `X-AgentControl-Token: ac_…` with
  roles `read` < `ops` < `admin`. The panel password in `X-AgentControl-Auth`
  still works as admin for compatibility.
- `/oc/*` requires a panel session/header and same-origin requests.
- First-run setup needs `/etc/agentcontrol/setup-token` from non-loopback clients
  (`?setup=TOKEN`); loopback is always allowed.
- TOTP setup is server-side: Settings -> authenticator -> QR -> verify 6 digits ->
  all sessions revoked. `pending_secret` stays inactive until verified.

## Security / operations

- `panel.db` (SQLite) holds workers, audit, tokens, metrics, health and incidents;
  `workers.json` is only a legacy import.
- Audit every mutating action (`panel.audit.record`); the UI reads `/api/audit`.
- The monitor thread (`panel/monitor.py`) checks disk/cert/workers/updates and
  sends Telegram/webhook/email/ntfy alerts from `notifications.yaml`.
- CSP uses a per-request nonce; panel JavaScript is external (`static/panel.js`).
  Do not add inline handlers — use `data-action` and the delegated dispatcher.
- A daily `agentcontrol-maintenance.timer` prunes metrics/health/audit, backs up
  `/etc/agentcontrol` + SQLite, cleans old OpenCode builds and checks versions.
- OpenCode upgrades go through `scripts/update-opencode.sh` (digest verification,
  3-way patch, build/smoke, atomic swap, rollback). No auto-apply.

## OpenCode

One shared `opencode serve` on loopback 4096. `POST /api/opencode/open/<name>`
ensures it and returns `/oc/?directory=…`. The patched frontend is built by
`scripts/build-opencode-web.sh` and must match the pinned source
(`scripts/opencode-versions.sh`: OpenCode 2.4.1, Bun 1.4.4, patch
`scripts/opencode-web-2.4.1.patch`). Mismatch returns `install_required`;
install is async and logged to `state_dir/opencode-install.log`. Upgrades:
`scripts/update-opencode.sh --check` / `--version X`, and
`POST /api/opencode/update` (admin). It needs a matching
`scripts/opencode-web-<version>.patch`; `--reuse-patch` tries an older patch with
3-way apply. After a binary upgrade call `POST /api/opencode/restart`.

## Fleet

Hostname URLs only (Workers cannot fetch raw IPs). The dashboard password is hashed
in KV (`fleet_auth`); browsers use an HttpOnly session cookie, and CI/automation may
send `X-Fleet-Password`. Per-server credentials live in KV (AES-GCM when
`FLEET_ENC_KEY` is set) and are forwarded as `X-AgentControl-Auth` (or a token).
Cached first load (`/api/fleet/cache`), one visible tab per 60 s, hidden tabs stop
polling. A 5-minute cron checks `/api/health`, stores hourly samples and sends
down/recovery alerts (`/api/notifications`). “Update all panels” is canary-first:
one server must become healthy before the rest are updated. Estimate KV writes
before changing polling/caching: see `fleet/CLOUDFLARE.md`.

## Verify

```bash
cd /opt/agentcontrol
./venv/bin/python -m unittest discover -s tests          # 101 tests
curl -s http://127.0.0.1:30328/health
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' http://127.0.0.1:30228/health
curl -s https://<public_hostname>:30228/health
curl -s https://<public_hostname>:30228/api/public/status
systemctl status agentcontrol agentcontrol-caddy agentcontrol-tls-front
systemctl list-timers agentcontrol-maintenance.timer
fail2ban-client status agentcontrol        # when installed
journalctl -u agentcontrol -u agentcontrol-caddy -u agentcontrol-tls-front -n 50
tail -n 50 /var/lib/agentcontrol/update.log
```

Browser work: verify a saved-session reload, `/oc/` assets, SSE
(`/oc/global/event`), and terminal WebSocket traffic.

## Release

1. Bump `version.json`, then run `python scripts/sync-versions.py` (updates
   `fleet/version.json`, `fleet/public/version.json`, `fleet/package*.json`).
2. Run tests (`python -m unittest discover -s tests`) and `npx tsc --noEmit` in
   `fleet/`; update `docs/` and README if behavior changed.
3. Push to `main` (CI runs tests, deploys Fleet, and can trigger panel rollout).
4. Roll out panels from Fleet (canary first) or `POST /api/update/apply`, then
   confirm `/health` and `update.log`.

User-visible changes without a version bump are incomplete.
