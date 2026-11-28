# Architecture

Verified against the 1.8.1 working tree. Keep this file in sync with code when a
flow changes.

## Components

```text
Browser ──https──> tls_front.py :30228 ──TLS──> Caddy :30443 ──http──> Flask :30328
   │                                                                    │
   │                                                              /oc/ proxy
   │                                                                    │
   └──── plain http :30228 ──308──> https (bare IP: straight to Flask)  v
                                                          opencode serve :4096
Fleet (Cloudflare Worker) ──https:30228──> same panel (X-AgentControl-Auth)
```

| Process | Unit | Bind | Purpose |
|---|---|---|---|
| Flask panel | `agentcontrol.service` | `127.0.0.1:30328` | UI, API, worker supervision, OpenCode proxy |
| HTTPS front | `agentcontrol-tls-front.service` | `0.0.0.0:30228` | HTTP→HTTPS redirect, TLS pass-through to Caddy |
| Caddy | `agentcontrol-caddy.service` | `127.0.0.1:30443` | TLS termination, reverse proxy, ACME |
| OpenCode serve | child of panel | `127.0.0.1:4096` | shared AI coding backend |
| Cursor workers | detached children | per-worker mgmt port | `agent worker` processes |

Config: `/etc/agentcontrol/config.yaml` (repo `config.yaml` is a symlink).
Secrets: `/etc/agentcontrol` (`api-key`, `auth-password`, `totp.json`,
`Caddyfile`, `opencode-secrets.json`, `opencode-providers.env`,
`opencode-serve-password`, optional `env`).
State: `/var/lib/agentcontrol` (see `docs/CONFIG.md`).

## Panel authentication

1. Password lives (hashed) in `auth_password_file`. If missing, `auth_enabled()` is
   false and `GET /` renders `gate_setup.html`; setup endpoints are open until a
   password (and separately an API key) is written. From non-loopback addresses a
   setup token is required.
2. `POST /api/auth/login` verifies Argon2 (upgrading legacy plaintext on success),
   then, when TOTP is enabled, verifies a 6-digit code (`pyotp`, window ±1).
3. Success creates a row in `state_dir/sessions.db`: random 32-byte token, only its
   SHA-256 stored, TTL `session_days` (default 30). Cookie
   `agentcontrol_session` is `HttpOnly`, `SameSite=Lax`, `Secure` when the request
   is HTTPS (ProxyFix trusts one hop when `internal_port` is set).
4. `@app.before_request` guards every `/api/*` route except an explicit open set
   (see `docs/API.md`), enforces same-origin for mutating browser requests, and
   checks the actor role (`read` < `ops` < `admin`). `/oc/*` performs its own
   session + same-origin check.
5. Automation/Fleet use scoped tokens in `X-AgentControl-Token`; the panel password
   in `X-AgentControl-Auth`/Bearer still works as admin for compatibility.
6. TOTP: `panel_totp.py` stores `{enabled, secret, pending_secret}` in
   `totp.json` mode 600. A new secret stays pending until a valid code is entered;
   enabling/disabling revokes all sessions.

## Worker lifecycle

- `GET /api/folders` scans `scan_root` (default `/root`), skipping excluded names,
  dot-prefixed entries and folders containing `.agentcontrol-ignore`.
- Start (`POST /api/start/<name>`):
  1. validate folder, agent binary, API key, proxy/bridge/runtime (Docker socket).
  2. `ensure_worker_git()` may set `origin` from `git_remote_template` /
     `git_remote_overrides` / `.agentcontrol-git-remote`.
  3. run `agent worker --worker-dir <path> --data-dir state_dir/worker-data/<name>
     --name agentcontrol-<name> --idle-release-timeout <s>
     --management-addr 127.0.0.1:<hash port> start --verbose`, detached
     (`start_new_session`), output appended to `state_dir/<name>.log`.
  4. environment: `CURSOR_API_KEY`, `CURSOR_AGENT_WORKER_ID` (UUID v5 of folder),
     `CURSOR_MODEL` when `default_model` is set, plus proxy/bridge env.
  5. persist `{pid, worker_id, started_at, log_file, mgmt_port}` in `workers.json`.
- Readiness: loopback `/readyz` on the management port; fallback scans the current
  log session for known success/error markers; last resort is `worker_ready_timeout`
  (45 s default, capped at 25 s for the log fallback).
- Stop: `SIGTERM` to the process group, then the entry is removed. Restart = stop +
  start. Idle release is enforced by the agent CLI itself.
- `reconcile_state()` prunes entries whose PID is gone; every listing calls it.

## OpenCode

- One `opencode serve` on `127.0.0.1:4096`, started lazily by `ensure_serve()`; PID
  and log in `state_dir/opencode-serve.json` / `opencode-serve.log`.
- Binary and web build are pinned in `scripts/opencode-versions.sh` (currently
  2.4.1 / Bun 1.4.4). The private binary lives in `state_dir/opencode-bin/`; the
  frontend is `opencode-web` → `.opencode-web-builds/<version>.<random>`.
- `POST /api/opencode/open/<name>` returns 409 `install_required` when binary or web
  version differs. Install is asynchronous (`POST /api/opencode/install`), guarded by
  `opencode-install.lock`, logged to `opencode-install.log`.
- `/oc/` serves the patched SPA, proxies API/SSE to 4096 with Basic auth, and bridges
  terminal WebSockets. Navigation falls back to `index.html`; missing `assets/*` 404.
- Launch handoff: when a password is set and TOTP is **not** enabled,
  `open` also returns `opencode_launch_path` (`/api/fleet/sso?token=…`, TTL 180 s,
  single use) so Fleet can open an already-authenticated session. With TOTP enabled
  no token is issued; Fleet opens the panel and the user signs in.

## Security, audit and monitoring

- `panel/security.py`: Argon2 password verifies and upgrades legacy plaintext on
  first login; scoped tokens (`ac_<id>_<secret>`, SHA-256 at rest); a sliding-window
  login limiter (10 failures / 5 min) that also writes `state_dir/auth.log` for the
  fail2ban jail.
- `panel/db.py`: SQLite (`state_dir/panel.db`) for workers, audit, tokens, metrics,
  health samples and incident state. `workers.json` is imported once.
- Every mutating route calls `panel.audit.record`; `/api/audit` + CSV expose it.
- The monitor thread (`panel/monitor.py`) samples metrics every 15 s, checks
  disk/cert/workers/updates every minute, persists health every 5 min and emits
  alerts with throttling. Channels live in `panel/notify.py`.
- `/metrics` exposes Prometheus text; `/status` is a public read-only page;
  `/api/terminal/ws` is an admin-only PTY (config-switchable).

## Fleet (Cloudflare Worker)

- Browser → Worker `/api/*` with an HttpOnly session cookie (or `X-Fleet-Password`
  for CI). The dashboard password is PBKDF2-hashed in KV; server credentials are
  AES-GCM encrypted when `FLEET_ENC_KEY` is set.
- A 5-minute cron checks `/api/health`, keeps hourly samples, and sends
  down/recovery alerts from the Fleet alert config.
- “Update all panels” is canary-first: the first server must return a new version
  and healthy `/health` before the rest are updated.
- KV keys/limits and polling rules: `fleet/CLOUDFLARE.md`.

## Update path

`POST /api/update/apply` → detached `scripts/panel-update.sh` (via `systemd-run` when
available, so the panel restart cannot kill it) → git fetch + `reset --hard
origin/main` → pip install → `enable-caddy.sh` → restart → `/health` poll → on
failure reset to the previous commit → `install-caddy.sh` + logrotate/fail2ban.
Progress/lock in `update.log` / `update.lock`. The UI polls `/api/update/status`.
Fleet's “Update all panels” updates one canary first and stops on failure.
