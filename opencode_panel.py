"""Shared OpenCode serve process, secrets sync, and /oc reverse proxy helpers."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import threading
import time
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen

import simple_websocket
import websocket

_SERVE_LOCK = threading.Lock()
_SERVE_PROC: subprocess.Popen | None = None
_INSTALL_LOCK = threading.Lock()
_INSTALL_PROC: subprocess.Popen | None = None

DEFAULT_BIN_CANDIDATES = (
    Path("/root/.opencode/bin/opencode"),
    Path.home() / ".opencode/bin/opencode",
    Path("/usr/local/bin/opencode"),
)

LAUNCH_TOKEN_TTL = 180
_LAUNCH_LOCK = threading.Lock()
_LAUNCH_TOKENS: dict[str, tuple[str, float]] = {}
_VERSION_CACHE: dict[tuple[str, float], str] = {}


def pinned_version() -> str:
    path = Path(__file__).resolve().parent / "scripts/opencode-versions.sh"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("opencode_version="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return "2.4.1"


def binary_version(path: str | Path | None) -> str:
    if not path:
        return ""
    try:
        stat = os.stat(path)
    except OSError:
        return ""
    key = (str(path), stat.st_mtime)
    cached = _VERSION_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        proc = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        version = (proc.stdout or "").strip().splitlines()
    except (OSError, subprocess.SubprocessError):
        version = []
    result = version[-1].strip().removeprefix("opencode v") if version else ""
    if len(_VERSION_CACHE) > 16:
        _VERSION_CACHE.clear()
    _VERSION_CACHE[key] = result
    return result


def web_build_version() -> str:
    try:
        return (opencode_web_root() / "OPENCODE_VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def create_launch_path(directory: str) -> str:
    """Create a single-use link that signs the browser into the panel and opens a project."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _LAUNCH_LOCK:
        for key, (_, expires) in list(_LAUNCH_TOKENS.items()):
            if expires < now:
                _LAUNCH_TOKENS.pop(key, None)
        _LAUNCH_TOKENS[token] = (directory, now + LAUNCH_TOKEN_TTL)
    return "/api/fleet/sso?" + urlencode({"token": token})


def consume_launch_token(token: str) -> str | None:
    if not token:
        return None
    with _LAUNCH_LOCK:
        item = _LAUNCH_TOKENS.pop(token, None)
    if not item:
        return None
    directory, expires = item
    if expires < time.time():
        return None
    return directory


def secrets_file(cfg: dict) -> Path:
    return Path(cfg.get("opencode_secrets_file", "/etc/agentcontrol/opencode-secrets.json"))


def serve_log_file(state_dir: Path) -> Path:
    return state_dir / "opencode-serve.log"


def serve_state_file(state_dir: Path) -> Path:
    return state_dir / "opencode-serve.json"


def find_opencode_bin(cfg: dict) -> str | None:
    custom = (cfg.get("opencode_bin") or "").strip()
    custom_executable = custom and Path(custom).is_file() and os.access(custom, os.X_OK)
    if custom_executable and binary_version(custom) == pinned_version():
        return custom
    managed = Path(cfg.get("state_dir", "/var/lib/agentcontrol")) / "opencode-bin/opencode"
    if managed.is_file() and os.access(managed, os.X_OK):
        return str(managed)
    if custom_executable:
        return custom
    found = shutil.which("opencode")
    if found:
        return found
    for candidate in DEFAULT_BIN_CANDIDATES:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def opencode_web_root() -> Path:
    return Path(__file__).resolve().parent / "opencode-web"


def web_install_required(cfg: dict) -> bool:
    binary = find_opencode_bin(cfg)
    if not binary or not (opencode_web_root() / "index.html").is_file():
        return True
    expected = pinned_version()
    return binary_version(binary) != expected or web_build_version() != expected


def install_status(cfg: dict, state_dir: Path) -> dict[str, Any]:
    # Polling reaps the installer and lets any authenticated tab rejoin it.
    with _INSTALL_LOCK:
        exit_code = _INSTALL_PROC.poll() if _INSTALL_PROC else None
        if _INSTALL_PROC and exit_code is None:
            return {"status": "installing"}
        if not web_install_required(cfg):
            return {"status": "ready", "version": pinned_version()}
        if _INSTALL_PROC:
            error = f"OpenCode installation failed. See {state_dir / 'opencode-install.log'}."
            return {"status": "failed", "error": error}
        return {"status": "missing", "version": pinned_version()}


def start_install(cfg: dict, state_dir: Path) -> tuple[dict[str, Any], int]:
    global _INSTALL_PROC
    with _INSTALL_LOCK:
        if _INSTALL_PROC and _INSTALL_PROC.poll() is None:
            return {"status": "installing"}, 202
        if not web_install_required(cfg):
            return {"status": "ready"}, 200
        script = opencode_web_root().parent / "scripts/install-opencode.sh"
        log_path = state_dir / "opencode-install.log"
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            with log_path.open("w", encoding="utf-8") as log:
                _INSTALL_PROC = subprocess.Popen(
                    ["timeout", "--kill-after=10s", "20m", "bash", str(script)],
                    env={**os.environ, "STATE_DIR": str(state_dir), "OPENCODE_BIN": find_opencode_bin(cfg) or ""},
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                )
        except OSError:
            return {"status": "failed", "error": "Could not start the OpenCode installer."}, 500
        return {"status": "installing"}, 202


def load_secrets(cfg: dict) -> dict[str, str]:
    path = secrets_file(cfg)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key in (
        "anthropic_api_key",
        "openai_api_key",
        "opencode_go_key",
        "default_model",
        "remote_public_url",
    ):
        value = raw.get(key)
        if value is not None and str(value).strip():
            out[key] = str(value).strip()
    if raw.get("remote_enabled"):
        out["remote_enabled"] = "true"
    return out


def save_secrets(cfg: dict, data: dict[str, Any]) -> dict[str, str]:
    path = secrets_file(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = load_secrets(cfg)
    for key in ("anthropic_api_key", "openai_api_key", "opencode_go_key", "default_model"):
        if key not in data:
            continue
        value = str(data.get(key, "")).strip()
        if value:
            current[key] = value
        elif data.get(f"clear_{key}"):
            current.pop(key, None)
    if "remote_enabled" in data:
        if data.get("remote_enabled"):
            current["remote_enabled"] = "true"
        else:
            current.pop("remote_enabled", None)
    if "remote_public_url" in data:
        url = str(data.get("remote_public_url", "")).strip()
        if url:
            current["remote_public_url"] = url
        elif data.get("clear_remote_public_url"):
            current.pop("remote_public_url", None)
    payload = {**current}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    apply_provider_config(cfg, payload)
    return public_secrets(payload)


def public_secrets(stored: dict[str, str] | None = None) -> dict[str, Any]:
    stored = stored or {}
    return {
        "anthropic_set": bool(stored.get("anthropic_api_key")),
        "openai_set": bool(stored.get("openai_api_key")),
        "opencode_go_set": bool(stored.get("opencode_go_key")),
        "default_model": stored.get("default_model", ""),
        "remote_enabled": stored.get("remote_enabled") == "true",
        "remote_public_url": stored.get("remote_public_url", ""),
    }


def provider_env_file(cfg: dict) -> Path:
    return Path(cfg.get("opencode_env_file", "/etc/agentcontrol/opencode-providers.env"))


def global_config_file(cfg: dict) -> Path:
    custom = (cfg.get("opencode_config_file") or "").strip()
    if custom:
        return Path(custom)
    return Path(os.environ.get("OPENCODE_CONFIG", str(Path.home() / ".config/opencode/opencode.json")))


def apply_provider_config(cfg: dict, secrets: dict[str, str]) -> None:
    env_path = provider_env_file(cfg)
    lines = ["# Generated by AgentControl — do not edit manually"]
    if secrets.get("anthropic_api_key"):
        lines.append(f"ANTHROPIC_API_KEY={secrets['anthropic_api_key']}")
    if secrets.get("openai_api_key"):
        lines.append(f"OPENAI_API_KEY={secrets['openai_api_key']}")
    if secrets.get("opencode_go_key"):
        lines.append(f"OPENCODE_API_KEY={secrets['opencode_go_key']}")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        env_path.chmod(0o600)
    except OSError:
        pass

    config_path = global_config_file(cfg)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    body: dict[str, Any] = {}
    if config_path.is_file():
        try:
            existing = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                body = existing
        except (json.JSONDecodeError, OSError):
            body = {}
    providers = body.get("providers")
    if not isinstance(providers, dict):
        providers = {}
    if secrets.get("anthropic_api_key"):
        providers["anthropic"] = {
            "settings": {"apiKey": "{env:ANTHROPIC_API_KEY}"},
        }
    if secrets.get("openai_api_key"):
        providers["openai"] = {
            "settings": {"apiKey": "{env:OPENAI_API_KEY}"},
        }
    if secrets.get("opencode_go_key"):
        providers["opencode"] = {
            "settings": {"apiKey": "{env:OPENCODE_API_KEY}"},
        }
    body["$schema"] = "https://opencode.ai/config.json"
    body["providers"] = providers
    permissions = body.get("permissions")
    if not isinstance(permissions, list) or not permissions:
        # Shared server agents need full local access: every tool, edits, and paths
        # outside the project directory without interactive permission prompts.
        body["permissions"] = [{"action": "*", "resource": "*", "effect": "allow"}]
    body.setdefault("update", "notify")
    model = (secrets.get("default_model") or "").strip()
    if model:
        body["model"] = model
    config_path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    try:
        config_path.chmod(0o600)
    except OSError:
        pass


def serve_password(cfg: dict, panel_password: str | None) -> str:
    path = Path(cfg.get("opencode_serve_password_file", "/etc/agentcontrol/opencode-serve-password"))
    if path.is_file():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    candidate = (panel_password or "").strip() or os.urandom(24).hex()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(candidate + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return candidate


def opencode_port(cfg: dict) -> int:
    try:
        return int(cfg.get("opencode_port", 4096))
    except (TypeError, ValueError):
        return 4096


def serve_base_url(cfg: dict) -> str:
    host = cfg.get("opencode_hostname", "127.0.0.1")
    return f"http://{host}:{opencode_port(cfg)}"


def is_serve_healthy(cfg: dict, password: str) -> bool:
    # OpenCode 2.x exposes server identity under /api/info; 1.x used /global/health.
    for path, check in (
        ("/api/info", lambda body: isinstance(body, dict)),
        ("/global/health", lambda body: isinstance(body, dict) and body.get("healthy") is True),
    ):
        try:
            req = Request(
                f"{serve_base_url(cfg)}{path}",
                headers={"Authorization": _basic_auth_header(password)},
            )
            with urlopen(req, timeout=3) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                if check(body):
                    return True
        except (HTTPError, URLError, OSError, json.JSONDecodeError, ValueError):
            continue
    return False


def _basic_auth_header(password: str) -> str:
    import base64

    token = base64.b64encode(f"opencode:{password}".encode()).decode("ascii")
    return f"Basic {token}"


def load_serve_state(state_dir: Path) -> dict:
    path = serve_state_file(state_dir)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_serve_state(state_dir: Path, payload: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    serve_state_file(state_dir).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _is_pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def build_serve_env(cfg: dict, password: str) -> dict[str, str]:
    env = os.environ.copy()
    env["OPENCODE_SERVER_PASSWORD"] = password
    env["OPENCODE_PASSWORD"] = password
    env_path = provider_env_file(cfg)
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    bin_dir = ""
    opencode_bin = find_opencode_bin(cfg)
    if opencode_bin:
        bin_dir = str(Path(opencode_bin).parent)
    path = env.get("PATH", "")
    if bin_dir:
        env["PATH"] = f"{bin_dir}:{path}"
    env["BROWSER"] = "/bin/true"
    env["HOME"] = str(Path.home())
    return env


def kill_serve_process(state_dir: Path) -> None:
    global _SERVE_PROC
    with _SERVE_LOCK:
        state = load_serve_state(state_dir)
        pid = int(state.get("pid") or 0)
        if pid and _is_pid_running(pid):
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        if _SERVE_PROC and _SERVE_PROC.poll() is None:
            try:
                os.killpg(os.getpgid(_SERVE_PROC.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
        _SERVE_PROC = None
        serve_state_file(state_dir).unlink(missing_ok=True)


def start_serve_process(
    cfg: dict, state_dir: Path, password: str, *, force: bool = False
) -> dict[str, Any]:
    global _SERVE_PROC
    if force:
        kill_serve_process(state_dir)
    opencode_bin = find_opencode_bin(cfg)
    if not opencode_bin:
        return {"ok": False, "error": "opencode binary not found — install from https://opencode.ai/install"}

    port = opencode_port(cfg)
    hostname = str(cfg.get("opencode_hostname", "127.0.0.1"))
    secrets = load_secrets(cfg)
    apply_provider_config(cfg, secrets)

    log_path = serve_log_file(state_dir)
    cmd = [
        opencode_bin,
        "serve",
        "--hostname",
        hostname,
        "--port",
        str(port),
        "--print-logs",
    ]

    with _SERVE_LOCK:
        state = load_serve_state(state_dir)
        pid = int(state.get("pid") or 0)
        if pid and _is_pid_running(pid):
            return {"ok": True, "status": "already_running", "pid": pid}

        if _SERVE_PROC and _SERVE_PROC.poll() is None:
            return {"ok": True, "status": "already_running", "pid": _SERVE_PROC.pid}

        try:
            log_fp = open(log_path, "ab")
            proc = subprocess.Popen(
                cmd,
                stdout=log_fp,
                stderr=subprocess.STDOUT,
                env=build_serve_env(cfg, password),
                start_new_session=True,
            )
        except OSError as exc:
            return {"ok": False, "error": str(exc)}

        _SERVE_PROC = proc
        save_serve_state(
            state_dir,
            {
                "pid": proc.pid,
                "port": port,
                "started_at": time.time(),
                "log_file": str(log_path),
                "binary": opencode_bin,
                "version": binary_version(opencode_bin),
            },
        )

    deadline = time.time() + 60
    while time.time() < deadline:
        if is_serve_healthy(cfg, password):
            return {"ok": True, "status": "started", "pid": proc.pid}
        if proc.poll() is not None:
            tail = ""
            try:
                tail = log_path.read_text(encoding="utf-8", errors="ignore")[-2000:]
            except OSError:
                pass
            return {"ok": False, "error": "opencode serve exited", "log_tail": tail}
        time.sleep(0.4)

    return {"ok": False, "error": "opencode serve did not become healthy in time"}


def ensure_serve(cfg: dict, state_dir: Path, panel_password: str | None) -> dict[str, Any]:
    password = serve_password(cfg, panel_password)
    if is_serve_healthy(cfg, password):
        state = load_serve_state(state_dir)
        current_bin = find_opencode_bin(cfg)
        current_version = binary_version(current_bin)
        if state.get("binary") != current_bin or state.get("version") != current_version:
            kill_serve_process(state_dir)
        else:
            return {"ok": True, "healthy": True, "pid": state.get("pid"), "port": opencode_port(cfg)}
    return start_serve_process(cfg, state_dir, password)


def opencode_public_path(directory: str) -> str:
    query = urlencode({"directory": directory})
    return f"/oc/?{query}"


def opencode_status(cfg: dict, state_dir: Path, panel_password: str | None) -> dict[str, Any]:
    password = serve_password(cfg, panel_password)
    healthy = is_serve_healthy(cfg, password)
    state = load_serve_state(state_dir)
    secrets = public_secrets(load_secrets(cfg))
    binary = find_opencode_bin(cfg)
    return {
        "installed": bool(binary),
        "version": binary_version(binary) if binary else "",
        "pinned_version": pinned_version(),
        "web_version": web_build_version(),
        "install_required": web_install_required(cfg),
        "healthy": healthy,
        "port": opencode_port(cfg),
        "pid": state.get("pid") if healthy else None,
        "proxy_prefix": "/oc",
        "secrets": secrets,
        "log_file": str(serve_log_file(state_dir)),
    }


_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "proxy-connection", "te", "trailer", "trailers", "transfer-encoding", "upgrade",
}


def _upstream_target(subpath: str, query: bytes) -> str:
    path = "/" + quote(subpath.lstrip("/"), safe="/")
    return path + ("?" + query.decode("ascii") if query else "")


def proxy_request(
    cfg: dict,
    panel_password: str | None,
    subpath: str,
    method: str,
    query_string: bytes,
    headers: dict[str, str],
    body: bytes | None,
):
    origin = serve_base_url(cfg)
    endpoint = urlsplit(origin)
    oauth_callback = method == "POST" and subpath.endswith("/oauth/callback")
    upstream = HTTPConnection(endpoint.hostname, endpoint.port, timeout=600 if oauth_callback else 120)
    target = _upstream_target(subpath, query_string)
    connection_tokens = set(headers.get("Connection", "").lower().replace(" ", "").split(","))
    skip = _HOP_HEADERS | connection_tokens | {
        "host", "content-length", "accept-encoding", "cookie", "authorization",
        "origin", "referer", "x-agentcontrol-auth",
    }
    forwarded = {
        key: value
        for key, value in headers.items()
        if key.lower() not in skip and not key.lower().startswith(("forwarded", "x-forwarded-"))
    }
    forwarded["Authorization"] = _basic_auth_header(serve_password(cfg, panel_password))
    forwarded["Accept-Encoding"] = "identity"
    if "Origin" in headers:
        forwarded["Origin"] = origin
    try:
        upstream.request(method, target, body=body, headers=forwarded)
        response = upstream.getresponse()
    except (OSError, HTTPException):
        upstream.close()
        return 502, [("Content-Type", "text/plain")], b"OpenCode upstream unavailable", None

    pinned_headers = {"cache-control", "set-cookie", "www-authenticate", "location"}
    response_connections = set(response.headers.get("Connection", "").lower().replace(" ", "").split(","))
    out = [
        (key, value) for key, value in response.getheaders()
        if key.lower() not in _HOP_HEADERS | pinned_headers | response_connections
        and not key.lower().startswith("access-control-")
    ]
    out.append(("Cache-Control", "no-store"))
    location = response.headers.get("Location")
    if location:
        destination = urlsplit(urljoin(origin + target, location))
        if (destination.scheme, destination.netloc) == (endpoint.scheme, endpoint.netloc):
            location = "/oc" + destination._replace(scheme="", netloc="").geturl()
        elif destination.scheme != "https" or not destination.hostname:
            upstream.close()
            return 502, [("Content-Type", "text/plain")], b"Invalid OpenCode redirect", None
        out.append(("Location", location))
    if response.headers.get("Content-Type", "").partition(";")[0].strip() == "text/event-stream":
        out.append(("X-Accel-Buffering", "no"))

    def stream():
        try:
            while chunk := response.read1(16 * 1024):
                yield chunk
        finally:
            upstream.close()

    return response.status, out, stream(), upstream.close


def proxy_terminal(
    cfg: dict,
    panel_password: str | None,
    terminal: str,
    query: bytes,
    environ: dict,
    prefix: str = "pty",
) -> int:
    url = serve_base_url(cfg).replace("http://", "ws://", 1)
    target = _upstream_target(f"{prefix}/{terminal}/connect", query)
    password = serve_password(cfg, panel_password)
    try:
        upstream = websocket.create_connection(
            url + target,
            header={"Authorization": _basic_auth_header(password)},
            http_no_proxy=["127.0.0.1", "localhost", "::1"],
            redirect_limit=0,
            timeout=5,
        )
    except (OSError, websocket.WebSocketException):
        return 502
    try:
        downstream = simple_websocket.Server.accept(environ, ping_interval=20, max_message_size=1024 * 1024)
        upstream.settimeout(None)

        def relay_output():
            try:
                while True:
                    opcode, payload = upstream.recv_data()
                    if opcode == websocket.ABNF.OPCODE_CLOSE:
                        break
                    downstream.send(payload.decode("utf-8") if opcode == websocket.ABNF.OPCODE_TEXT else payload)
            except (OSError, UnicodeError, websocket.WebSocketException, simple_websocket.ConnectionClosed):
                pass
            finally:
                try:
                    downstream.close()
                except (OSError, simple_websocket.ConnectionClosed):
                    pass

        reader = threading.Thread(target=relay_output, daemon=True)
        reader.start()
        try:
            while downstream.connected:
                payload = downstream.receive(timeout=1)
                if payload is not None:
                    opcode = websocket.ABNF.OPCODE_TEXT if isinstance(payload, str) else websocket.ABNF.OPCODE_BINARY
                    upstream.send(payload, opcode=opcode)
        except (OSError, websocket.WebSocketException, simple_websocket.ConnectionClosed):
            pass
    finally:
        upstream.shutdown()
    return 204


def try_refresh_remote_url(cfg: dict, state_dir: Path, panel_password: str | None) -> dict[str, Any]:
    """Best-effort: read experimental console switch URL when OpenCode Go is configured."""
    ensure = ensure_serve(cfg, state_dir, panel_password)
    if not ensure.get("ok"):
        return ensure
    password = serve_password(cfg, panel_password)
    url = f"{serve_base_url(cfg)}/experimental/console"
    try:
        req = Request(url, headers={"Authorization": _basic_auth_header(password)})
        with urlopen(req, timeout=8) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError, OSError, json.JSONDecodeError):
        return {"ok": False, "error": "console endpoint unavailable — use panel URL /oc/ for remote UI"}

    remote_url = ""
    if isinstance(body, dict):
        remote_url = str(body.get("url") or body.get("publicUrl") or "").strip()
    if remote_url:
        save_secrets(cfg, {"remote_public_url": remote_url})
    return {"ok": True, "remote_public_url": remote_url}


def reconcile_serve_state(state_dir: Path) -> None:
    state = load_serve_state(state_dir)
    pid = int(state.get("pid") or 0)
    if pid and not _is_pid_running(pid):
        serve_state_file(state_dir).unlink(missing_ok=True)


def stop_orphan_serve_on_shutdown() -> None:
    global _SERVE_PROC
    if _SERVE_PROC and _SERVE_PROC.poll() is None:
        try:
            os.killpg(os.getpgid(_SERVE_PROC.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
