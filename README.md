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

Update later:

```bash
sudo /opt/agentcontrol/bootstrap.sh
# or: cd /opt/agentcontrol && git pull && sudo bash install.sh
```

Guard file after host install: `/etc/agentcontrol/HOST_ONLY` — blocks `install-wizard.sh` / Docker panel.

### Docker wizard (not recommended for Compose repos)

Workers started from a Docker panel **cannot** use host `docker.sock`. Only use if every repo is host-native:

```bash
sudo DOCKER_INSTALL=1 /opt/agentcontrol/bootstrap.sh
```

Interactive proxy wizard (direct vs xray, default proxy port `30229`):

```bash
sudo DOCKER_INSTALL=1 FORCE_DOCKER_INSTALL=1 /opt/agentcontrol/install-wizard.sh
```

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

Docker (only if you explicitly used `DOCKER_INSTALL=1`):

```bash
cd /opt/agentcontrol
docker compose ps
docker compose logs -f
docker compose restart
```

Worker logs: `/var/lib/agentcontrol/<folder>.log`  
Worker names in Cursor: `agentcontrol-<folder>`

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
| GET | `/health` | no |

Header: `X-AgentControl-Auth: <password>`

## Multi-server fleet (Cloudflare Workers)

Manage **all servers from one URL** — no central VPS, no Tunnel.  
See **[fleet/README.md](fleet/README.md)** for deploy steps (`wrangler login` → KV → `FLEET_PASSWORD` → `npm run deploy`).

## License

MIT — see [LICENSE](LICENSE)

---

## فارسی

```bash
sudo bash -c 'git clone https://github.com/EmRa228/agentcontrol.git /opt/agentcontrol && /opt/agentcontrol/bootstrap.sh'
```

ویزارد: مستقیم یا پروکسی xray (پورت پیش‌فرض `30229`)، مسیر پروژه‌ها (پیش‌فرض `/root`). API key اختیاری — در داشبورد هم قابل تنظیم است.

پسورد: `sudo cat /etc/agentcontrol/auth-password`
