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

## Quick install (idempotent)

```bash
sudo bash -c 'git clone https://github.com/EmRa228/agentcontrol.git /opt/agentcontrol && CURSOR_API_KEY=YOUR_CURSOR_KEY /opt/agentcontrol/bootstrap.sh'
```

Update later:

```bash
sudo CURSOR_API_KEY=YOUR_KEY PANEL_PASSWORD=your-pass /opt/agentcontrol/bootstrap.sh
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

```bash
systemctl status agentcontrol
systemctl restart agentcontrol
journalctl -u agentcontrol -f
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

## License

MIT — see [LICENSE](LICENSE)

---

## فارسی

```bash
sudo bash -c 'git clone https://github.com/EmRa228/agentcontrol.git /opt/agentcontrol && CURSOR_API_KEY=کلید_شما /opt/agentcontrol/bootstrap.sh'
```

پسورد: `sudo cat /etc/agentcontrol/auth-password`
