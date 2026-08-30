# AGENTS.md — AgentControl

Token-efficient context for AI agents. Prefer this file over re-reading `README.md` and the full tree unless you are changing a specific module.

**Do not commit secrets.** Never paste API keys, xray UUIDs, Reality keys, or panel passwords into docs or commits.

---

## Product

**AgentControl** is a lightweight web panel to **start/stop Cursor Cloud Agent workers** on Linux servers.

- Lists project folders under a configurable root (default `/root`).
- **Start** → runs `agent worker` as `agentcontrol-<folder>`; opens `cursor.com/agents#workerId=...`.
- **Stop** → terminates the worker process group.
- Server stats: CPU, RAM, disk, swap, network, Docker containers, active workers.
- Stable worker ID per folder (deterministic UUID v5).
- Auto idle-release after **12 hours** (`idle_release_seconds`, default `43200`).
- **Per-server panel password** — stored in browser `localStorage`; server copy in `/etc/agentcontrol/auth-password`.
- **Cursor API key** optional at install; can be set later in dashboard Settings.
- **xray client config** — VLESS+Reality upstream + local HTTP inbound for restricted networks (Iran, etc.).

Repo: `https://github.com/EmRa228/agentcontrol`  
Default install path: `/opt/agentcontrol`

---

## Architecture

```
Phone/Browser → Flask panel :30228
                    │
                    ├─► agent CLI (`agent worker`) per project folder
                    │       └─► cursor.com (via HTTP_PROXY when xray enabled)
                    │
Host xray (:30229 HTTP inbound) ─► VLESS+Reality outbound ─► upstream proxy server
```

| Runtime | Role |
|---|---|
| **Docker** (default) | `agentcontrol` container: Flask + agent CLI; `network_mode: host`, `pid: host` |
| **Host xray** | Local HTTP proxy (`127.0.0.1:30229` default) + upstream outbound |
| **Legacy systemd** | `agentcontrol.service` → venv + `app.py` (no Docker) |

**Default install is Docker**, not systemd. Container mounts:

- `${SCAN_ROOT}` (default `/root`) — project folders for workers
- `/etc/agentcontrol` — config, secrets, xray client yaml, runtime env
- `/var/lib/agentcontrol` — worker state, logs
- `/usr/local/etc/xray` — host xray `config.json` (read/write for apply)

Docker build uses **direct** network (Debian/PyPI). **Proxy applies at runtime** via `/etc/agentcontrol/env` (`HTTP_PROXY`/`HTTPS_PROXY`).

Worker management ports: `127.0.0.1:32000–32800` (hash of folder name).

---

## How to run

```bash
# Fresh install (interactive wizard)
sudo bash -c 'git clone https://github.com/EmRa228/agentcontrol.git /opt/agentcontrol && /opt/agentcontrol/bootstrap.sh'

# Update / reinstall (idempotent)
sudo /opt/agentcontrol/bootstrap.sh

# Non-interactive — proxy mode, import existing xray client
sudo XRAY_IMPORT_ONLY=1 AGENTCONTROL_NETWORK_MODE=2 AGENTCONTROL_PROXY_PORT=30229 SCAN_ROOT=/root /opt/agentcontrol/bootstrap.sh

# Legacy systemd (no Docker)
sudo LEGACY_INSTALL=1 /opt/agentcontrol/bootstrap.sh

# Docker ops
cd /opt/agentcontrol
docker compose ps
docker compose logs -f
docker compose restart
curl -sS http://127.0.0.1:30228/health
```

After meaningful changes:

| If you changed… | Command |
|---|---|
| `app.py`, `templates/`, `xray_client.py`, `Dockerfile` | `cd /opt/agentcontrol && docker compose up -d --build` |
| `install-wizard.sh`, `scripts/*` only | rerun `./install-wizard.sh` or `./bootstrap.sh` |
| `/etc/agentcontrol/xray-client.yaml` manually | `python3 scripts/apply-xray-client.py` |

Panel URL: `http://SERVER_IP:30228`

---

## Install wizard (`install-wizard.sh`)

Called by `bootstrap.sh` unless `LEGACY_INSTALL=1`.

| Prompt / env | Default | Notes |
|---|---|---|
| Network mode `1`/`2` | `2` (proxy) | `AGENTCONTROL_NETWORK_MODE` |
| Proxy port | `30229` | `AGENTCONTROL_PROXY_PORT`; local HTTP inbound only |
| scan_root | `/root` | `SCAN_ROOT` |
| xray client fields | import from host | `XRAY_IMPORT_ONLY=1` skips prompts |
| Cursor API key | optional | `CURSOR_API_KEY` or dashboard later |
| Panel password | optional | `PANEL_PASSWORD` or dashboard later |

xray client env (non-interactive): `XRAY_ADDRESS`, `XRAY_PORT`, `XRAY_UUID`, `XRAY_SERVER_NAME`, `XRAY_PUBLIC_KEY`, `XRAY_SHORT_ID`, `XRAY_FINGERPRINT`, `XRAY_FLOW`.

---

## Configuration files

| Path | Purpose |
|---|---|
| `/etc/agentcontrol/config.yaml` | Panel: port, scan_root, idle timeout, agent_bin, default_model |
| `/etc/agentcontrol/api-key` | Cursor personal API key |
| `/etc/agentcontrol/auth-password` | Panel password |
| `/etc/agentcontrol/env` | Runtime `HTTP_PROXY` for container + workers |
| `/etc/agentcontrol/xray-client.yaml` | Canonical xray client settings (AgentControl-managed) |
| `/usr/local/etc/xray/config.json` | Host xray config (inbound + outbound applied here) |
| `/var/lib/agentcontrol/workers.json` | Running worker PIDs/state |
| `/var/lib/agentcontrol/<folder>.log` | Per-worker logs |

`config.example.yaml` keys: `port` (30228), `scan_root`, `idle_release_seconds`, `api_key_file`, `auth_password_file`, `state_dir`, `exclude_prefixes`, `exclude_dirs`, `default_model`, `agent_bin`.

Hide a project from the list: create `.agentcontrol-ignore` in the folder (or POST `/api/hide/<name>`).

---

## xray client (`xray_client.py`)

Manages **upstream VLESS+Reality outbound** + **local HTTP inbound** (`agentcontrol-http-in`).

- Canonical store: `/etc/agentcontrol/xray-client.yaml`
- Applies to host `/usr/local/etc/xray/config.json` (outbound tag default `reality-out`)
- Restarts xray via `systemctl` or `nsenter` (container needs `cap_add: SYS_ADMIN`, `pid: host`)
- Writes `/etc/agentcontrol/env` with `http://127.0.0.1:<proxy_port>`

CLI:

```bash
python3 scripts/apply-xray-client.py --import   # import from xray config.json
python3 scripts/apply-xray-client.py --show
python3 scripts/apply-xray-client.py --test
python3 scripts/apply-xray-client.py            # apply saved settings
```

UI: panel → server section → **xray client (proxy)** — edit, Save & apply, Test, Import.

Env overrides: `XRAY_CONFIG`, `XRAY_CLIENT_FILE`.

---

## Request flows

### Start worker

1. POST `/api/start/<folder>` (auth required).
2. `start_worker()` → `agent worker --worker-dir <path> --name agentcontrol-<folder> ... start`.
3. Env includes `CURSOR_API_KEY`, `CURSOR_AGENT_WORKER_ID`, optional `CURSOR_MODEL`, and `HTTP_PROXY` from container env.
4. Ready probe: tail log for `connected`/`ready` markers; GET `/api/ready/<name>`.

### First visit / setup

Open endpoints: `/`, `/health`, `/api/auth/*`, `/api/setup/*` (password always; api-key only when not yet configured).

Dashboard setup gate: panel password (required) → Cursor API key (optional at install, required to start workers).

### Auth

Header: `X-AgentControl-Auth: <password>` or `Authorization: Bearer <password>`.

---

## API (panel)

| Method | Path | Auth |
|---|---|---|
| GET | `/health` | no |
| GET | `/` | no |
| POST | `/api/auth/login` | no |
| GET | `/api/auth/status` | no |
| GET | `/api/setup/status` | no |
| POST | `/api/setup/password` | no (first-time only) |
| POST | `/api/setup/api-key` | no if unset; else yes |
| GET | `/api/system` | yes |
| GET | `/api/system/history` | yes |
| GET | `/api/folders` | yes |
| POST | `/api/start/<name>` | yes |
| POST | `/api/stop/<name>` | yes |
| POST | `/api/hide/<name>` | yes |
| GET | `/api/ready/<name>` | yes |
| GET | `/api/status` | yes |
| GET | `/api/models` | yes |
| GET | `/api/settings` | yes |
| POST | `/api/settings/model` | yes |
| GET | `/api/xray/client` | yes |
| POST | `/api/xray/client` | yes |
| POST | `/api/xray/client/import` | yes |
| POST | `/api/xray/client/test` | yes |

---

## Repo map

```
/opt/agentcontrol
├── AGENTS.md              ← this file
├── README.md              ← human install docs
├── bootstrap.sh           ← idempotent entry: git pull → install-wizard (or legacy install.sh)
├── install-wizard.sh      ← Docker + xray wizard
├── install.sh             ← legacy systemd install
├── app.py                 ← Flask app + worker control
├── xray_client.py         ← xray client apply/import/test
├── config.example.yaml
├── Dockerfile
├── docker-compose.yml
├── docker/entrypoint.sh
├── agentcontrol.service   ← legacy systemd unit
├── templates/index.html   ← single-page panel UI
├── scripts/
│   ├── apply-xray-client.py
│   └── setup-xray-proxy.sh  ← thin wrapper (legacy)
└── fleet/                 ← Cloudflare Workers multi-server dashboard (separate deploy)
```

---

## Fleet (Cloudflare Workers)

Optional multi-server dashboard in `fleet/`. **Do not change** unless explicitly asked.

- Deploy: `cd fleet && npm install && wrangler login` → KV → `FLEET_PASSWORD` → `npm run deploy`
- Workers cannot `fetch()` raw IPs — use DNS hostname (grey cloud) + port `30228`
- See `fleet/README.md`

---

## Coding conventions for agents

- Keep logic in `app.py` / `xray_client.py`; bash only for install/bootstrap.
- Default path: **Docker + wizard**; preserve `LEGACY_INSTALL=1` systemd path.
- xray: edit via `xray_client.py` API/CLI — do not hand-edit `config.json` without backup.
- Never log or commit secrets from `/etc/agentcontrol/`.
- Panel copy: English in code/templates; README has short Persian section.
- Worker spawn must inherit proxy env from `os.environ` (already in `start_worker`).
- After app/template changes on a live server: `docker compose up -d --build` from `/opt/agentcontrol`.
- `fleet/` is out of scope unless the task says otherwise.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `pip`/PyPI timeout on host | Use Docker install; build is direct, runtime uses xray proxy |
| Worker cannot reach Cursor | Check `/etc/agentcontrol/env`, xray listening on proxy port, outbound config |
| xray apply fails in container | Ensure `/usr/local/etc/xray` mounted, `SYS_ADMIN` + `pid: host` |
| Panel login loop | Wrong password; check `/etc/agentcontrol/auth-password` |
| Start fails: no API key | Set in Settings or `/etc/agentcontrol/api-key` |
| Port 30228 in use | `docker compose ps`; stop legacy `systemctl stop agentcontrol` |
| Proxy build fails | Expected — wizard builds direct; proxy is runtime-only |

---

## Quick file pointers

| Task | Start here |
|---|---|
| Worker start/stop | `app.py` → `start_worker`, `stop_worker` |
| Folder listing | `app.py` → `list_folders`, `folder_path` |
| Install / Docker | `install-wizard.sh`, `docker-compose.yml`, `Dockerfile` |
| xray client | `xray_client.py`, `scripts/apply-xray-client.py` |
| Panel UI | `templates/index.html` |
| Fleet dashboard | `fleet/src/index.ts`, `fleet/README.md` |
