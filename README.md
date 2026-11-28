# AgentControl

Lightweight web panel to **start/stop Cursor Cloud Agent workers** on any Linux
server, open a shared **OpenCode** workspace, and manage many servers from one
Cloudflare-hosted **Fleet** dashboard.

![Python](https://img.shields.io/badge/python-3.10+-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## What it does

- Lists project folders under a configurable root (default `/root`)
- **Start / Stop / Restart** a worker (`agentcontrol-<folder>`) and open Cursor
- Project search, filters, sorting, git details, live worker logs (tail/download)
- Shared **OpenCode serve** with a patched web UI at `/oc/` after sign-in
- Server stats, charts (live/1h/6h/24h/7d), per-project readiness
- Optional **proxy pool** (xray) and **Cursor edge bridge** for restricted networks
- **Password (Argon2) + optional TOTP**, server-side sessions, scoped API tokens
- **Alerts** (Telegram, webhook, email, ntfy) with per-channel switches and severity
- **HTTPS by default** — bundled Caddy behind a same-port front; HTTP redirects
- **Public status page** (`/status`), Prometheus metrics (`/metrics`), audit log
- **Admin terminal** in the browser (can be disabled in config)
- **Fleet** — optional multi-server dashboard on Cloudflare Workers + KV, with
  health badges, drift detection, down/recovery alerts and canary panel updates

## Requirements

- Linux + systemd (host install; Docker is deprecated)
- Python 3.10+
- [Cursor agent CLI](https://cursor.com/docs/cloud-agent/self-hosted-guides/my-machines)
- A Cursor personal API key from [cursor.com/settings](https://cursor.com/settings)

## Quick install

```bash
sudo bash -c 'git clone https://github.com/EmRa228/agentcontrol.git /opt/agentcontrol && /opt/agentcontrol/bootstrap.sh'
```

Non-interactive:

```bash
sudo CURSOR_API_KEY=YOUR_KEY PANEL_PASSWORD=your-password /opt/agentcontrol/install.sh
```

Interactive wizard (network mode, scan root, optional key):

```bash
sudo /opt/agentcontrol/install-wizard.sh
# proxy/xray variant:
sudo AGENTCONTROL_NETWORK_MODE=2 AGENTCONTROL_PROXY_PORT=30229 /opt/agentcontrol/install-wizard.sh
```

Update later:

```bash
sudo /opt/agentcontrol/bootstrap.sh
# or from the panel: Update panel
```

Open `https://SERVER:30228` (or `https://your-host:30228` when `public_hostname` is
set). First visit asks for the panel password and Cursor API key.

**Fresh reinstall** (removes legacy `agentstart`):

```bash
sudo CURSOR_API_KEY=YOUR_KEY bash -c '
systemctl stop agentstart agentcontrol 2>/dev/null || true
rm -rf /opt/agentstart /opt/agentcontrol
rm -f /etc/systemd/system/agentstart.service
git clone https://github.com/EmRa228/agentcontrol.git /opt/agentcontrol
/opt/agentcontrol/bootstrap.sh
'
```

## Password and API key

The panel password is stored as an **Argon2 hash** in
`/etc/agentcontrol/auth-password`, so `sudo cat` shows a hash, not the password.

```bash
# Set or reset (prompts twice; requires >= 8 chars)
sudo /opt/agentcontrol/venv/bin/python /opt/agentcontrol/scripts/set-password.py

# API key
echo "YOUR_CURSOR_KEY" | sudo tee /etc/agentcontrol/api-key
sudo chmod 600 /etc/agentcontrol/api-key
sudo systemctl restart agentcontrol
```

The browser stores no password; a server-side session (30 days by default) is
created after password + optional TOTP. Settings → **Sessions** lists devices and
can revoke one or all. Settings → **API tokens** creates scoped tokens (`read`,
`ops`, `admin`) for Fleet/automation instead of sharing the panel password.

On a fresh install, non-loopback setup requires the token printed by the installer
(`/etc/agentcontrol/setup-token`), e.g. `https://HOST:30228/?setup=TOKEN`.

## Configuration

`/etc/agentcontrol/config.yaml` (the repo `config.yaml` is a symlink):

```yaml
port: 30228               # public HTTPS port
internal_port: 30328      # Flask, loopback only (set by enable-caddy.sh)
https_internal_port: 30443
bind_host: 127.0.0.1
public_hostname: panel.example.com   # optional; empty = On-Demand TLS
scan_root: /root
idle_release_seconds: 43200
session_days: 30
```

All keys, env overrides, secrets and state files: **[docs/CONFIG.md](docs/CONFIG.md)**.

## TLS layout

```text
30228  agentcontrol-tls-front
  TLS       -> 127.0.0.1:30443 Caddy -> 127.0.0.1:30328 Flask
  HTTP      -> 308 https://HOST:30228   (bare IP -> Flask, no ACME)
```

Caddy never binds 80/443 for the site; port 80 is used only for ACME HTTP-01. With
`public_hostname` the certificate is managed for that name and **renews
automatically** about 30 days before expiry; keep port 80 free for that challenge.
Plain `http://HOST:30228` is redirected to HTTPS with 308. Bare-IP HTTP access stays
plain because ACME cannot issue certificates for IPs. If port 80 belongs to another
proxy, set `tls_cert_file` / `tls_key_file` instead. Details:
[docs/OPERATIONS.md](docs/OPERATIONS.md).

## OpenCode

One shared `opencode serve` on loopback `4096`, opened per project from the panel
(button → `/oc/?directory=…`). The binary and patched frontend are pinned
(OpenCode 2.4.1 + Bun 1.4.4) and installed on demand; check
`/var/lib/agentcontrol/opencode-install.log` if it fails. Provider keys are managed
in Settings → OpenCode; ChatGPT Plus/Pro uses OpenCode's headless OAuth flow.

## Fleet (multi-server)

One dashboard for all servers, no central VPS. Deploy steps:
[fleet/README.md](fleet/README.md). Polling/KV limits:
[fleet/CLOUDFLARE.md](fleet/CLOUDFLARE.md).

## API and docs

| Doc | Contents |
|---|---|
| [docs/API.md](docs/API.md) | panel + Fleet endpoints, auth |
| [docs/CONFIG.md](docs/CONFIG.md) | config keys, env, secrets, state |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | install/update, TLS, rollback, failures |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | processes, auth, worker/OpenCode/Fleet flows |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | local dev, tests, release checklist |
| [AGENTS.md](AGENTS.md) | guide for coding agents |

## License

MIT — see [LICENSE](LICENSE)

---

## فارسی

```bash
sudo bash -c 'git clone https://github.com/EmRa228/agentcontrol.git /opt/agentcontrol && /opt/agentcontrol/bootstrap.sh'
```

ویزارد: مستقیم یا پروکسی xray (پورت پیش‌فرض `30229`)، مسیر پروژه‌ها (پیش‌فرض `/root`). کلید API اختیاری است و از پنل هم قابل تنظیم است.

پسورد پنل به‌صورت هش ذخیره می‌شود؛ برای تغییر/بازنشانی:

```bash
sudo /opt/agentcontrol/venv/bin/python /opt/agentcontrol/scripts/set-password.py
```

آدرس پنل: `https://IP-SERVER:30228` (یا نام دامنه در صورت تنظیم `public_hostname`). برای گواهی معتبر، DNS دامنه را به سرور بدهید و پورت ۸۰ را باز نگه دارید.
