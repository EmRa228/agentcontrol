# AgentControl

Lightweight web panel to **start/stop Cursor Cloud Agent workers** on any Linux server.  
Mobile-friendly UI with per-server password protection.

![Python](https://img.shields.io/badge/python-3.10+-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## What it does

- Lists project folders under a configurable root (default `/root`)
- **Start** → launches `agent worker` and opens `cursor.com/agents#workerId=...`
- **Stop** → terminates the worker process
- Shows server stats (CPU, RAM, disk, uptime, Docker, active workers)
- Stable worker ID per folder (deterministic UUID)
- Auto idle-release after **12 hours**
- Always-on via **systemd**
- **Per-server password** — stored in browser `localStorage` (one login per server per browser)

## Requirements

- Linux + systemd
- Python 3.10+
- [Cursor agent CLI](https://cursor.com/docs/cloud-agent/self-hosted-guides/my-machines)
- Personal API key from [Cursor Dashboard](https://cursor.com/settings)

## Quick install (idempotent)

```bash
sudo bash -c 'git clone https://github.com/EmRa228/agentcontrol.git /opt/agentstart && CURSOR_API_KEY=YOUR_CURSOR_KEY /opt/agentstart/bootstrap.sh'
```

Update or change API key / password later:

```bash
sudo CURSOR_API_KEY=YOUR_KEY PANEL_PASSWORD=your-panel-pass /opt/agentstart/bootstrap.sh
```

`bootstrap.sh` is safe to run multiple times:
- `git pull` if repo exists (no clone error)
- updates API key when `CURSOR_API_KEY` is set
- updates panel password when `PANEL_PASSWORD` is set
- restarts the systemd service

Open from phone: `http://SERVER_IP:30228`

## Panel password

On first install a **unique simple password** is generated per server, e.g. `cmtg-uk-a3f9k`.

```bash
sudo cat /etc/agentstart/auth-password
```

Set manually:

```bash
echo "my-password" | sudo tee /etc/agentstart/auth-password
sudo chmod 600 /etc/agentstart/auth-password
```

The browser saves the password in **localStorage** (keyed by host:port) so you only enter it once per server per browser.

## Cursor API key

```bash
echo "YOUR_CURSOR_PERSONAL_API_KEY" | sudo tee /etc/agentstart/api-key
sudo chmod 600 /etc/agentstart/api-key
sudo systemctl restart agentstart
```

Without this key, workers cannot connect to Cursor.

## Configuration

File: `/etc/agentstart/config.yaml`

| Key | Default | Description |
|-----|---------|-------------|
| `port` | `30228` | HTTP port |
| `scan_root` | `/root` | Folder scan path |
| `idle_release_seconds` | `43200` | 12h worker idle timeout |
| `api_key_file` | `/etc/agentstart/api-key` | Cursor API key |
| `auth_password_file` | `/etc/agentstart/auth-password` | Panel password |
| `exclude_prefixes` | `["."]` | Skip hidden dirs |
| `exclude_dirs` | `[]` | Skip by name |

## Service

```bash
systemctl status agentstart
systemctl restart agentstart
journalctl -u agentstart -f
```

Worker logs: `/var/lib/agentstart/<folder>.log`

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
| GET | `/api/ready/<name>` | yes |
| GET | `/health` | no |

Authenticated requests send header: `X-AgentControl-Auth: <password>`

## Security notes (public repo)

- This repo contains **no secrets**
- Never commit `/etc/agentstart/api-key` or `auth-password`
- Panel password is basic protection — use VPN/firewall for production
- Default port `30228` — restrict access if possible

## License

MIT — see [LICENSE](LICENSE)

---

## فارسی

پنل سبک برای مدیریت Cursor Agent روی سرور لینوکس.

```bash
sudo bash -c 'git clone https://github.com/EmRa228/agentcontrol.git /opt/agentstart && CURSOR_API_KEY=کلید_شما /opt/agentstart/bootstrap.sh'
```

پسورد پنل: `sudo cat /etc/agentstart/auth-password`  
در مرورگر یک‌بار وارد می‌کنی و در localStorage می‌ماند.
