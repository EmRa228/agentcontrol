#!/usr/bin/env python3
"""agentcontrol — lightweight panel to start/stop Cursor agent workers."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import platform
import re
import secrets
import signal
import shutil
import socket
import subprocess
import sys
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import urlopen

import yaml
from flask import Flask, g, jsonify, redirect, render_template, request, Response, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import safe_join

from xray_client import (
    apply_client_settings,
    apply_proxy_env,
    build_status_report,
    import_from_xray_config,
    load_client_settings,
    merge_share_url,
    merge_vless_url,
    proxy_listening,
    public_settings,
    set_proxy_enabled,
    test_cursor_api,
    test_proxy,
)
from cursor_bridge import (
    apply_cursor_bridge_env,
    load_bridge_settings,
    public_bridge_settings,
    save_bridge_from_panel,
    test_cursor_bridge,
)
from proxy_pool import (
    add_proxy_link,
    add_subscription,
    apply_pool_config,
    ensure_proxy_ready,
    migrate_legacy_to_pool,
    pool_status,
    refresh_all_subscriptions,
    select_and_apply_best,
    set_pool_enabled,
    test_all_proxies,
)
from worker_git import ensure_worker_git, folder_git_status, git_details
import opencode_panel
import panel_sessions
import panel_totp
from panel import audit as audit_log
from panel import health as health_mod
from panel import metrics as metrics_mod
from panel import security as security_mod
from panel import terminal as terminal_mod
from panel.config import build_settings, load_notifications, save_notifications, validate_settings
from panel.db import Store
from panel.monitor import Monitor
from panel.notify import Notifier

IGNORE_MARKER = ".agentcontrol-ignore"
WORKER_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
CURSOR_AGENTS_URL = "https://cursor.com/agents#workerId={worker_id}"
MGMT_PORT_BASE = 32000
MGMT_PORT_RANGE = 800
VERSION_FILE = Path(__file__).resolve().parent / "version.json"
GITHUB_RAW_BASE = os.environ.get(
    "AGENTCONTROL_GITHUB_RAW",
    "https://raw.githubusercontent.com/EmRa228/agentcontrol/main",
)
INSTALL_DIR = Path(os.environ.get("INSTALL_DIR", "/opt/agentcontrol"))
HOST_ONLY_FILE = Path("/etc/agentcontrol/HOST_ONLY")
DOCKER_SOCK = Path("/var/run/docker.sock")


def running_inside_docker() -> bool:
    if Path("/.dockerenv").is_file():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return "docker" in cgroup or "containerd" in cgroup


def docker_sock_available() -> bool:
    return DOCKER_SOCK.is_socket()


def project_needs_docker(path: Path) -> bool:
    return any((path / name).is_file() for name in ("docker-compose.yml", "compose.yml", "compose.yaml"))


def worker_runtime_error(name: str | None = None) -> str | None:
    if running_inside_docker():
        return (
            "AgentControl panel is running inside Docker — Cursor agents would not have "
            "host /var/run/docker.sock. Run install.sh on the host (systemd), not Docker."
        )
    if not docker_sock_available():
        return (
            "Host /var/run/docker.sock is missing — agents cannot run docker compose. "
            "Install the panel on the host with install.sh (see /etc/agentcontrol/HOST_ONLY)."
        )
    if name:
        path = folder_path(name)
        if path and project_needs_docker(path) and not docker_sock_available():
            return (
                f"{name} uses Docker Compose but /var/run/docker.sock is missing. "
                "Install the panel on the host with install.sh."
            )
    return None


def ensure_host_panel() -> None:
    if running_inside_docker():
        raise SystemExit(
            "AgentControl must run on the host (systemd), not inside Docker. "
            "Run: bash /opt/agentcontrol/install.sh"
        )


def host_runtime_status() -> dict:
    return {
        "inside_docker": running_inside_docker(),
        "docker_sock": docker_sock_available(),
        "host_only_guard": HOST_ONLY_FILE.is_file(),
        "worker_start_allowed": worker_runtime_error() is None,
    }


def load_version() -> dict:
    try:
        return json.loads(VERSION_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {"component": "panel", "version": "0.0.0", "updated": ""}


def version_tuple(value: str) -> tuple[int, int, int]:
    parts: list[int] = []
    for part in (value or "0").split("."):
        try:
            parts.append(int(part))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def version_is_newer(remote: str, local: str) -> bool:
    return version_tuple(remote) > version_tuple(local)


def fetch_github_json(relative_path: str) -> dict:
    url = f"{GITHUB_RAW_BASE.rstrip('/')}/{relative_path.lstrip('/')}"
    try:
        from proxy_pool import cursor_urlopen

        with cursor_urlopen(url, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except ImportError:
        with urlopen(url, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))


def update_in_progress() -> bool:
    if not UPDATE_LOCK.is_file():
        return False
    try:
        pid = int(UPDATE_LOCK.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        UPDATE_LOCK.unlink(missing_ok=True)
        return False


def read_update_log_tail(limit: int = 2000) -> str:
    if not UPDATE_LOG.is_file():
        return ""
    try:
        return UPDATE_LOG.read_text(encoding="utf-8", errors="ignore")[-limit:]
    except OSError:
        return ""


def start_panel_update() -> tuple[dict, int]:
    if update_in_progress():
        return {"status": "already_running", "in_progress": True}, 409

    script_path = f"{INSTALL_DIR}/scripts/panel-update.sh"
    if not Path(script_path).is_file():
        return {"error": f"update script missing: {script_path}"}, 500

    ensure_state_dir()
    env = {
        **os.environ,
        "INSTALL_DIR": str(INSTALL_DIR),
        "STATE_DIR": str(STATE_DIR),
    }
    if running_inside_docker():
        cmd = [
            "nsenter",
            "-t",
            "1",
            "-m",
            "-u",
            "-i",
            "-n",
            "-w",
            "--",
            "env",
            f"INSTALL_DIR={INSTALL_DIR}",
            f"STATE_DIR={STATE_DIR}",
            "bash",
            script_path,
        ]
    else:
        if shutil.which("systemd-run"):
            cmd = [
                "systemd-run",
                "--unit=agentcontrol-update",
                "--collect",
                "--no-block",
                "--property=Type=oneshot",
                f"--property=WorkingDirectory={INSTALL_DIR}",
                f"--setenv=INSTALL_DIR={INSTALL_DIR}",
                f"--setenv=STATE_DIR={STATE_DIR}",
                f"--setenv=CONFIG_DIR={os.environ.get('CONFIG_DIR', '/etc/agentcontrol')}",
                "/bin/bash",
                script_path,
            ]
        else:
            cmd = ["bash", script_path]
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
            cwd=str(INSTALL_DIR),
        )
    except OSError as exc:
        if cmd and cmd[0] == "systemd-run":
            try:
                subprocess.Popen(
                    ["bash", script_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=env,
                    start_new_session=True,
                    cwd=str(INSTALL_DIR),
                )
            except OSError as fallback_exc:
                return {"error": f"failed to start update: {fallback_exc}"}, 500
        else:
            return {"error": f"failed to start update: {exc}"}, 500

    return {"status": "started", "in_progress": True}, 202


CONFIG_SEARCH = [
    Path("/etc/agentcontrol/config.yaml"),
    Path(__file__).resolve().parent / "config.yaml",
    Path(__file__).resolve().parent / "config.example.yaml",
]


CONFIG_FILE: Path | None = None


def load_config() -> dict:
    global CONFIG_FILE
    for path in CONFIG_SEARCH:
        if path.is_file():
            with open(path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            CONFIG_FILE = path
            break
    else:
        cfg = {}
        CONFIG_FILE = CONFIG_SEARCH[0]

    cfg.setdefault("port", int(os.environ.get("PORT", 30228)))
    cfg.setdefault("scan_root", os.environ.get("SCAN_ROOT", "/root"))
    cfg.setdefault("idle_release_seconds", int(os.environ.get("IDLE_RELEASE_SECONDS", 43200)))
    cfg.setdefault("api_key_file", os.environ.get("API_KEY_FILE", "/etc/agentcontrol/api-key"))
    cfg.setdefault("exclude_prefixes", ["."])
    cfg.setdefault("exclude_dirs", [])
    cfg.setdefault("state_dir", os.environ.get("STATE_DIR", "/var/lib/agentcontrol"))
    cfg.setdefault("agent_bin", os.environ.get("AGENT_BIN", ""))
    cfg.setdefault("worker_ready_timeout", int(os.environ.get("WORKER_READY_TIMEOUT", 45)))
    cfg.setdefault("auth_password_file", os.environ.get("AUTH_PASSWORD_FILE", "/etc/agentcontrol/auth-password"))
    cfg.setdefault("default_model", os.environ.get("DEFAULT_MODEL", ""))
    cfg.setdefault("git_remote_template", os.environ.get("GIT_REMOTE_TEMPLATE", ""))
    cfg.setdefault("git_remote_overrides", {})
    cfg.setdefault("opencode_bin", os.environ.get("OPENCODE_BIN", ""))
    cfg.setdefault("opencode_port", int(os.environ.get("OPENCODE_PORT", "4096")))
    cfg.setdefault("opencode_hostname", os.environ.get("OPENCODE_HOSTNAME", "127.0.0.1"))
    cfg.setdefault(
        "opencode_secrets_file",
        os.environ.get("OPENCODE_SECRETS_FILE", "/etc/agentcontrol/opencode-secrets.json"),
    )
    cfg.setdefault(
        "opencode_env_file",
        os.environ.get("OPENCODE_ENV_FILE", "/etc/agentcontrol/opencode-providers.env"),
    )
    cfg.setdefault(
        "opencode_serve_password_file",
        os.environ.get("OPENCODE_SERVE_PASSWORD_FILE", "/etc/agentcontrol/opencode-serve-password"),
    )
    cfg.setdefault("internal_port", int(os.environ.get("INTERNAL_PORT", "0") or 0))
    cfg.setdefault("bind_host", os.environ.get("BIND_HOST", ""))
    cfg.setdefault("public_hostname", os.environ.get("PUBLIC_HOSTNAME", ""))
    cfg.setdefault("tls_cert_file", os.environ.get("TLS_CERT_FILE", ""))
    cfg.setdefault("tls_key_file", os.environ.get("TLS_KEY_FILE", ""))
    cfg.setdefault("acme_email", os.environ.get("ACME_EMAIL", ""))
    cfg.setdefault(
        "session_days",
        int(os.environ.get("SESSION_DAYS", str(panel_sessions.DEFAULT_SESSION_DAYS))),
    )

    if os.environ.get("PORT"):
        cfg["port"] = int(os.environ["PORT"])
    if os.environ.get("INTERNAL_PORT"):
        cfg["internal_port"] = int(os.environ["INTERNAL_PORT"])
    if os.environ.get("BIND_HOST"):
        cfg["bind_host"] = os.environ["BIND_HOST"]
    for key, env_name in (
        ("state_dir", "STATE_DIR"),
        ("scan_root", "SCAN_ROOT"),
        ("api_key_file", "API_KEY_FILE"),
        ("auth_password_file", "AUTH_PASSWORD_FILE"),
    ):
        if os.environ.get(env_name):
            cfg[key] = os.environ[env_name]

    return cfg


def save_config_patch(updates: dict) -> None:
    path = CONFIG_FILE or CONFIG_SEARCH[0]
    current: dict = {}
    if path.is_file():
        with open(path, encoding="utf-8") as f:
            current = yaml.safe_load(f) or {}
    current.update(updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(current, f, default_flow_style=False, sort_keys=False)
    CFG.update(updates)


CFG = load_config()
SETTINGS = build_settings(CFG)
CONFIG_PROBLEMS = validate_settings(SETTINGS)
load_bridge_settings()  # /etc/agentcontrol/.env → os.environ
STATE_DIR = Path(CFG["state_dir"])
STORE = Store(STATE_DIR / "panel.db")
STORE.initialize()
SESSIONS = panel_sessions.SessionStore(STATE_DIR / "sessions.db")
SESSIONS.initialize()
NOTIFIER = Notifier(STORE, SETTINGS.notifications_file)
LOGIN_LIMITER = security_mod.LoginLimiter(
    SETTINGS.max_login_attempts,
    max(SETTINGS.lockout_seconds, 300),
    SETTINGS.lockout_seconds,
)
AUTH_FAILURE_LOG = STATE_DIR / "auth.log"
PROCESS_START = time.time()
MONITOR = None
opencode_panel.reconcile_serve_state(STATE_DIR)
STATE_FILE = STATE_DIR / "workers.json"
WORKER_DATA_ROOT = STATE_DIR / "worker-data"
RECENT_TOUCHES_FILE = STATE_DIR / "recent-touches.json"
UPDATE_LOG = STATE_DIR / "update.log"
UPDATE_LOCK = STATE_DIR / "update.lock"
METRICS_HISTORY: deque = deque(maxlen=1800)
try:
    for _point in STORE.metrics_since(time.time() - 7 * 86400, limit=1800):
        METRICS_HISTORY.append(
            {
                "t": _point.get("ts"),
                "cpu": _point.get("cpu"),
                "load_pct": _point.get("load_pct"),
                "ram": _point.get("ram"),
                "disk": _point.get("disk"),
                "swap": _point.get("swap"),
                "net": _point.get("net"),
            }
        )
except Exception:
    pass
_CPU_CACHE = {"percent": 0.0, "cores": 1, "at": 0.0}
_DOCKER_CACHE = {"data": None, "at": 0.0}
_NET_CACHE = {"rx": 0, "tx": 0, "at": 0.0}
_DISK_IO_CACHE = {"io_ms": 0, "at": 0.0}
app = Flask(__name__)
if CFG.get("internal_port"):
    # The bundled Caddy terminates TLS and forwards X-Forwarded-* headers.
    # Only trust a single proxy hop, and only when the app is bound to loopback
    # behind that proxy (internal_port is configured).
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)


def read_panel_password() -> str | None:
    path = Path(CFG["auth_password_file"])
    if path.is_file():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    return None


def auth_enabled() -> bool:
    return bool(read_panel_password())


AUTH_COOKIE = "agentcontrol_auth"  # legacy cookie that used to hold the raw password
AUTH_COOKIE_MAX_AGE = 60 * 60 * 24 * 30
SESSION_COOKIE = panel_sessions.SESSION_COOKIE


def session_ttl_seconds() -> float:
    try:
        days = float(CFG.get("session_days", panel_sessions.DEFAULT_SESSION_DAYS))
    except (TypeError, ValueError):
        days = float(panel_sessions.DEFAULT_SESSION_DAYS)
    return max(0.1, days) * 24 * 60 * 60


def header_password() -> str:
    header = request.headers.get("X-AgentControl-Auth", "")
    if header:
        return header.strip()
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def session_token() -> str:
    return request.cookies.get(SESSION_COOKIE, "").strip()


def set_session_cookie(response, token: str):
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(session_ttl_seconds()),
        httponly=True,
        samesite="Lax",
        secure=request.is_secure,
        path="/",
    )
    return response


def clear_session_cookie(response):
    response.set_cookie(
        SESSION_COOKIE,
        "",
        max_age=0,
        httponly=True,
        samesite="Lax",
        secure=request.is_secure,
        path="/",
    )
    return response


def create_session() -> str:
    return SESSIONS.create(
        session_ttl_seconds(),
        ip=request.remote_addr or "",
        ua=request.headers.get("User-Agent", ""),
    )


def panel_session_ok() -> bool:
    if not auth_enabled():
        return False
    candidate = header_password()
    if candidate and password_ok(candidate):
        return True
    return SESSIONS.validate(session_token())


def password_ok(candidate: str) -> bool:
    expected = read_panel_password()
    if not expected:
        return True
    if not security_mod.verify_password(candidate, expected):
        return False
    if not security_mod.password_is_hashed(expected):
        security_mod.upgrade_password_file(Path(CFG["auth_password_file"]), candidate)
    return True


def write_panel_password(password: str) -> None:
    security_mod.write_password_file(Path(CFG["auth_password_file"]), password.strip())


def token_header() -> str:
    header = request.headers.get("X-AgentControl-Token", "").strip()
    if header:
        return header
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        candidate = auth[7:].strip()
        if candidate.startswith(security_mod.TOKEN_PREFIX + "_"):
            return candidate
    return ""


def resolve_actor() -> dict | None:
    """Return {name, role, source} for the request, or None when unauthenticated."""
    token = token_header()
    if token:
        actor = security_mod.verify_token(token, STORE)
        if actor:
            return {"name": actor["name"], "role": actor["role"], "source": "token"}
    if auth_enabled():
        candidate = header_password()
        if candidate and not candidate.startswith(security_mod.TOKEN_PREFIX + "_"):
            if password_ok(candidate):
                return {"name": "panel-password", "role": security_mod.ROLE_ADMIN, "source": "header"}
        if SESSIONS.validate(session_token()):
            return {"name": "panel", "role": security_mod.ROLE_ADMIN, "source": "session"}
    return None


def required_role(path: str, method: str) -> str:
    if path.startswith(("/api/tokens", "/api/audit", "/api/notifications", "/api/terminal", "/api/agent")):
        return security_mod.ROLE_ADMIN
    if path.startswith("/api/settings"):
        return security_mod.ROLE_ADMIN
    if path.startswith("/api/opencode/update") or path.startswith("/api/opencode/restart"):
        return security_mod.ROLE_ADMIN
    if path == "/api/update/apply":
        return security_mod.ROLE_ADMIN
    if method in {"GET", "HEAD", "OPTIONS"}:
        return security_mod.ROLE_READ
    return security_mod.ROLE_OPS


def same_origin_ok() -> bool:
    """CSRF defence for state-changing requests with a browser Origin header."""
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return True
    origin = request.headers.get("Origin")
    if not origin:
        return True
    return origin.rstrip("/") == request.host_url.rstrip("/")


def write_api_key(key: str) -> None:
    path = Path(CFG["api_key_file"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key.strip(), encoding="utf-8")
    os.chmod(path, 0o600)


SETUP_TOKEN_FILE = Path("/etc/agentcontrol/setup-token")


def ensure_setup_token() -> str:
    try:
        existing = SETUP_TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        existing = ""
    if existing:
        return existing
    token = secrets.token_urlsafe(24)
    try:
        SETUP_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETUP_TOKEN_FILE.write_text(token + "\n", encoding="utf-8")
        SETUP_TOKEN_FILE.chmod(0o600)
    except OSError:
        pass
    return token


def setup_allowed() -> bool:
    """Allow first-run setup from loopback, a valid setup token, or a session."""
    client = request.remote_addr or ""
    if client in {"127.0.0.1", "::1", "localhost"}:
        return True
    if auth_enabled() and panel_session_ok():
        return True
    supplied = (
        request.headers.get("X-Setup-Token", "")
        or request.args.get("setup", "")
        or (request.get_json(silent=True) or {}).get("setup_token", "")
    )
    if not supplied:
        return False
    expected = ensure_setup_token()
    return secrets.compare_digest(str(supplied).strip(), expected)


def api_key_configured() -> bool:
    return bool(read_api_key())


@app.before_request
def require_panel_auth():
    open_endpoints = {
        "health",
        "index",
        "public_status_page",
        "api_public_status",
        "api_prometheus",
        "api_openapi",
        "connectivity_test",
        "connectivity_test_minimal_html",
        "api_auth_login",
        "api_auth_logout",
        "api_auth_status",
        "api_setup_status",
        "api_setup_password",
        "api_fleet_sso",
        "api_caddy_allow",
    }
    g.actor_name = "anonymous"
    g.actor_role = ""
    g.actor_source = "anonymous"
    g.csp_nonce = secrets.token_urlsafe(16)
    endpoint = (request.endpoint or "").split(".")[-1]
    if endpoint in open_endpoints:
        return None
    if endpoint == "api_setup_api_key" and not api_key_configured():
        return None
    if not request.path.startswith("/api/"):
        return None
    if not auth_enabled():
        return jsonify({"error": "setup_required", "needs_password": True, "code": "setup_required"}), 403
    if not same_origin_ok():
        return jsonify({"error": "cross-origin request rejected", "code": "cross_origin"}), 403
    actor = resolve_actor()
    if actor is None:
        return jsonify({"error": "unauthorized", "code": "unauthorized"}), 401
    g.actor_name = actor["name"]
    g.actor_role = actor["role"]
    g.actor_source = actor["source"]
    needed = required_role(request.path, request.method)
    if not security_mod.role_allows(actor["role"], needed):
        return jsonify(
            {
                "error": f"role '{actor['role']}' cannot perform this action",
                "code": "insufficient_role",
                "required": needed,
            }
        ), 403
    return None


@app.after_request
def security_headers(response):
    if request.path.startswith("/oc/") or request.path == "/oc":
        return response
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        f"script-src 'self' 'nonce-{getattr(g, 'csp_nonce', '')}'; connect-src 'self' ws: wss:; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    )
    if request.is_secure:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response



def worker_id_for(folder: str) -> str:
    return str(uuid.uuid5(WORKER_NAMESPACE, f"agentcontrol:{folder}"))


def worker_name(folder: str) -> str:
    return f"agentcontrol-{folder}"


def agent_url(folder: str) -> str:
    worker_id = worker_id_for(folder)
    model = (CFG.get("default_model") or "").strip()
    if model:
        return f"https://cursor.com/agents?model={quote(model)}#workerId={worker_id}"
    return CURSOR_AGENTS_URL.format(worker_id=worker_id)


def list_agent_models() -> list[dict]:
    agent_bin = find_agent_bin()
    if not agent_bin:
        return []
    env = apply_proxy_env(os.environ.copy())
    env = apply_cursor_bridge_env(env)
    api_key = read_api_key()
    if api_key:
        env["CURSOR_API_KEY"] = api_key
    try:
        result = subprocess.run(
            [agent_bin, "--list-models"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    models: list[dict] = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line or line.lower().startswith("available models"):
            continue
        if " - " in line:
            model_id, label = line.split(" - ", 1)
        else:
            model_id, label = line, line
        models.append({"id": model_id.strip(), "label": label.strip()})
    return models


def mgmt_port(name: str) -> int:
    try:
        digest = hashlib.md5(name.encode(), usedforsecurity=False).hexdigest()
    except TypeError:
        digest = hashlib.md5(name.encode()).hexdigest()
    return MGMT_PORT_BASE + (int(digest[:4], 16) % MGMT_PORT_RANGE)


def find_agent_bin() -> str | None:
    if CFG.get("agent_bin"):
        p = Path(CFG["agent_bin"])
        if p.is_file():
            return str(p)
    found = shutil.which("agent")
    if found:
        return found
    for candidate in (
        Path.home() / ".local/bin/agent",
        Path("/root/.local/bin/agent"),
        Path("/usr/local/bin/agent"),
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def relative_time_en(ts: float) -> str:
    delta = max(0, time.time() - ts)
    if delta < 45:
        return "just now"
    if delta < 3600:
        m = int(delta / 60)
        return f"{m}m ago"
    if delta < 86400:
        h = int(delta / 3600)
        return f"{h}h ago"
    if delta < 2592000:
        d = int(delta / 86400)
        return f"{d}d ago"
    if delta < 31536000:
        mo = int(delta / 2592000)
        return f"{mo}mo ago"
    y = int(delta / 31536000)
    return f"{y}y ago"


def read_api_key() -> str | None:
    path = Path(CFG["api_key_file"])
    if not path.is_file():
        return None
    key = path.read_text(encoding="utf-8").strip()
    return key or None


def verify_cursor_api_key(api_key: str) -> dict[str, object]:
    """Check that Cursor accepts this personal API key for worker auth."""
    key = api_key.strip()
    if not key:
        return {"valid": False, "error": "api_key required"}
    if not key.startswith("crsr_"):
        return {
            "valid": False,
            "error": "API key must be a Cursor personal key (starts with crsr_)",
        }

    agent_bin = find_agent_bin()
    if not agent_bin:
        return {"valid": False, "error": "agent CLI not found — run install.sh again"}

    proxy_gate = ensure_proxy_ready()
    if not proxy_gate.get("ok") and proxy_gate.get("mode") != "direct":
        return {
            "valid": False,
            "error": proxy_gate.get("error") or "proxy is not ready — fix proxy settings first",
        }

    check_dir = Path("/tmp/agentcontrol-key-check")
    check_dir.mkdir(parents=True, exist_ok=True)
    env = apply_proxy_env(os.environ.copy())
    env = apply_cursor_bridge_env(env)
    env["CURSOR_API_KEY"] = key
    env["PATH"] = "/root/.local/bin:" + env.get("PATH", "")

    proc = subprocess.Popen(
        [
            agent_bin,
            "worker",
            "--worker-dir",
            str(check_dir),
            "--data-dir",
            str(check_dir / ".cursor-data"),
            "--name",
            "agentcontrol-key-check",
            "start",
            "--verbose",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        start_new_session=True,
    )

    output_lines: list[str] = []
    deadline = time.time() + 25
    try:
        assert proc.stdout is not None
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
                continue
            output_lines.append(line)
            lower = line.lower()
            if "authenticated with api key" in lower:
                return {"valid": True}
            if "api key is invalid" in lower or (
                "invalid" in lower and "api key" in lower
            ):
                return {
                    "valid": False,
                    "error": "invalid API key — create a new personal key at cursor.com/settings",
                }
        if proc.poll() is None:
            return {"valid": False, "error": "API key verification timed out"}
        tail = "".join(output_lines)[-500:].strip()
        return {
            "valid": False,
            "error": "could not verify API key with Cursor",
            "detail": tail or None,
        }
    finally:
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    proc.kill()
                proc.wait(timeout=5)


def fmt_bytes(num: int | float) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(num)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def fmt_duration_en(seconds: float) -> str:
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def read_os_name() -> str:
    try:
        with open("/etc/os-release", encoding="utf-8") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.platform()


def primary_ip() -> str:
    try:
        result = subprocess.run(
            ["hostname", "-I"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        ip = result.stdout.strip().split()
        if ip:
            return ip[0]
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "—"


def cpu_usage_percent(sample_seconds: float = 0.25) -> tuple[float, int]:
    now = time.time()
    if now - _CPU_CACHE["at"] < 1.5 and _CPU_CACHE["at"] > 0:
        return _CPU_CACHE["percent"], _CPU_CACHE["cores"]

    def read_cpu() -> tuple[int, int]:
        with open("/proc/stat", encoding="utf-8") as f:
            parts = f.readline().split()[1:]
        values = [int(v) for v in parts]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        return total, idle

    total1, idle1 = read_cpu()
    time.sleep(sample_seconds)
    total2, idle2 = read_cpu()
    total_delta = total2 - total1
    idle_delta = idle2 - idle1
    cores = os.cpu_count() or 1
    if total_delta <= 0:
        percent = _CPU_CACHE["percent"]
    else:
        percent = round(max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100)), 1)
    _CPU_CACHE.update({"percent": percent, "cores": cores, "at": time.time()})
    return percent, cores


def memory_stats() -> dict:
    data: dict[str, int] = {}
    with open("/proc/meminfo", encoding="utf-8") as f:
        for line in f:
            key, value = line.split(":", 1)
            data[key] = int(value.split()[0]) * 1024
    total = data.get("MemTotal", 0)
    available = data.get("MemAvailable", data.get("MemFree", 0))
    used = max(0, total - available)
    swap_total = data.get("SwapTotal", 0)
    swap_free = data.get("SwapFree", 0)
    swap_used = max(0, swap_total - swap_free)
    return {
        "total": total,
        "used": used,
        "percent": round((used / total) * 100, 1) if total else 0,
        "swap_total": swap_total,
        "swap_used": swap_used,
        "swap_percent": round((swap_used / swap_total) * 100, 1) if swap_total else 0,
    }


def disk_stats(path: str) -> dict:
    usage = shutil.disk_usage(path)
    percent = round((usage.used / usage.total) * 100, 1) if usage.total else 0
    return {
        "path": path,
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
        "percent": percent,
    }


def load_average() -> list[float]:
    with open("/proc/loadavg", encoding="utf-8") as f:
        return [float(x) for x in f.read().split()[:3]]


def load_percent(load_1: float, cores: int) -> float:
    if cores <= 0:
        return 0.0
    return round(min(999.0, (load_1 / cores) * 100), 1)


def disk_io_stats() -> dict:
    """Approximate aggregate disk busy % from /proc/diskstats (between samples)."""
    now = time.time()
    io_ms = 0
    try:
        with open("/proc/diskstats", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 13:
                    continue
                name = parts[2]
                if name.startswith(("loop", "ram", "dm-")):
                    continue
                io_ms += int(parts[12])
    except OSError:
        return {"busy_percent": None}

    busy = None
    if _DISK_IO_CACHE["at"] > 0:
        dt = now - _DISK_IO_CACHE["at"]
        if dt > 0:
            delta = io_ms - _DISK_IO_CACHE["io_ms"]
            busy = round(min(100.0, (delta / (dt * 1000)) * 100), 1)
    _DISK_IO_CACHE.update({"io_ms": io_ms, "at": now})
    return {"busy_percent": busy}


def network_stats() -> dict:
    now = time.time()
    rx_total = tx_total = 0
    with open("/proc/net/dev", encoding="utf-8") as f:
        for line in f.readlines()[2:]:
            parts = line.split()
            if len(parts) < 10:
                continue
            iface = parts[0].rstrip(":")
            if iface == "lo":
                continue
            rx_total += int(parts[1])
            tx_total += int(parts[9])

    rx_rate = tx_rate = 0.0
    if _NET_CACHE["at"] > 0:
        dt = now - _NET_CACHE["at"]
        if dt > 0:
            rx_rate = max(0.0, (rx_total - _NET_CACHE["rx"]) / dt)
            tx_rate = max(0.0, (tx_total - _NET_CACHE["tx"]) / dt)

    _NET_CACHE.update({"rx": rx_total, "tx": tx_total, "at": now})
    total_rate = rx_rate + tx_rate
    return {
        "rx_bytes_total": rx_total,
        "tx_bytes_total": tx_total,
        "rx_rate": rx_rate,
        "tx_rate": tx_rate,
        "total_rate": total_rate,
        "rx_rate_human": fmt_bytes(rx_rate) + "/s",
        "tx_rate_human": fmt_bytes(tx_rate) + "/s",
        "total_rate_human": fmt_bytes(total_rate) + "/s",
    }


def parse_docker_percent(value: str) -> float:
    try:
        return float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return 0.0


def parse_docker_size_bytes(text: str) -> int:
    text = str(text).strip().upper()
    match = re.match(r"([\d.]+)\s*([A-Z]+)", text)
    if not match:
        return 0
    value = float(match.group(1))
    unit = match.group(2)
    multipliers = {
        "B": 1,
        "KB": 1000,
        "KIB": 1024,
        "MB": 1000**2,
        "MIB": 1024**2,
        "GB": 1000**3,
        "GIB": 1024**3,
        "TB": 1000**4,
        "TIB": 1024**4,
    }
    return int(value * multipliers.get(unit, 1))


def parse_docker_mem_used_bytes(mem_usage: str) -> int:
    if not mem_usage or mem_usage == "—":
        return 0
    return parse_docker_size_bytes(mem_usage.split("/")[0].strip())


def docker_inspect_labels() -> dict[str, dict]:
    if not shutil.which("docker"):
        return {}
    try:
        result = subprocess.run(
            [
                "docker",
                "ps",
                "-q",
                "--format",
                "{{.ID}}",
            ],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
        ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not ids:
            return {}
        inspect = subprocess.run(
            [
                "docker",
                "inspect",
                *ids,
                "--format",
                "{{.Name}}|{{index .Config.Labels \"com.docker.compose.project\"}}|{{index .Config.Labels \"com.docker.compose.service\"}}",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}

    labels: dict[str, dict] = {}
    for line in inspect.stdout.splitlines():
        parts = line.strip().split("|", 2)
        if not parts:
            continue
        name = parts[0].lstrip("/")
        labels[name] = {
            "compose_project": parts[1] if len(parts) > 1 and parts[1] else None,
            "compose_service": parts[2] if len(parts) > 2 and parts[2] else None,
        }
    return labels


def enrich_container_shares(containers: list[dict]) -> tuple[dict, list[dict]]:
    total_cpu = sum(c["cpu_percent"] for c in containers)
    total_mem = sum(c["mem_bytes"] for c in containers)
    totals = {
        "cpu_percent": round(total_cpu, 2),
        "mem_bytes": total_mem,
        "mem_human": fmt_bytes(total_mem),
    }
    for container in containers:
        container["cpu_share"] = round((container["cpu_percent"] / total_cpu) * 100, 1) if total_cpu > 0 else 0.0
        container["mem_share"] = round((container["mem_bytes"] / total_mem) * 100, 1) if total_mem > 0 else 0.0
    return totals, containers


def docker_grouped_stats() -> dict:
    if not shutil.which("docker"):
        return {"containers": [], "groups": [], "totals": {"cpu_percent": 0, "mem_bytes": 0, "mem_human": "0 B"}}

    labels = docker_inspect_labels()
    try:
        result = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {"containers": [], "groups": [], "totals": {"cpu_percent": 0, "mem_bytes": 0, "mem_human": "0 B"}}

    containers: list[dict] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = item.get("Name") or item.get("Container") or "unknown"
        meta = labels.get(name, {})
        mem_usage = item.get("MemUsage") or "—"
        containers.append(
            {
                "name": name,
                "service": meta.get("compose_service") or name,
                "compose_project": meta.get("compose_project"),
                "cpu_percent": parse_docker_percent(item.get("CPUPerc", "0")),
                "mem_percent": parse_docker_percent(item.get("MemPerc", "0")),
                "mem_bytes": parse_docker_mem_used_bytes(mem_usage),
                "mem_usage": mem_usage,
                "net_io": item.get("NetIO") or "—",
            }
        )

    totals, containers = enrich_container_shares(containers)
    grouped: dict[str, dict] = {}
    for container in containers:
        project = container.get("compose_project")
        if project:
            group_id = f"compose:{project}"
            group_type = "compose"
            group_label = project
        else:
            group_id = f"standalone:{container['name']}"
            group_type = "standalone"
            group_label = container["name"]

        if group_id not in grouped:
            grouped[group_id] = {
                "id": group_id,
                "label": group_label,
                "type": group_type,
                "containers": [],
                "cpu_percent": 0.0,
                "mem_bytes": 0,
            }
        group = grouped[group_id]
        group["containers"].append(container)
        group["cpu_percent"] += container["cpu_percent"]
        group["mem_bytes"] += container["mem_bytes"]

    groups: list[dict] = []
    for group in grouped.values():
        group["cpu_percent"] = round(group["cpu_percent"], 2)
        group["mem_human"] = fmt_bytes(group["mem_bytes"])
        group["container_count"] = len(group["containers"])
        group["cpu_share"] = round((group["cpu_percent"] / totals["cpu_percent"]) * 100, 1) if totals["cpu_percent"] > 0 else 0.0
        group["mem_share"] = round((group["mem_bytes"] / totals["mem_bytes"]) * 100, 1) if totals["mem_bytes"] > 0 else 0.0
        group["containers"].sort(key=lambda c: (c["cpu_percent"], c["mem_bytes"]), reverse=True)
        groups.append(group)

    groups.sort(key=lambda g: (g["cpu_percent"], g["mem_bytes"]), reverse=True)
    containers.sort(key=lambda c: (c["cpu_percent"], c["mem_bytes"]), reverse=True)
    return {"containers": containers, "groups": groups, "totals": totals}


def docker_container_stats() -> list[dict]:
    return docker_grouped_stats()["containers"]


def uptime_seconds() -> float:
    with open("/proc/uptime", encoding="utf-8") as f:
        return float(f.read().split()[0])


def agent_version() -> str | None:
    agent_bin = find_agent_bin()
    if not agent_bin:
        return None
    try:
        result = subprocess.run(
            [agent_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        return (result.stdout or result.stderr).strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def docker_stats() -> dict:
    now = time.time()
    if _DOCKER_CACHE["data"] is not None and now - _DOCKER_CACHE["at"] < 10:
        return _DOCKER_CACHE["data"]
    if not shutil.which("docker"):
        return {"available": False}
    try:
        running = subprocess.run(
            ["docker", "ps", "-q"],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
        all_ps = subprocess.run(
            ["docker", "ps", "-aq"],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
        version = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        running_n = len([x for x in running.stdout.splitlines() if x.strip()])
        total_n = len([x for x in all_ps.stdout.splitlines() if x.strip()])
        grouped = docker_grouped_stats()
        data = {
            "available": True,
            "version": (version.stdout or "").strip() or None,
            "running": running_n,
            "total": total_n,
            "stopped": max(0, total_n - running_n),
            "storage": [],
            "containers": grouped["containers"],
            "groups": grouped["groups"],
            "totals": grouped["totals"],
        }
        _DOCKER_CACHE.update({"data": data, "at": now})
        return data
    except (OSError, subprocess.SubprocessError):
        return {
            "available": True,
            "running": None,
            "total": None,
            "stopped": None,
            "storage": [],
            "containers": [],
            "groups": [],
            "totals": {"cpu_percent": 0, "mem_bytes": 0, "mem_human": "0 B"},
        }


def record_metrics(snapshot: dict) -> None:
    docker = snapshot.get("docker") or {}
    network = snapshot.get("network") or {}
    swap_pct = snapshot["memory"].get("swap_percent")
    METRICS_HISTORY.append(
        {
            "t": time.time(),
            "cpu": snapshot["cpu"]["percent"],
            "load_pct": snapshot["cpu"]["load_percent"],
            "ram": snapshot["memory"]["percent"],
            "disk": snapshot["disk_root"]["percent"],
            "swap": swap_pct if swap_pct is not None else 0,
            "net": network.get("total_rate") or 0,
            "docker_running": docker.get("running") or 0,
        }
    )


def collect_system_info() -> dict:
    mem = memory_stats()
    root_disk = disk_stats("/")
    scan_root = CFG["scan_root"]
    scan_disk = disk_stats(scan_root) if scan_root != "/" else None
    state = reconcile_state()
    folders = list_folders()
    running_workers = [name for name, info in state.items() if info.get("pid") and is_running(info["pid"])]
    loads = load_average()
    cpu_percent, cpu_cores = cpu_usage_percent()
    net = network_stats()

    result = {
        "hostname": socket.gethostname(),
        "ip": primary_ip(),
        "os": read_os_name(),
        "kernel": platform.release(),
        "uptime_seconds": uptime_seconds(),
        "uptime_human": fmt_duration_en(uptime_seconds()),
        "cpu": {
            "percent": cpu_percent,
            "cores": cpu_cores,
            "load_1": loads[0],
            "load_5": loads[1],
            "load_15": loads[2],
            "load_percent": load_percent(loads[0], cpu_cores),
        },
        "memory": {
            "used": mem["used"],
            "total": mem["total"],
            "percent": mem["percent"],
            "used_human": fmt_bytes(mem["used"]),
            "total_human": fmt_bytes(mem["total"]),
            "swap_used_human": fmt_bytes(mem["swap_used"]) if mem["swap_total"] else None,
            "swap_percent": mem["swap_percent"] if mem["swap_total"] else None,
        },
        "disk_root": {
            **root_disk,
            "used_human": fmt_bytes(root_disk["used"]),
            "total_human": fmt_bytes(root_disk["total"]),
            "free_human": fmt_bytes(root_disk["free"]),
        },
        "disk_scan_root": (
            {
                **scan_disk,
                "used_human": fmt_bytes(scan_disk["used"]),
                "total_human": fmt_bytes(scan_disk["total"]),
                "free_human": fmt_bytes(scan_disk["free"]),
            }
            if scan_disk
            else None
        ),
        "disk_io": disk_io_stats(),
        "agent": {
            "installed": bool(find_agent_bin()),
            "path": find_agent_bin(),
            "version": agent_version(),
            "api_key_set": bool(read_api_key()),
        },
        "panel": {
            "port": CFG["port"],
            "scan_root": CFG["scan_root"],
            "idle_hours": round(CFG["idle_release_seconds"] / 3600, 1),
            "project_count": len(folders),
            "running_workers": len(running_workers),
            "running_worker_names": running_workers,
            "version": load_version().get("version"),
            "host_runtime": host_runtime_status(),
        },
        "docker": docker_stats(),
        "network": net,
        "history_points": len(METRICS_HISTORY),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    record_metrics(result)
    return result


def ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def worker_data_dir(name: str) -> Path:
    """Per-project Cursor data dir so multiple workers can run concurrently."""
    path = WORKER_DATA_ROOT / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def _seed_workers_from_json() -> None:
    """One-time import of the legacy workers.json into SQLite."""
    if not STATE_FILE.is_file() or STORE.workers():
        return
    try:
        legacy = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if isinstance(legacy, dict) and legacy:
        STORE.replace_workers(legacy)


_seed_workers_from_json()


def load_state() -> dict:
    ensure_state_dir()
    return STORE.workers()


def save_state(state: dict) -> None:
    ensure_state_dir()
    STORE.replace_workers(state)


def is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def reconcile_state() -> dict:
    state = load_state()
    changed = False
    for name, info in list(state.items()):
        pid = info.get("pid")
        if not pid or not is_running(pid):
            del state[name]
            changed = True
    if changed:
        save_state(state)
    return state


def is_folder_ignored(path: Path) -> bool:
    return (path / IGNORE_MARKER).is_file()


def is_excluded(name: str) -> bool:
    if name in CFG.get("exclude_dirs", []):
        return True
    for prefix in CFG.get("exclude_prefixes", []):
        if name.startswith(prefix):
            return True
    return False


def folder_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def load_recent_touches() -> dict[str, float]:
    if not RECENT_TOUCHES_FILE.is_file():
        return {}
    try:
        raw = json.loads(RECENT_TOUCHES_FILE.read_text(encoding="utf-8"))
        return {str(k): float(v) for k, v in raw.items()}
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return {}


def save_recent_touches(touches: dict[str, float]) -> None:
    ensure_state_dir()
    RECENT_TOUCHES_FILE.write_text(json.dumps(touches, indent=2) + "\n", encoding="utf-8")


def touch_project(name: str) -> float:
    touches = load_recent_touches()
    now = time.time()
    touches[name] = now
    save_recent_touches(touches)
    return now


def list_folders() -> list[dict]:
    root = Path(CFG["scan_root"])
    if not root.is_dir():
        return []

    state = reconcile_state()
    touches = load_recent_touches()
    folders = []
    for entry in root.iterdir():
        if not entry.is_dir() or is_excluded(entry.name) or is_folder_ignored(entry):
            continue
        name = entry.name
        info = state.get(name, {})
        pid = info.get("pid")
        running = bool(pid and is_running(pid))
        mtime = folder_mtime(entry)
        touched = touches.get(name) or mtime
        git_status = folder_git_status(entry)
        oc_healthy = opencode_panel.is_serve_healthy(
            CFG, opencode_panel.serve_password(CFG, read_panel_password())
        )
        folders.append(
            {
                "name": name,
                "path": str(entry),
                "running": running,
                "ready": worker_ready(name) if running else False,
                "worker_id": worker_id_for(name),
                "agent_url": agent_url(name),
                "opencode_path": opencode_panel.opencode_public_path(str(entry)),
                "opencode_healthy": oc_healthy,
                "started_at": info.get("started_at"),
                "pid": pid if running else None,
                "mtime": mtime,
                "mtime_relative": relative_time_en(mtime),
                "touched_at": touched,
                "touched_relative": relative_time_en(touched),
                "git_is_repo": git_status["is_repo"],
                "git_has_remote": git_status["has_remote"],
                "git_remote_url": git_status.get("remote_url"),
            }
        )

    folders.sort(key=lambda item: item.get("touched_at") or item["mtime"], reverse=True)
    return folders


def folder_path(name: str) -> Path | None:
    if "/" in name or name in (".", "..") or is_excluded(name):
        return None
    path = Path(CFG["scan_root"]) / name
    if path.is_dir():
        return path
    return None


WORKER_LOG_SESSION_MARKER = "--- agentcontrol worker start "

LOG_ERROR_PHRASES = (
    "failed to reach",
    "api key is invalid",
    "worker not found",
    "do not have access",
    "another worker daemon",
    "econnrefused",
    "connection refused",
    "unknown option",
    "unauthorized",
)


def worker_log_session(log_tail: str) -> str:
    if not log_tail:
        return ""
    if WORKER_LOG_SESSION_MARKER in log_tail:
        return log_tail.rsplit(WORKER_LOG_SESSION_MARKER, 1)[-1]
    return log_tail


def detect_worker_log_errors(log_tail: str) -> list[str]:
    if not log_tail:
        return []
    session = worker_log_session(log_tail)
    errors: list[str] = []
    for line in session.splitlines():
        text = line.strip()
        if not text:
            continue
        low = text.lower()
        if low.startswith("error:"):
            errors.append(text)
            continue
        if re.search(r"\b(ERROR|FATAL)\b", text) and "ctx=worker-mode" in text:
            errors.append(text)
            continue
        if any(phrase in low for phrase in LOG_ERROR_PHRASES):
            errors.append(text)
    return errors[-8:]


def worker_log_tail(info: dict) -> str:
    log_file = info.get("log_file")
    if not log_file or not Path(log_file).is_file():
        return ""
    try:
        return Path(log_file).read_text(encoding="utf-8", errors="ignore")[-4000:]
    except OSError:
        return ""


def probe_worker_readyz(port: int) -> dict | None:
    try:
        with urlopen(f"http://127.0.0.1:{int(port)}/readyz", timeout=1.5) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="ignore") or "{}")
            return {"http_status": resp.status, **body}
    except (URLError, OSError, ValueError, json.JSONDecodeError):
        return None


def worker_ready(name: str) -> bool:
    state = reconcile_state()
    info = state.get(name)
    if not info:
        return False
    pid = info.get("pid")
    if not pid or not is_running(pid):
        return False

    port = info.get("mgmt_port")
    if port:
        probe = probe_worker_readyz(port)
        if probe and int(probe.get("http_status", 0)) == 200:
            if probe.get("connected") is True:
                return True
            if str(probe.get("status", "")).lower() == "ok":
                return True

    tail = worker_log_tail(info)
    session = worker_log_session(tail)
    if detect_worker_log_errors(session):
        return False

    if session:
        markers = ("Private worker started", "Worker is now running", "connected: true")
        low = session.lower()
        if any(marker.lower() in low for marker in markers):
            return True

    started_at = info.get("started_at")
    wait_sec = int(CFG.get("worker_ready_timeout", 45))
    if started_at:
        try:
            started = datetime.fromisoformat(started_at)
            age = (datetime.now(timezone.utc) - started).total_seconds()
            return age >= min(wait_sec, 25)
        except ValueError:
            pass
    return False


def start_worker(name: str) -> tuple[dict, int]:
    path = folder_path(name)
    if not path:
        return {"error": "folder not found"}, 404

    agent_bin = find_agent_bin()
    if not agent_bin:
        return {"error": "agent CLI not found — run install.sh again"}, 500

    api_key = read_api_key()
    if not api_key:
        return {
            "error": "Cursor API key missing. Set it: echo YOUR_KEY | sudo tee /etc/agentcontrol/api-key"
        }, 500

    xray_settings = load_client_settings()
    proxy_gate = ensure_proxy_ready()
    if not proxy_gate.get("ok") and proxy_gate.get("mode") != "direct":
        return {
            "error": proxy_gate.get("error")
            or "HTTP proxy is not ready. Fix proxy pool in Settings before starting workers.",
        }, 503
    if xray_settings.get("enabled") and not proxy_listening(xray_settings):
        return {
            "error": "HTTP proxy is not listening on "
            f"{xray_settings.get('proxy_listen', '127.0.0.1')}:{xray_settings.get('proxy_port', 30229)}. "
            "Fix xray in Settings before starting workers.",
        }, 503

    runtime_error = worker_runtime_error(name)
    if runtime_error:
        return {"error": runtime_error}, 503

    state = reconcile_state()
    existing = state.get(name)
    if existing and existing.get("pid") and is_running(existing["pid"]):
        return {
            "status": "already_running",
            "name": name,
            "worker_id": worker_id_for(name),
            "agent_url": agent_url(name),
            "ready": worker_ready(name),
            "pid": existing["pid"],
        }, 200

    try:
        os.utime(path, None)
    except OSError:
        pass
    touch_project(name)
    git_prepare = ensure_worker_git(name, path, CFG)

    worker_id = worker_id_for(name)
    port = mgmt_port(name)
    env = apply_proxy_env(os.environ.copy())
    env = apply_cursor_bridge_env(env)
    env["CURSOR_AGENT_WORKER_ID"] = worker_id
    env["CURSOR_API_KEY"] = api_key
    env["PATH"] = "/root/.local/bin:" + env.get("PATH", "")
    default_model = (CFG.get("default_model") or "").strip()
    if default_model:
        env["CURSOR_MODEL"] = default_model

    log_file = STATE_DIR / f"{name}.log"
    marker = f"\n{WORKER_LOG_SESSION_MARKER}{datetime.now(timezone.utc).isoformat()} ---\n"
    try:
        with open(log_file, "ab") as log:
            log.write(marker.encode("utf-8"))
    except OSError:
        pass
    cmd = [
        agent_bin,
        "worker",
        "--worker-dir",
        str(path),
        "--data-dir",
        str(worker_data_dir(name)),
        "--name",
        worker_name(name),
        "--idle-release-timeout",
        str(CFG["idle_release_seconds"]),
        "--management-addr",
        f"127.0.0.1:{port}",
        "start",
        "--verbose",
    ]

    try:
        with open(log_file, "ab") as log:
            proc = subprocess.Popen(
                cmd,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
    except OSError as exc:
        return {"error": str(exc)}, 500

    state[name] = {
        "pid": proc.pid,
        "worker_id": worker_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "log_file": str(log_file),
        "mgmt_port": port,
    }
    save_state(state)

    return {
        "status": "started",
        "name": name,
        "worker_id": worker_id,
        "agent_url": agent_url(name),
        "ready": False,
        "pid": proc.pid,
        "git": git_prepare,
    }, 200


def restart_worker(name: str) -> tuple[dict, int]:
    if not folder_path(name):
        return {"error": "folder not found"}, 404
    state = reconcile_state()
    info = state.get(name)
    was_running = bool(info and info.get("pid") and is_running(info["pid"]))
    if was_running:
        stop_result, stop_code = stop_worker(name)
        if stop_code >= 400 and stop_result.get("error"):
            return stop_result, stop_code
        time.sleep(0.6)
    start_result, start_code = start_worker(name)
    start_result["restarted"] = True
    start_result["was_running"] = was_running
    return start_result, start_code


def stop_worker(name: str) -> tuple[dict, int]:
    if not folder_path(name):
        return {"error": "folder not found"}, 404

    state = reconcile_state()
    info = state.get(name)
    if not info or not info.get("pid"):
        return {"status": "not_running", "name": name}, 200

    pid = info["pid"]
    if is_running(pid):
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except ProcessLookupError:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    del state[name]
    save_state(state)
    if MONITOR is not None:
        MONITOR.note_stopped(name)
    return {"status": "stopped", "name": name, "pid": pid}, 200


def fleet_system_summary() -> dict:
    """Smaller system payload for Fleet (avoids DPI truncating large HTTP bodies)."""
    info = collect_system_info()
    info.pop("docker", None)
    panel = info.get("panel") or {}
    panel.pop("running_worker_names", None)
    info["panel"] = panel
    return info


FLEET_FOLDER_KEYS = (
    "name",
    "path",
    "running",
    "ready",
    "mtime_relative",
    "touched_relative",
    "worker_id",
    "git_is_repo",
    "git_has_remote",
    "opencode_path",
    "opencode_healthy",
)


def fleet_folders_summary(folders: list[dict]) -> list[dict]:
    """Omit agent_url (cursor.com) from Fleet poll payloads — DPI-sensitive."""
    return [{k: item.get(k) for k in FLEET_FOLDER_KEYS} for item in folders]


def fleet_proxy_summary() -> dict:
    status = pool_status()
    active = status.get("active_proxy") or {}
    check = active.get("last_check") or {}
    return {
        "enabled": status.get("enabled"),
        "listening": status.get("listening"),
        "active_proxy_id": status.get("active_proxy_id"),
        "active_proxy": (
            {"id": active.get("id"), "name": active.get("name"), "ok": check.get("ok")}
            if active
            else None
        ),
        "proxy_count": status.get("proxy_count"),
        "subscription_count": status.get("subscription_count"),
    }


def load_pool_safe():
    from proxy_pool import load_pool

    return load_pool()


def public_proxy_entry_safe(entry):
    from proxy_pool import public_proxy_entry

    return public_proxy_entry(entry)


def _normalized_ip_set(values) -> set:
    out = set()
    for value in values:
        try:
            out.add(ipaddress.ip_address(str(value).split("%", 1)[0]))
        except ValueError:
            continue
    return out


def _local_address_set() -> set:
    values = {primary_ip()}
    for name in {socket.gethostname(), "localhost", "127.0.0.1"}:
        try:
            values.update(addr[4][0] for addr in socket.getaddrinfo(name, None))
        except OSError:
            pass
    return _normalized_ip_set(values)


_CADDY_ALLOW_CACHE: dict[str, tuple[float, bool]] = {}
_CADDY_ALLOW_TTL = 300.0


def domain_resolves_here(domain: str) -> bool:
    """True when Caddy may request a certificate for this hostname.

    Restricts On-Demand TLS to the configured public hostname or names whose
    DNS currently points at one of this server's addresses.
    """
    domain = domain.strip().lower().rstrip(".")
    if not domain:
        return False
    configured = str(CFG.get("public_hostname") or "").strip().lower().rstrip(".")
    if configured and domain == configured:
        return True
    now = time.time()
    cached = _CADDY_ALLOW_CACHE.get(domain)
    if cached and now - cached[0] < _CADDY_ALLOW_TTL:
        return cached[1]
    allowed = False
    try:
        addrs = {addr[4][0] for addr in socket.getaddrinfo(domain, None)}
        allowed = bool(_normalized_ip_set(addrs) & _local_address_set())
    except OSError:
        allowed = False
    if len(_CADDY_ALLOW_CACHE) > 512:
        _CADDY_ALLOW_CACHE.clear()
    _CADDY_ALLOW_CACHE[domain] = (now, allowed)
    return allowed


def totp_json(payload: dict) -> Response:
    resp = jsonify(payload)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def serve_password_for_panel() -> str:
    return opencode_panel.serve_password(CFG, read_panel_password())


def opencode_proxy_access():
    if not panel_session_ok():
        return Response("Sign in to AgentControl first, then open OpenCode.", status=401)
    origin = request.headers.get("Origin")
    scheme = {"ws": "http", "wss": "https"}.get(request.scheme, request.scheme)
    if origin and origin != f"{scheme}://{request.host}":
        return Response("OpenCode requires a same-origin request.", status=403)
    return None


def opencode_terminal_proxy_for(terminal: str, prefix: str):
    denied = opencode_proxy_access()
    if denied is not None:
        return denied
    if not re.fullmatch(r"pty_[A-Za-z0-9]+", terminal):
        return Response(status=404)
    status = opencode_panel.proxy_terminal(
        CFG, read_panel_password(), terminal, request.query_string, request.environ, prefix=prefix,
    )
    return Response(status=status)


def _project_total() -> int:
    root = Path(CFG["scan_root"])
    if not root.is_dir():
        return 0
    total = 0
    for entry in root.iterdir():
        if not entry.is_dir() or is_excluded(entry.name) or is_folder_ignored(entry):
            continue
        total += 1
    return total


def _opencode_healthy() -> bool | None:
    try:
        password = opencode_panel.serve_password(CFG, read_panel_password())
        return bool(opencode_panel.is_serve_healthy(CFG, password))
    except Exception:
        return None


def panel_health_snapshot(state: dict | None = None) -> dict:
    state = state if state is not None else reconcile_state()
    running = [name for name, info in state.items() if info.get("pid") and is_running(info["pid"])]
    version = load_version().get("version", "0.0.0")
    update_available = STORE.meta_get("update_available", "0") == "1"
    snapshot = health_mod.collect_health(
        state_dir=STATE_DIR,
        version=version,
        uptime_seconds=time.time() - PROCESS_START,
        workers_running=len(running),
        workers_total=_project_total(),
        opencode_healthy=_opencode_healthy(),
        cert_file=str(CFG.get("tls_cert_file") or ""),
        disk_path="/",
        warn_percent=SETTINGS.disk_warn_percent,
        crit_percent=SETTINGS.disk_crit_percent,
        cert_warn_days=SETTINGS.cert_warn_days,
        cert_crit_days=SETTINGS.cert_crit_days,
        update_available=update_available,
        extra={"hostname": socket.gethostname(), "public_hostname": CFG.get("public_hostname", "")},
    )
    snapshot["workers"] = {
        name: {"running": bool(info.get("pid") and is_running(info["pid"]))}
        for name, info in state.items()
    }
    return snapshot


def check_panel_update() -> dict:
    local = load_version()
    try:
        remote = fetch_github_json("version.json")
    except (URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        return {"local": local, "remote": None, "update_available": False, "error": str(exc)}
    remote_version = str(remote.get("version", "0.0.0"))
    available = version_is_newer(remote_version, local.get("version", "0.0.0"))
    STORE.meta_set("update_available", "1" if available else "0")
    STORE.meta_set("remote_version", remote_version)
    return {"local": local, "remote": remote, "update_available": available}


def monitor_snapshot() -> dict:
    return panel_health_snapshot()


def _merge_channels(current, incoming: dict) -> dict:
    from panel.config import Channel

    merged = {}
    for name in ("telegram", "webhook", "email", "ntfy"):
        existing = current.channels.get(name)
        payload = incoming.get(name)
        if payload is None:
            if existing:
                merged[name] = existing
            continue
        if isinstance(payload, bool):
            merged[name] = Channel(enabled=payload, options=dict(existing.options) if existing else {})
            continue
        options = {str(k): v for k, v in payload.items() if k != "enabled"}
        options.pop("bot_token_set", None)
        options.pop("password_set", None)
        options.pop("secret_set", None)
        options.pop("token_set", None)
        if existing:
            for secret in ("bot_token", "password", "secret", "token"):
                if not options.get(secret) and existing.options.get(secret):
                    options[secret] = existing.options[secret]
        merged[name] = Channel(enabled=bool(payload.get("enabled")), options=options)
    return merged


def _public_health(snapshot: dict) -> dict:
    return {
        "status": snapshot.get("status"),
        "ok": snapshot.get("ok"),
        "version": snapshot.get("version"),
        "uptime_seconds": snapshot.get("uptime_seconds"),
        "checked_at": snapshot.get("checked_at"),
        "workers_running": snapshot.get("workers_running"),
        "workers_total": snapshot.get("workers_total"),
        "checks": [
            {"key": check.get("key"), "status": check.get("status"), "message": check.get("message")}
            for check in snapshot.get("checks", [])
        ],
        "certificate": {
            "found": snapshot.get("certificate", {}).get("found"),
            "days_left": snapshot.get("certificate", {}).get("days_left"),
        },
        "disk": {"percent": snapshot.get("disk", {}).get("percent")},
    }


MONITOR = Monitor(
    store=STORE,
    notifier=NOTIFIER,
    settings=SETTINGS,
    snapshot=monitor_snapshot,
    update_check=check_panel_update,
    logger=lambda message: print(message, flush=True),
)


from panel.routes import auth as _routes_auth  # noqa: E402
from panel.routes import opencode as _routes_opencode  # noqa: E402
from panel.routes import ops as _routes_ops  # noqa: E402
from panel.routes import proxy as _routes_proxy  # noqa: E402
from panel.routes import settings as _routes_settings  # noqa: E402
from panel.routes import system as _routes_system  # noqa: E402
from panel.routes import workers as _routes_workers  # noqa: E402

for _routes in (
    _routes_auth,
    _routes_workers,
    _routes_opencode,
    _routes_proxy,
    _routes_settings,
    _routes_system,
    _routes_ops,
):
    _routes.install(sys.modules[__name__])
    app.register_blueprint(_routes.bp)


def _install_v1_aliases() -> None:
    """Expose every /api/... route under /api/v1/... without duplicating handlers."""
    for rule in list(app.url_map.iter_rules()):
        if not rule.rule.startswith("/api/") or rule.rule.startswith("/api/v1/"):
            continue
        if getattr(rule, "websocket", False):
            continue
        methods = sorted((rule.methods or set()) - {"HEAD", "OPTIONS"})
        app.add_url_rule(
            "/api/v1" + rule.rule[4:],
            endpoint=rule.endpoint,
            view_func=app.view_functions[rule.endpoint],
            methods=methods,
        )


_install_v1_aliases()


if __name__ == "__main__":
    if CONFIG_PROBLEMS:
        raise SystemExit("Invalid configuration:\n- " + "\n- ".join(CONFIG_PROBLEMS))
    MONITOR.start()
    ensure_host_panel()
    ensure_state_dir()
    internal_port = int(CFG.get("internal_port") or 0)
    bind_host = str(CFG.get("bind_host") or "").strip()
    if not bind_host:
        bind_host = "127.0.0.1" if internal_port else "0.0.0.0"
    app.run(host=bind_host, port=internal_port or int(CFG["port"]), debug=False)
