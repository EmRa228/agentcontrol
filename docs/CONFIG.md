# Configuration, secrets, state

Config search order (`load_config`): `/etc/agentcontrol/config.yaml` →
`<repo>/config.yaml` (symlink) → `<repo>/config.example.yaml`. Missing keys get the
defaults below; environment variables can override several.

## `config.yaml`

| Key | Default | Notes |
|---|---|---|
| `port` | `30228` (`PORT`) | public HTTPS port (front + Fleet URLs) |
| `internal_port` | unset / `0` (`INTERNAL_PORT`) | Flask loopback port; unset = Flask serves `port` directly (no Caddy) |
| `https_internal_port` | `30443` (`HTTPS_INTERNAL_PORT`) | Caddy TLS upstream |
| `bind_host` | `""` (`BIND_HOST`) | set to `127.0.0.1` by `enable-caddy.sh` |
| `public_hostname` | `""` (`PUBLIC_HOSTNAME`) | fixed ACME name; empty = On-Demand TLS |
| `tls_cert_file`, `tls_key_file` | `""` | reuse an existing certificate |
| `acme_email` | `""` (`ACME_EMAIL`) | Let's Encrypt account email |
| `session_days` | `30` (`SESSION_DAYS`) | browser session TTL |
| `max_login_attempts` | `10` | failures before a lockout |
| `lockout_seconds` | `300` | lockout duration |
| `monitor_interval_seconds` | `60` | health/alert loop |
| `metrics_interval_seconds` | `15` | metric sample interval |
| `metrics_retention_hours` | `168` | metric retention |
| `update_check_seconds` | `21600` | background update check |
| `disk_warn_percent` / `disk_crit_percent` | `85` / `95` | alert thresholds |
| `cert_warn_days` / `cert_crit_days` | `21` / `7` | certificate thresholds |
| `status_page_enabled` | `true` | public `/status` + `/api/public/status` |
| `metrics_public` | `false` | `/metrics` without token/loopback |
| `terminal_enabled` | `true` | admin browser terminal |
| `terminal_shell` | `/bin/bash` | PTY shell |
| `terminal_max_seconds` | `3600` | terminal session cap |
| `notifications_file` | `/etc/agentcontrol/notifications.yaml` | alert channels |
| `scan_root` | `/root` (`SCAN_ROOT`) | project folders = direct children |
| `idle_release_seconds` | `43200` (`IDLE_RELEASE_SECONDS`) | passed to `agent worker` |
| `worker_ready_timeout` | `45` (`WORKER_READY_TIMEOUT`) | readiness fallback window |
| `exclude_prefixes` | `["."]` | skip names starting with these |
| `exclude_dirs` | `[]` | skip exact names |
| `api_key_file` | `/etc/agentcontrol/api-key` (`API_KEY_FILE`) | Cursor key |
| `auth_password_file` | `/etc/agentcontrol/auth-password` (`AUTH_PASSWORD_FILE`) | panel password |
| `state_dir` | `/var/lib/agentcontrol` (`STATE_DIR`) | state/logs |
| `agent_bin` | `""` (`AGENT_BIN`) | empty = search PATH / `~/.local/bin` |
| `default_model` | `""` (`DEFAULT_MODEL`) | appended to the Cursor URL + `CURSOR_MODEL` |
| `git_remote_template` | `""` (`GIT_REMOTE_TEMPLATE`) | e.g. `git@host:org/{name}.git` |
| `git_remote_overrides` | `{}` | per-folder origin map |
| `opencode_bin` | `""` (`OPENCODE_BIN`) | empty = managed binary in `state_dir/opencode-bin` |
| `opencode_port` | `4096` (`OPENCODE_PORT`) | loopback only |
| `opencode_hostname` | `127.0.0.1` (`OPENCODE_HOSTNAME`) | keep loopback |
| `opencode_secrets_file` | `/etc/agentcontrol/opencode-secrets.json` | provider keys |
| `opencode_env_file` | `/etc/agentcontrol/opencode-providers.env` | provider env |
| `opencode_serve_password_file` | `/etc/agentcontrol/opencode-serve-password` | Basic auth for serve |
| `caddy_bin` | `""` | optional external Caddy (installer default: `state_dir/bin/caddy`) |

Per-project overrides: `<folder>/.agentcontrol-ignore` hides a folder;
`<folder>/.agentcontrol-git-remote` sets its origin (first line wins).
`exclude_dirs` in the shipped example contains incident/scratch names — treat it as
host-local policy, not product behavior.

## Environment variables

`PORT`, `SCAN_ROOT`, `IDLE_RELEASE_SECONDS`, `API_KEY_FILE`, `AUTH_PASSWORD_FILE`,
`STATE_DIR`, `AGENT_BIN`, `WORKER_READY_TIMEOUT`, `DEFAULT_MODEL`,
`GIT_REMOTE_TEMPLATE`, `OPENCODE_BIN`, `OPENCODE_PORT`, `OPENCODE_HOSTNAME`,
`OPENCODE_SECRETS_FILE`, `OPENCODE_ENV_FILE`, `OPENCODE_SERVE_PASSWORD_FILE`,
`INTERNAL_PORT`, `BIND_HOST`, `PUBLIC_HOSTNAME`, `TLS_CERT_FILE`, `TLS_KEY_FILE`,
`ACME_EMAIL`, `SESSION_DAYS`, `INSTALL_DIR`, `AGENTCONTROL_GITHUB_RAW`,
`AGENTCONTROL_TOTP_FILE`, `PROXY_POOL_FILE`, `PROXY_POOL_STATUS_FILE`,
`XRAY_CONFIG`, `XRAY_CLIENT_FILE`, `XRAY_STATUS_FILE`, `GIT_FETCH_TIMEOUT`,
`NOTIFICATIONS_FILE`, `PANEL_DISABLE_BACKGROUND`.

Installer-only: `CURSOR_API_KEY`, `PANEL_PASSWORD`, `AGENTCONTROL_REPO`,
`AGENTCONTROL_NETWORK_MODE` (`1` direct / `2` xray), `AGENTCONTROL_PROXY_PORT`,
`WIZARD`, `CONFIG_DIR`, `CADDY_VERSION`, `BUN_BIN`.

## Secrets (`/etc/agentcontrol`, never in Git/logs)

| File | Content |
|---|---|
| `api-key` | Cursor personal API key |
| `auth-password` | panel password (**Argon2 hash**, 600) |
| `setup-token` | first-run token for non-loopback setup |
| `notifications.yaml` | alert channels/events/thresholds |
| `totp.json` | `{enabled, secret, pending_secret}` |
| `opencode-secrets.json` | OpenAI/Anthropic/OpenCode Go keys |
| `opencode-providers.env` | provider env for `opencode serve` |
| `opencode-serve-password` | Basic auth password for port 4096 |
| `Caddyfile` | generated by `install-caddy.sh` |
| `.env` | Cursor edge bridge (`CURSOR_BRIDGE_*`, `CURSOR_API_ENDPOINT`) |
| `proxy-pool.yaml`, `xray-client.yaml` | proxy/xray settings |
| `totp.json`, `env` | optional systemd EnvironmentFile |
| `HOST_ONLY` | guard: panel must not run in Docker |

## State (`/var/lib/agentcontrol`)

| Path | Purpose |
|---|---|
| `panel.db` | SQLite: workers, audit, tokens, metrics, health, incidents |
| `workers.json` | legacy worker state (imported once) |
| `sessions.db` | SQLite browser sessions (hashes only, WAL) |
| `auth.log` | failed logins for fail2ban |
| `notifications.yaml` | in `/etc/agentcontrol`, not state |
| `recent-touches.json` | last manual/UI activity per project |
| `<project>.log` | worker stdout/stderr (appended per start, marker-separated) |
| `worker-data/<project>/` | `--data-dir` for the Cursor agent |
| `update.log`, `update.lock` | panel update output and PID lock |
| `opencode-bin/`, `opencode-serve.json`, `opencode-serve.log` | managed OpenCode |
| `opencode-install.log`, `opencode-install.lock` | async installer |
| `bin/caddy`, `caddy/` | bundled Caddy binary + data |
| `proxy-pool-status.json`, `xray-status.json` | proxy status caches |

Disk growth watch: worker logs are never rotated by the panel (largest files in
practice), and `caddy/` keeps ACME state. Back up `/etc/agentcontrol` plus
`opencode` config/data before service changes.
