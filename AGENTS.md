# AGENTS.md — AgentControl

Token-efficient context for AI agents. Prefer this file over re-reading `README.md` and the full tree unless you are changing a specific module.

**Do not commit secrets.** Never paste API keys, xray UUIDs, Reality keys, or panel passwords into docs or commits.

---

## Product

**AgentControl** is a lightweight Linux web panel that **starts/stops Cursor Cloud Agent workers** (`agent worker`) per project folder. Optional **Fleet** dashboard (Cloudflare Workers) aggregates many servers into one UI.

**Versioning:** bump `version.json` (panel) and `fleet/version.json` (fleet) on every user-visible change; version is shown in each UI header and exposed via `/api/version`.

| Component | Stack | Runs on |
|-----------|-------|---------|
| Single-server panel | Python 3.10+ Flask, vanilla HTML/JS | Each Linux VPS (**host systemd** — Docker panel disabled) |
| Fleet dashboard | Cloudflare Workers (TypeScript), KV, static `public/` | Cloudflare edge |

- Lists project folders under `scan_root` (default `/root`).
- **Start** → `agent worker` as `agentcontrol-<folder>`; opens `cursor.com/agents#workerId=...`.
- **Stop** → terminates worker process group.
- Server stats: CPU, RAM, disk, swap, network, Docker, active workers.
- Stable worker ID per folder (deterministic UUID v5).
- Auto idle-release after **12 hours** (`idle_release_seconds`, default `43200`).
- **Panel password** — `/etc/agentcontrol/auth-password`; browser `localStorage`.
- **Cursor API key** optional at install; required to start workers (dashboard Settings).
- **xray client** — VLESS+Reality upstream + local HTTP inbound for restricted networks (Iran, etc.).

**Repo:** https://github.com/EmRa228/agentcontrol  
**Default install path:** `/opt/agentcontrol`  
**Default panel port:** `30228`

---

## Architecture

```
Browser → Flask panel :30228
            ├─ GET /api/system, /api/folders, …
            └─ POST /api/start|stop/<name> → agent worker
                    └─► cursor.com (via HTTP_PROXY when xray enabled)

Host xray (127.0.0.1:30229 HTTP inbound) ─► VLESS+Reality outbound ─► upstream proxy
```

| Runtime | Role |
|---|---|
| **Host systemd** (default) | `agentcontrol.service` → venv + `app.py` on host; workers inherit `/var/run/docker.sock` |
| **Host xray** | Local HTTP proxy (default `127.0.0.1:30229`) + upstream outbound |
| ~~Docker~~ | **Disabled** — `docker/entrypoint.sh` exits; use `install.sh` or `scripts/migrate-from-docker.sh` |

**Default install is host systemd** (`bootstrap.sh` → `install.sh`). Docker panel/workers cannot access host `docker.sock`.

Docker **build** is direct (Debian/PyPI). **Proxy applies at runtime** via `/etc/agentcontrol/env`.

**Worker details:**

- **Worker ID:** `uuid.uuid5(WORKER_NAMESPACE, f"agentcontrol:{folder}")` — **never change** `WORKER_NAMESPACE`.
- **Worker name in Cursor:** `agentcontrol-<folder>`.
- **Redirect URL:** `https://cursor.com/agents#workerId={id}` (optional `?model=` from `default_model`).
- **Management ports:** `127.0.0.1:32000–32800` (hash of folder name).
- **Hide project:** `.agentcontrol-ignore` in folder; POST `/api/hide/<name>`.
- **Metrics history:** in-memory `deque` (max 1800 points, ~2s refresh → ~1h). Fields: `cpu`, `load_pct`, `ram`, `disk`, `swap`, `net`, `docker_running`.

### Fleet

```
Browser → Worker (*.workers.dev or custom domain)
            ├─ KV: server list { id, name, url, password }
            ├─ GET /api/fleet/snapshot  → one-shot poll (used by UI)
            └─ proxy → each server /api/* with X-AgentControl-Auth
```

**Live updates:** client polls `/api/fleet/snapshot` every **25s** while the tab is **visible**; polling **stops** when the tab is hidden (`document.visibilitychange`) to reduce Worker requests. Exponential backoff on errors (max 120s). Manual **Refresh** triggers an immediate snapshot.

**Version:** `fleet/version.json` → UI header, `/version.json`, `GET /api/version`.

**Critical:** Cloudflare Workers **cannot fetch raw IP addresses** (error 1003). Fleet URLs must use a **hostname** with grey-cloud DNS A record, e.g. `http://ac-tg-uk.example.com:30228`.

---

## Version files

| File | Component | Shown in UI | API |
|------|-----------|-------------|-----|
| `version.json` (repo root) | panel | header subtitle `vX.Y.Z` | `GET /api/version`, `GET /version.json`, `GET /health` |
| `fleet/version.json` | fleet | fleet header subtitle | `GET /api/version`, static `/version.json` |

**Bump both** (keep versions in sync unless intentionally diverging) whenever you ship panel or fleet UI/backend changes. Copy `fleet/version.json` → `fleet/public/version.json` for static asset serving.

---

```bash
# Fresh install (host systemd)
sudo bash -c 'git clone https://github.com/EmRa228/agentcontrol.git /opt/agentcontrol && /opt/agentcontrol/bootstrap.sh'

# Migrate off Docker panel (one-time, on host SSH)
sudo bash /opt/agentcontrol/scripts/migrate-from-docker.sh

# Update / reinstall (idempotent)
sudo /opt/agentcontrol/bootstrap.sh

# Host ops
systemctl status agentcontrol
journalctl -u agentcontrol -f
curl -sS http://127.0.0.1:30228/health
```

Open firewall: `30228/tcp`.

| If you changed… | Command |
|---|---|
| `app.py`, `templates/`, `xray_client.py` | `cd /opt/agentcontrol && git pull && sudo bash install.sh` |
| `install.sh`, `scripts/*` only | `sudo bash /opt/agentcontrol/bootstrap.sh` |
| `/etc/agentcontrol/xray-client.yaml` manually | `python3 scripts/apply-xray-client.py` |

### Fleet deploy

```bash
cd fleet
npm install
npx wrangler login
npx wrangler kv namespace create KV   # paste id into wrangler.jsonc
npx wrangler secret put FLEET_PASSWORD
npm run deploy
```

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

xray env (non-interactive): `XRAY_ADDRESS`, `XRAY_PORT`, `XRAY_UUID`, `XRAY_SERVER_NAME`, `XRAY_PUBLIC_KEY`, `XRAY_SHORT_ID`, `XRAY_FINGERPRINT`, `XRAY_FLOW`.

---

## Configuration

### Runtime paths (not in git)

| Path | Purpose |
|------|---------|
| `/opt/agentcontrol` | App install dir |
| `/etc/agentcontrol/config.yaml` | Panel config |
| `/etc/agentcontrol/api-key` | Cursor personal API key |
| `/etc/agentcontrol/auth-password` | Panel password |
| `/etc/agentcontrol/env` | Runtime `HTTP_PROXY` for container + workers |
| `/etc/agentcontrol/xray-client.yaml` | Canonical xray client settings |
| `/usr/local/etc/xray/config.json` | Host xray (inbound + outbound applied here) |
| `/var/lib/agentcontrol/workers.json` | Worker PIDs/state |
| `/var/lib/agentcontrol/<folder>.log` | Per-worker logs |

### `config.yaml` keys

| Key | Default | Meaning |
|-----|---------|---------|
| `port` | `30228` | Flask listen port |
| `scan_root` | `/root` | Project folders root |
| `idle_release_seconds` | `43200` | Auto-stop idle workers |
| `api_key_file` | `/etc/agentcontrol/api-key` | Cursor API key |
| `auth_password_file` | `/etc/agentcontrol/auth-password` | Panel password |
| `state_dir` | `/var/lib/agentcontrol` | PIDs, logs, state JSON |
| `default_model` | `""` | Appended to Cursor redirect URL |
| `agent_bin` | `""` | Path to `agent` CLI (auto-detected) |
| `exclude_prefixes` | `["."]` | Skip dirs starting with these |
| `exclude_dirs` | `[...]` | Exact dir names to skip |

---

## xray client (`xray_client.py`)

Manages **upstream VLESS+Reality outbound** + **local HTTP inbound** (`agentcontrol-http-in`).

- Canonical store: `/etc/agentcontrol/xray-client.yaml`
- Applies to `/usr/local/etc/xray/config.json` (outbound tag default `reality-out`)
- Restarts xray via `systemctl` or `nsenter` (container: `cap_add: SYS_ADMIN`, `pid: host`)
- Writes `/etc/agentcontrol/env` with `http://127.0.0.1:<proxy_port>`

```bash
python3 scripts/apply-xray-client.py --import
python3 scripts/apply-xray-client.py --show
python3 scripts/apply-xray-client.py --test
python3 scripts/apply-xray-client.py
```

UI: panel → server section → **xray client (proxy)** — edit, Save & apply, Test, Import.  
Env overrides: `XRAY_CONFIG`, `XRAY_CLIENT_FILE`.

---

## Authentication

| Layer | Header / storage | Notes |
|-------|------------------|-------|
| Panel | `X-AgentControl-Auth: <password>` | or `Authorization: Bearer`; `localStorage` key `agentcontrol_auth_<host>` |
| Fleet | `X-Fleet-Password: <secret>` | Wrangler secret `FLEET_PASSWORD`; `localStorage` key `agentcontrol_fleet_pw` |
| Fleet → server | `X-AgentControl-Auth` | Per-server password in KV (never returned to browser) |

First visit: `/api/setup/password` and `/api/setup/api-key` when files missing.

---

## API (single server)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/health` | no | `{ ok, component, version }` |
| GET | `/api/version` | no | Panel `version.json` |
| GET | `/version.json` | no | Panel `version.json` |
| GET | `/` | no | Panel HTML |
| POST | `/api/auth/login` | no | Validate password |
| GET | `/api/auth/status` | no | Auth + setup flags |
| GET | `/api/setup/status` | no | Needs password / API key |
| POST | `/api/setup/password` | no | First-time password |
| POST | `/api/setup/api-key` | no if unset; else yes | API key |
| GET | `/api/system` | yes | Full system snapshot |
| GET | `/api/system/history` | yes | `{ points: [...] }` |
| GET | `/api/folders` | yes | Projects (mtime desc) |
| GET | `/api/models` | yes | `agent --list-models` |
| GET | `/api/settings` | yes | `default_model`, etc. |
| POST | `/api/settings/model` | yes | Save default model |
| GET | `/api/xray/client` | yes | xray client settings + test |
| POST | `/api/xray/client` | yes | Save & apply xray |
| POST | `/api/xray/client/import` | yes | Import from xray config |
| POST | `/api/xray/client/test` | yes | Test HTTP proxy |
| POST | `/api/start/<name>` | yes | Start worker |
| POST | `/api/stop/<name>` | yes | Stop worker |
| GET | `/api/ready/<name>` | yes | Running + cloud ready |
| POST | `/api/hide/<name>` | yes | `.agentcontrol-ignore` |
| GET | `/api/status` | yes | Legacy summary |

## API (Fleet Worker)

| Method | Path | Auth |
|--------|------|------|
| GET | `/api/version` | no |
| POST | `/api/login` | body `{ password }` |
| GET | `/api/servers` | Fleet password |
| POST | `/api/servers` | Add server (probes `/api/system`) |
| DELETE | `/api/servers/<id>` | Remove server |
| GET | `/api/fleet/snapshot` | One-shot snapshot (UI poll target) |
| POST | `/api/fleet/<serverId>/start\|stop/<project>` | Proxy |
| GET | `/api/fleet/<serverId>/ready/<project>` | Proxy |

---

## Repo map

```
agentcontrol/
├── AGENTS.md              ← this file (update on every change)
├── version.json           ← panel version (bump on panel changes)
├── README.md              ← human install docs (EN + FA snippet)
├── bootstrap.sh           ← idempotent: git pull → install-wizard (or legacy install.sh)
├── install-wizard.sh      ← Docker + xray wizard
├── install.sh             ← legacy systemd install
├── app.py                 ← Flask backend + worker lifecycle
├── xray_client.py         ← xray client apply/import/test
├── config.example.yaml
├── Dockerfile
├── docker-compose.yml
├── docker/entrypoint.sh
├── agentcontrol.service   ← legacy systemd unit
├── templates/index.html   ← single-server UI
├── scripts/
│   ├── apply-xray-client.py
│   └── setup-xray-proxy.sh
└── fleet/
    ├── version.json       # fleet version (sync to public/version.json)
    ├── src/index.ts       # Worker: proxy API, KV, /api/version
    ├── public/index.html  # Fleet UI (poll + visibility pause)
    ├── public/version.json
    ├── wrangler.jsonc
    └── README.md
```

---

## UI conventions

### Single-server (`templates/index.html`)

- Version in header subtitle from `version.json` (template `{{ version.version }}`).
- Dark theme CSS variables: `--bg`, `--card`, `--accent`, etc.
- Auto-refresh every **2s**; pauses when modals open (`modalOpen`, `busy`).
- Event delegation on `#list` for Start/Stop.
- Sparklines: CPU (load overlay), RAM, Disk, Network; swap on RAM card + detail grid.
- Docker: compose groups with CPU/RAM bars (`docker.groups` from backend).
- Long-press / right-click → hide project.
- **xray client** form in server settings section.

### Fleet (`fleet/public/index.html`)

- Live updates via **polling** `GET /api/fleet/snapshot` every **25s** while tab is visible.
- **Tab hidden:** polling stops; live indicator shows `○ paused` (saves Worker requests).
- **Backoff:** failed polls retry with exponential delay up to 120s.
- Start modal: 4-step progress, live worker logs, no auto-redirect to Cursor on errors.
- Auth: `localStorage` key `agentcontrol_fleet_pw` (persistent login).
- **Main panel** toolbar button + per-server **Open panel** / **Set main**.
- Version in header from `/version.json`.
- Server cards: 4 metrics; **2×2 grid mobile**, 4 columns desktop.
- **Quick agents:** top 3 folders by mtime as chips on card header.
- **Server sort:** `localStorage` `agentcontrol_fleet_recent` — recent servers float up.
- Avoid `minmax(130px)` grids that cause horizontal scroll on mobile.

---

## Backend conventions

- **Python:** stdlib-first; minimal deps (`flask`, `pyyaml`); no ORM.
- **Config:** `CONFIG_SEARCH` chain; `save_config_patch()` for runtime updates.
- **Docker stats:** `docker stats` + `docker inspect` labels; grouped by compose project.
- **Do not change** `WORKER_NAMESPACE`.
- **Workers:** inherit `os.environ` including `HTTP_PROXY` in `start_worker()`.
- **xray:** edit via `xray_client.py` — backups created on apply.
- Default deploy path: **Docker + wizard**; keep `LEGACY_INSTALL=1` systemd path.

### Adding API fields

1. Add to `collect_system_info()` / `record_metrics()` in `app.py`.
2. Update `templates/index.html` `renderSystem()` if panel should show it.
3. Fleet: `snapshotOneServer()` forwards full `system` + `history`; update Fleet UI only unless summary shortcuts needed.

---

## Local development

```bash
cd agentcontrol
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
python app.py
```

Fleet: `cd fleet && npm run dev` (usually `http://localhost:8787`).

---

## Testing checklist

1. **Panel:** `curl -H "X-AgentControl-Auth: pass" http://127.0.0.1:30228/api/system`
2. **Panel UI:** login, metrics, start/stop, hide, xray save/test, Docker expand.
3. **Docker:** `docker compose up -d --build`; `/health` OK; proxy env in container.
4. **Fleet:** hostname URL only; poll + visibility pause; mobile layout; version label.
5. **Restart:** workers reconcile via `reconcile_state()`.

---

## Troubleshooting

| Issue | Cause | Fix |
|-------|-------|-----|
| Cloudflare error 1003 | Fleet URL uses raw IP | Hostname + grey-cloud A record |
| `pip`/PyPI timeout on host | Restricted network | Docker install; build direct, runtime proxy |
| Worker cannot reach Cursor | Proxy/xray misconfigured | `/etc/agentcontrol/env`, xray outbound, test in UI |
| xray apply fails in container | Missing mounts/caps | Mount `/usr/local/etc/xray`, `SYS_ADMIN` + `pid: host` |
| Start fails | No API key | `/etc/agentcontrol/api-key` or Settings |
| Empty project list | Wrong `scan_root` / ignored | config + `.agentcontrol-ignore` |
| Port 30228 in use | Docker + legacy systemd | `docker compose ps`; `systemctl stop agentcontrol` |
| Fleet stream disconnects / stale data | Old SSE reconnect loop | Use snapshot polling (v1.2+); tab pause reduces load |
| Buttons dead after refresh | No event delegation | Bind on `#list` / `#servers` parent |
| Worker ID changed | `WORKER_NAMESPACE` modified | Never change UUID constant |

---

## Agent workflow

On **every** code change that ships to users:

1. Bump `version.json` and/or `fleet/version.json` (+ copy to `fleet/public/version.json`).
2. Update **this file** (`AGENTS.md`) if behavior, APIs, or deploy steps changed.
3. Panel: `docker compose up -d --build`. Fleet: `npm run deploy`.

---

## Scope guidance for agents

**Do:**

- Keep diffs focused; match vanilla JS / Flask patterns.
- Update README and this file when install paths or APIs change.
- Bump `version.json` / `fleet/version.json` on every user-visible change.
- After app changes on live server: `docker compose up -d --build`.
- Test Fleet with hostname URLs only.

**Avoid:**

- Heavy frontend frameworks or databases unless requested.
- Cloudflare Tunnel as requirement (design is direct HTTP per server).
- Storing fleet passwords in client code or logs.
- Shipping UI/API changes without bumping `version.json`.

---

## User communication

Project owner often communicates in **Persian (Farsi)**. Summaries and install instructions may be in Persian; code, comments, and this file stay in English.

## Related docs

- [README.md](README.md) — install, config, service commands
- [fleet/README.md](fleet/README.md) — Wrangler, KV, FLEET_PASSWORD, custom domain
