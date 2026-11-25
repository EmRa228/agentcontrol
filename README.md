# AgentControl

Lightweight web panel to **start/stop Cursor Cloud Agent workers** on any Linux server.  
Mobile-friendly UI with per-server password protection.

![Python](https://img.shields.io/badge/python-3.10+-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## What it does

- Lists project folders under a configurable root (default `/root`)
- **Start** → launches `agent worker` as `agentcontrol-<folder>` and opens `cursor.com/agents#workerId=...`
- **Stop** → terminates the worker process
- Shows server stats (CPU, RAM, disk, uptime, Docker, active workers)
- Stable worker ID per folder (deterministic UUID)
- Auto idle-release after **12 hours**
- Always-on via **systemd** (`agentcontrol.service`)
- **Per-server password** — stored in browser `localStorage`

## Requirements

- Linux + systemd
- Python 3.10+
- [Cursor agent CLI](https://cursor.com/docs/cloud-agent/self-hosted-guides/my-machines)
- Personal API key from [Cursor Dashboard](https://cursor.com/settings)

## Quick install (host systemd — recommended)

**Default:** panel runs on the host via systemd so Cursor workers inherit `/var/run/docker.sock` (required for Docker Compose repos like nictry).

```bash
sudo bash -c 'git clone https://github.com/EmRa228/agentcontrol.git /opt/agentcontrol && /opt/agentcontrol/bootstrap.sh'
```

Non-interactive with API key:

```bash
sudo CURSOR_API_KEY=YOUR_KEY /opt/agentcontrol/install.sh
```

Interactive wizard (network mode, scan root, optional API key — **default: direct, no proxy**):

```bash
sudo /opt/agentcontrol/install-wizard.sh
# or: sudo WIZARD=1 /opt/agentcontrol/bootstrap.sh
```

Non-interactive proxy mode (xray):

```bash
sudo AGENTCONTROL_NETWORK_MODE=2 AGENTCONTROL_PROXY_PORT=30229 SCAN_ROOT=/root /opt/agentcontrol/install-wizard.sh
```

Update later:

```bash
sudo /opt/agentcontrol/bootstrap.sh
# or: cd /opt/agentcontrol && git pull && sudo bash install.sh
```

Guard file after host install: `/etc/agentcontrol/HOST_ONLY` — blocks Docker panel (`docker compose`).

Fresh reinstall (removes legacy `agentstart`):

```bash
sudo CURSOR_API_KEY=YOUR_KEY bash -c '
systemctl stop agentstart agentcontrol 2>/dev/null || true
rm -rf /opt/agentstart /opt/agentcontrol
rm -f /etc/systemd/system/agentstart.service
git clone https://github.com/EmRa228/agentcontrol.git /opt/agentcontrol
/opt/agentcontrol/bootstrap.sh
'
```

Open from phone: `http://SERVER_IP:30228`

## Panel password

```bash
sudo cat /etc/agentcontrol/auth-password
```

Set manually:

```bash
echo "my-password" | sudo tee /etc/agentcontrol/auth-password
sudo chmod 600 /etc/agentcontrol/auth-password
```

Saved in browser **localStorage** (one login per server per browser).

## Cursor API key

```bash
echo "YOUR_CURSOR_PERSONAL_API_KEY" | sudo tee /etc/agentcontrol/api-key
sudo chmod 600 /etc/agentcontrol/api-key
sudo systemctl restart agentcontrol
```

## Configuration

File: `/etc/agentcontrol/config.yaml`

| Key | Default |
|-----|---------|
| `port` | `30228` |
| `scan_root` | `/root` |
| `idle_release_seconds` | `43200` |
| `api_key_file` | `/etc/agentcontrol/api-key` |
| `auth_password_file` | `/etc/agentcontrol/auth-password` |
| `state_dir` | `/var/lib/agentcontrol` |

## Service

Host systemd (default):

```bash
systemctl status agentcontrol
systemctl restart agentcontrol
journalctl -u agentcontrol -f
```

Worker logs: `/var/lib/agentcontrol/<folder>.log`  
Worker names in Cursor: `agentcontrol-<folder>`

## OpenCode (shared serve)

- AgentControl runs one shared OpenCode server on `127.0.0.1:4096`. Keep port 4096 on loopback.
- After panel sign-in, select **OpenCode** on a project. The URL at `/oc/?directory=…` opens a draft for that directory. The frontend and API share the panel origin.
- Reload or bookmark an OpenCode session URL to return to that session while the panel login cookie is valid.
- The proxy requires a panel session, adds upstream Basic auth, forwards API methods and SSE, and bridges the OpenCode terminal WebSocket. If the panel sits behind another proxy, preserve its host and scheme and allow long-lived streaming and WebSocket upgrades for `/oc/`.
- Panel Settings → **OpenCode** manages API keys. To use a ChatGPT Plus or Pro subscription, open OpenCode's provider connection dialog, select **OpenAI**, and choose **ChatGPT Pro/Plus (headless)** for a remote server. Complete the device authorization on the official OpenAI page. Use the same OpenCode service user and data directory after restarts so saved credentials are reused.
- Fleet project launch returns the server panel's OpenCode URL. Sign in to that panel in the browser once.

The frontend build is pinned to OpenCode 1.19.6 and uses Bun 1.4.4. With that OpenCode release on the server, run `scripts/build-opencode-web.sh` from the AgentControl checkout. Set `BUN_BIN` if Bun is installed outside PATH. The script installs the built app at `opencode-web`. The patch makes the assets, router and API clients prefix-aware. Update the pinned source, patch, and matching backend together when upgrading OpenCode.

To verify a deployment, test the authenticated OpenCode service directly on loopback first, then sign in through the panel and open two projects. Check that the selected project survives browser refresh, JS and CSS use `/oc/assets/` with the proper MIME types, `/oc/global/event` delivers an event before closing, and the terminal can send and receive over its WebSocket. Verify ChatGPT OAuth by making an OpenAI model request, restarting just the OpenCode child process, and repeating the request without signing in again.

Back up `/etc/agentcontrol`, the OpenCode configuration, and its private data directory before changing service settings. Use a consistent SQLite backup if OpenCode is running. The default systemd unit can stop all child processes during a panel restart, including Cursor workers. Check worker state before reloading the panel.

## API

| Method | Path | Auth |
|--------|------|------|
| GET | `/` | no |
| POST | `/api/auth/login` | no |
| GET | `/api/auth/status` | no |
| GET | `/api/system` | yes |
| GET | `/api/folders` | yes |
| POST | `/api/start/<name>` | yes |
| POST | `/api/stop/<name>` | yes |
| POST | `/api/opencode/open/<name>` | yes |
| GET | `/api/opencode/status` | yes |
| GET/POST | `/api/settings/opencode` | yes |
| GET/POST | `/api/settings/totp/*` | yes |
| GET/POST/PUT/PATCH/DELETE | `/oc/…` | panel session cookie |
| GET | `/health` | no |

Header: `X-AgentControl-Auth: <password>`

## Multi-server fleet (Cloudflare Workers)

Manage **all servers from one URL** — no central VPS, no Tunnel.  
See **[fleet/README.md](fleet/README.md)** for deploy steps (`wrangler login` → KV → `npm run deploy` → first-visit password setup).  
Contributors changing Fleet polling or caching must read **[fleet/CLOUDFLARE.md](fleet/CLOUDFLARE.md)** (Workers/KV limits).

## License

MIT — see [LICENSE](LICENSE)

---

## فارسی

```bash
sudo bash -c 'git clone https://github.com/EmRa228/agentcontrol.git /opt/agentcontrol && /opt/agentcontrol/bootstrap.sh'
```

ویزارد: مستقیم یا پروکسی xray (پورت پیش‌فرض `30229`)، مسیر پروژه‌ها (پیش‌فرض `/root`). API key اختیاری — در داشبورد هم قابل تنظیم است.

پسورد: `sudo cat /etc/agentcontrol/auth-password`
