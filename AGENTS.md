# AGENTS.md — AgentControl

Guide for AI coding agents working in this repository.

## What this project is

**AgentControl** is a lightweight Linux web panel that starts and stops **Cursor Cloud Agent workers** (`agent worker`) per project folder. A optional **Fleet** dashboard (Cloudflare Workers) aggregates many servers into one UI.

| Component | Stack | Runs on |
|-----------|-------|---------|
| Single-server panel | Python 3.10+ Flask, vanilla HTML/JS | Each Linux VPS (`systemd`) |
| Fleet dashboard | Cloudflare Workers (TypeScript), KV, static `public/` | Cloudflare edge |

**Repo:** https://github.com/EmRa228/agentcontrol  
**Default panel port:** `30228`  
**Default project scan root:** `/root`

---

## Repository layout

```
agentcontrol/
├── app.py                 # Flask backend (API + worker lifecycle)
├── templates/index.html   # Single-server UI (metrics, Docker, projects)
├── bootstrap.sh           # Idempotent entry: clone/update → install.sh
├── install.sh             # Packages, venv, systemd, API key, agent CLI
├── agentcontrol.service   # systemd unit
├── config.example.yaml    # Copied to /etc/agentcontrol/config.yaml
├── requirements.txt       # flask, pyyaml
├── README.md              # User-facing install docs (EN + FA snippet)
└── fleet/
    ├── src/index.ts       # Worker: proxy API, SSE stream, KV server list
    ├── public/index.html  # Fleet UI
    ├── wrangler.jsonc     # Worker + KV + assets config
    └── README.md          # Fleet deploy docs
```

**Runtime paths on a server (not in git):**

| Path | Purpose |
|------|---------|
| `/opt/agentcontrol` | App install dir |
| `/etc/agentcontrol/config.yaml` | Config |
| `/etc/agentcontrol/api-key` | Cursor personal API key |
| `/etc/agentcontrol/auth-password` | Panel password |
| `/var/lib/agentcontrol/` | PID/state, worker logs (`<folder>.log`) |

---

## Architecture

### Single server

```
Browser → :30228 Flask
            ├─ GET /              → templates/index.html
            ├─ GET /api/system    → CPU/RAM/disk/swap/network/Docker
            ├─ GET /api/folders   → projects under scan_root
            └─ POST /api/start|stop/<name> → subprocess: agent worker
```

- **Worker ID:** deterministic `uuid.uuid5(WORKER_NAMESPACE, f"agentcontrol:{folder}")` — stable per folder.
- **Worker process name in Cursor:** `agentcontrol-<folder>`.
- **Redirect URL:** `https://cursor.com/agents#workerId={id}` (optional `&model=` from `default_model` config).
- **Idle release:** workers stopped after `idle_release_seconds` (default 12h).
- **Hide project:** creates `.agentcontrol-ignore` in the folder; skipped by `list_folders()`.
- **Metrics history:** in-memory `deque` (max 1800 points, ~2s refresh → ~1h). Fields: `cpu`, `load_pct`, `ram`, `disk`, `swap`, `net`, `docker_running`.

### Fleet

```
Browser → Worker (hub.aksbaz.com or *.workers.dev)
            ├─ KV: server list { id, name, url, password }
            ├─ GET /api/fleet/stream  → SSE snapshot every 4s
            └─ proxy → each server /api/* with X-AgentControl-Auth
```

**Critical:** Cloudflare Workers **cannot fetch raw IP addresses** (error 1003). Fleet server URLs must use a **hostname** with a grey-cloud DNS A record, e.g. `http://ac-tg-uk.aksbaz.com:30228`.

---

## Authentication

| Layer | Header / storage | Notes |
|-------|------------------|-------|
| Panel | `X-AgentControl-Auth: <password>` | File: `/etc/agentcontrol/auth-password`; browser `localStorage` key `agentcontrol_auth_<host>` |
| Fleet | `X-Fleet-Password: <secret>` | Wrangler secret `FLEET_PASSWORD`; browser `sessionStorage` |
| Fleet → server | `X-AgentControl-Auth` | Per-server password stored in KV (never returned to browser) |

First visit may use `/api/setup/password` and `/api/setup/api-key` when files are missing.

---

## API reference (single server)

All protected routes require `X-AgentControl-Auth` unless noted.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Panel HTML |
| GET | `/health` | `{ ok: true }` |
| POST | `/api/auth/login` | Validate password |
| GET | `/api/auth/status` | Auth + setup flags |
| GET | `/api/setup/status` | Needs password / API key |
| POST | `/api/setup/password` | First-time password |
| POST | `/api/setup/api-key` | First-time API key |
| GET | `/api/system` | Full system snapshot |
| GET | `/api/system/history` | `{ points: [...] }` |
| GET | `/api/folders` | Project list (sorted by mtime desc) |
| GET | `/api/models` | Cursor models from `agent --list-models` |
| GET | `/api/settings` | `default_model`, etc. |
| POST | `/api/settings/model` | Save default model |
| POST | `/api/start/<name>` | Start worker |
| POST | `/api/stop/<name>` | Stop worker |
| GET | `/api/ready/<name>` | Worker running + cloud ready |
| POST | `/api/hide/<name>` | Create `.agentcontrol-ignore` |
| GET | `/api/status` | Legacy/summary status |

## API reference (Fleet Worker)

| Method | Path | Auth |
|--------|------|------|
| POST | `/api/login` | body `{ password }` |
| GET | `/api/servers` | Fleet password |
| POST | `/api/servers` | Add server (probes `/api/system`) |
| DELETE | `/api/servers/<id>` | Remove server |
| GET | `/api/fleet/stream` | SSE live snapshots |
| GET | `/api/fleet/snapshot` | One-shot snapshot |
| POST | `/api/fleet/<serverId>/start\|stop/<project>` | Proxy |
| GET | `/api/fleet/<serverId>/ready/<project>` | Proxy |

---

## Configuration (`config.yaml`)

| Key | Default | Meaning |
|-----|---------|---------|
| `port` | `30228` | Flask listen port |
| `scan_root` | `/root` | Where to find project folders |
| `idle_release_seconds` | `43200` | Auto-stop idle workers |
| `api_key_file` | `/etc/agentcontrol/api-key` | Cursor API key |
| `auth_password_file` | `/etc/agentcontrol/auth-password` | Panel password |
| `state_dir` | `/var/lib/agentcontrol` | PIDs, logs, state JSON |
| `default_model` | `""` | Appended to Cursor redirect URL |
| `agent_bin` | `""` | Path to `agent` CLI (auto-detected on install) |
| `exclude_prefixes` | `["."]` | Skip dir names starting with these |
| `exclude_dirs` | `[...]` | Exact dir names to skip |

---

## Install & deploy

### New server (production)

```bash
sudo bash -c 'git clone https://github.com/EmRa228/agentcontrol.git /opt/agentcontrol && CURSOR_API_KEY=YOUR_KEY /opt/agentcontrol/bootstrap.sh'
```

Update:

```bash
sudo CURSOR_API_KEY=YOUR_KEY PANEL_PASSWORD=your-pass /opt/agentcontrol/bootstrap.sh
# or
cd /opt/agentcontrol && sudo git pull && sudo systemctl restart agentcontrol
```

Open firewall: `30228/tcp`.

### Fleet

```bash
cd fleet
npm install
npx wrangler login
npx wrangler kv namespace create KV   # paste id into wrangler.jsonc
npx wrangler secret put FLEET_PASSWORD
npm run deploy
```

### Git push (maintainer)

```bash
GIT_SSH_COMMAND="ssh -i ~/.ssh/agentstart_deploy" git push origin github-main:main
```

Local branch may be `github-main`; remote default is `main`.

---

## UI conventions

### Single-server (`templates/index.html`)

- Dark theme CSS variables: `--bg`, `--card`, `--accent`, etc.
- Auto-refresh every **2s** (`refreshAll`); pauses when modals open (`modalOpen`, `busy`).
- Event delegation on `#list` for Start/Stop (re-render safe).
- Sparklines + expanded charts: CPU (with load overlay), RAM, Disk, Network; swap shown inline on RAM card and in detail grid.
- Docker: compose groups with CPU/RAM share bars (`docker.groups` from backend).
- Long-press / right-click project → hide via `.agentcontrol-ignore`.

### Fleet (`fleet/public/index.html`)

- Live updates via **SSE** (`/api/fleet/stream`), not polling.
- Server cards: 4 metrics (CPU, RAM with swap %, Disk, Net); **2×2 grid on mobile**, 4 columns on desktop.
- **Recent projects:** top 3 folders by mtime as clickable chips on card header (Start/Stop, `stopPropagation`).
- **Server sort:** `localStorage` key `agentcontrol_fleet_recent` — recently interacted servers float to top.
- Expanded view: full charts, detail grid, Docker groups, all projects.
- `expanded` / `expandedDocker` Sets preserved across SSE re-renders.

When changing Fleet UI, avoid `minmax(130px)` or wide fixed grids that cause horizontal scroll on mobile.

---

## Backend conventions

- **Python style:** stdlib-first; minimal dependencies; no ORM.
- **Config:** load from `CONFIG_SEARCH` chain; `save_config_patch()` for runtime updates.
- **Docker stats:** parsed from `docker stats` + `docker inspect` labels; grouped by compose project.
- **Do not change** `WORKER_NAMESPACE` — breaks stable worker IDs for existing folders.
- **Subprocess:** workers run as root under systemd; `PATH` includes `/root/.local/bin` for `agent`.

### Adding API fields

1. Add to `collect_system_info()` / `record_metrics()` in `app.py`.
2. Update `templates/index.html` `renderSystem()` if panel should show it.
3. If Fleet needs it: `snapshotOneServer()` in `fleet/src/index.ts` already forwards full `system` + `history`; update Fleet UI only unless summary shortcuts are needed.

---

## Local development

### Panel (without full install)

```bash
cd /path/to/agentcontrol
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml   # edit scan_root, port
python app.py
```

Requires `agent` CLI and valid API key for start/stop.

### Fleet

```bash
cd fleet
npm run dev   # usually http://localhost:8787
```

Set `FLEET_PASSWORD` via `wrangler secret put` or `.dev.vars` for local dev.

---

## Testing checklist

After changes:

1. **Panel:** `curl -H "X-AgentControl-Auth: pass" http://127.0.0.1:30228/api/system`
2. **Panel UI:** login, metrics refresh, start/stop, hide project, Docker expand.
3. **Fleet:** login, add server with **hostname** URL, SSE live indicator, quick agents, expand charts.
4. **Mobile:** no horizontal scroll on metric cards.
5. **Restart:** `systemctl restart agentcontrol` — workers reconcile via `reconcile_state()`.

---

## Common pitfalls

| Issue | Cause | Fix |
|-------|-------|-----|
| Cloudflare error 1003 | Fleet URL uses raw IP | Use hostname + grey-cloud A record |
| Port unreachable from Fleet | Firewall / orange cloud proxy | Open `30228`; DNS only (grey cloud) |
| Start fails | No API key | `/etc/agentcontrol/api-key` or setup UI |
| Empty project list | Wrong `scan_root` or all ignored | Check config + `.agentcontrol-ignore` |
| Buttons dead after refresh | Missing event delegation | Bind on parent `#list` / `#servers`, not per-row |
| Sparklines empty | History not recording field | Add field in `record_metrics()` |
| Worker ID changed | `WORKER_NAMESPACE` modified | Never change UUID constant |

---

## Scope guidance for agents

**Do:**

- Keep diffs focused; match existing vanilla JS / Flask patterns.
- Update both README and this file when install paths or APIs change.
- Test Fleet with hostname URLs only.
- Commit with clear messages; push to `main` when asked to deploy.

**Avoid:**

- Adding heavy frontend frameworks or databases unless explicitly requested.
- Cloudflare Tunnel as a requirement (design is direct HTTP to each server).
- Storing fleet server passwords in client-side code or logs.
- Creating PRs unless the user asks (some sessions use direct push only).

---

## User communication

The project owner often communicates in **Persian (Farsi)**. Summaries and install instructions can be provided in Persian; code, comments, and this file remain in English.

## Related docs

- [README.md](README.md) — install, config, service commands
- [fleet/README.md](fleet/README.md) — Wrangler, KV, FLEET_PASSWORD, custom domain
