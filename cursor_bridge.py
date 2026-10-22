"""Cursor api2 edge bridge (TLS stream proxy) — settings in /etc/agentcontrol/.env only."""

from __future__ import annotations

import os
import re
import shutil
import ssl
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

BRIDGE_ENV_FILE = Path(os.environ.get("CURSOR_BRIDGE_ENV_FILE", "/etc/agentcontrol/.env"))
HOSTS_FILE = Path("/etc/hosts")
HOSTS_MARKER = "agentcontrol-cursor-bridge"
DEFAULT_TLS_HOST = "api2.cursor.sh"
DEFAULT_PORT = 33443


@dataclass
class BridgeSettings:
    enabled: bool = False
    edge_ip: str = ""
    port: int = DEFAULT_PORT
    tls_host: str = DEFAULT_TLS_HOST
    endpoint_override: str = ""

    @property
    def endpoint(self) -> str | None:
        if self.endpoint_override.strip():
            return self.endpoint_override.strip().rstrip("/")
        if not self.enabled or not self.edge_ip.strip():
            return None
        host = (self.tls_host or DEFAULT_TLS_HOST).strip()
        return f"https://{host}:{int(self.port)}"


def parse_dotenv(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def load_dotenv_file(path: Path | None = None) -> dict[str, str]:
    path = path or BRIDGE_ENV_FILE
    if not path.is_file():
        return {}
    try:
        return parse_dotenv(path.read_text(encoding="utf-8"))
    except OSError:
        return {}


def write_bridge_env(values: dict[str, str]) -> None:
    path = BRIDGE_ENV_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_dotenv_file(path)
    existing.update({k: v for k, v in values.items() if v is not None})
    lines = [
        "# Cursor edge bridge — do not commit (server-local secrets / IPs)",
        f"CURSOR_BRIDGE_ENABLED={existing.get('CURSOR_BRIDGE_ENABLED', 'false')}",
    ]
    if existing.get("CURSOR_API_ENDPOINT"):
        lines.append(f"CURSOR_API_ENDPOINT={existing['CURSOR_API_ENDPOINT']}")
    if existing.get("CURSOR_BRIDGE_EDGE_IP"):
        lines.append(f"CURSOR_BRIDGE_EDGE_IP={existing['CURSOR_BRIDGE_EDGE_IP']}")
    if existing.get("CURSOR_BRIDGE_PORT"):
        lines.append(f"CURSOR_BRIDGE_PORT={existing['CURSOR_BRIDGE_PORT']}")
    if existing.get("CURSOR_BRIDGE_TLS_HOST"):
        lines.append(f"CURSOR_BRIDGE_TLS_HOST={existing['CURSOR_BRIDGE_TLS_HOST']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    sync_os_environ(existing)


def sync_os_environ(dotenv: dict[str, str]) -> None:
    for key, value in dotenv.items():
        os.environ[key] = value


def load_bridge_settings() -> BridgeSettings:
    dotenv = load_dotenv_file()
    sync_os_environ(dotenv)
    enabled = str(dotenv.get("CURSOR_BRIDGE_ENABLED", "false")).lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    endpoint_override = dotenv.get("CURSOR_API_ENDPOINT", "").strip()
    if endpoint_override and not enabled:
        enabled = True
    port_raw = dotenv.get("CURSOR_BRIDGE_PORT", str(DEFAULT_PORT))
    try:
        port = int(port_raw)
    except ValueError:
        port = DEFAULT_PORT
    return BridgeSettings(
        enabled=enabled,
        edge_ip=dotenv.get("CURSOR_BRIDGE_EDGE_IP", "").strip(),
        port=port,
        tls_host=dotenv.get("CURSOR_BRIDGE_TLS_HOST", DEFAULT_TLS_HOST).strip()
        or DEFAULT_TLS_HOST,
        endpoint_override=endpoint_override,
    )


def public_bridge_settings() -> dict[str, object]:
    s = load_bridge_settings()
    endpoint = s.endpoint
    return {
        "enabled": s.enabled,
        "edge_ip": s.edge_ip,
        "port": s.port,
        "tls_host": s.tls_host,
        "endpoint": endpoint,
        "env_file": str(BRIDGE_ENV_FILE),
        "hosts_configured": hosts_entry_present(s.edge_ip, s.tls_host),
    }


def hosts_entry_present(edge_ip: str, tls_host: str) -> bool:
    if not edge_ip or not HOSTS_FILE.is_file():
        return False
    try:
        text = HOSTS_FILE.read_text(encoding="utf-8")
    except OSError:
        return False
    pattern = rf"^{re.escape(edge_ip)}\s+.*{re.escape(tls_host)}.*{HOSTS_MARKER}"
    return bool(re.search(pattern, text, re.MULTILINE))


def apply_bridge_hosts(settings: BridgeSettings | None = None) -> tuple[bool, str]:
    settings = settings or load_bridge_settings()
    if not settings.enabled or not settings.edge_ip:
        remove_bridge_hosts()
        return True, "hosts not required"
    line = (
        f"{settings.edge_ip} {settings.tls_host} api2direct.cursor.sh # {HOSTS_MARKER}\n"
    )
    try:
        text = HOSTS_FILE.read_text(encoding="utf-8") if HOSTS_FILE.is_file() else ""
        lines = [ln for ln in text.splitlines() if HOSTS_MARKER not in ln]
        lines.append(line.rstrip())
        HOSTS_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return True, "hosts updated"
    except OSError as exc:
        return False, str(exc)


def remove_bridge_hosts() -> None:
    if not HOSTS_FILE.is_file():
        return
    try:
        text = HOSTS_FILE.read_text(encoding="utf-8")
        lines = [ln for ln in text.splitlines() if HOSTS_MARKER not in ln]
        HOSTS_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    except OSError:
        pass


def apply_cursor_bridge_env(env: dict[str, str]) -> dict[str, str]:
    settings = load_bridge_settings()
    if settings.enabled:
        apply_bridge_hosts(settings)
        endpoint = settings.endpoint
        if endpoint:
            env["CURSOR_API_ENDPOINT"] = endpoint
    else:
        env.pop("CURSOR_API_ENDPOINT", None)
    return env


def save_bridge_from_panel(data: dict[str, object]) -> BridgeSettings:
    enabled = bool(data.get("enabled"))
    edge_ip = str(data.get("edge_ip", "")).strip()
    port = int(data.get("port", DEFAULT_PORT) or DEFAULT_PORT)
    tls_host = str(data.get("tls_host", DEFAULT_TLS_HOST)).strip() or DEFAULT_TLS_HOST
    endpoint_override = str(data.get("endpoint", "")).strip()

    values: dict[str, str] = {
        "CURSOR_BRIDGE_ENABLED": "true" if enabled else "false",
        "CURSOR_BRIDGE_EDGE_IP": edge_ip,
        "CURSOR_BRIDGE_PORT": str(port),
        "CURSOR_BRIDGE_TLS_HOST": tls_host,
    }
    if not enabled:
        values["CURSOR_API_ENDPOINT"] = ""
        values["CURSOR_BRIDGE_EDGE_IP"] = ""
    elif endpoint_override:
        ep = endpoint_override.rstrip("/")
        # Stream bridge is TLS to api2 hostname — not plain HTTP to the edge IP.
        if ep.lower().startswith("http://") and edge_ip and edge_ip in ep:
            ep = f"https://{tls_host}:{port}"
        values["CURSOR_API_ENDPOINT"] = ep
    elif edge_ip:
        values["CURSOR_API_ENDPOINT"] = f"https://{tls_host}:{port}"
    else:
        values["CURSOR_API_ENDPOINT"] = ""

    write_bridge_env(values)
    settings = load_bridge_settings()
    if settings.enabled:
        apply_bridge_hosts(settings)
    else:
        remove_bridge_hosts()
    return settings


def tcp_reachable(host: str, port: int, timeout: float = 8.0) -> tuple[bool, str]:
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, int(port)))
        return True, "tcp ok"
    except OSError as exc:
        return False, str(exc)
    finally:
        sock.close()


def test_cursor_bridge(api_key: str | None = None) -> dict[str, object]:
    settings = load_bridge_settings()
    endpoint = settings.endpoint
    if not settings.enabled or not endpoint:
        return {"ok": False, "error": "Edge bridge is disabled or incomplete (.env)"}

    if endpoint.lower().startswith("http://"):
        endpoint = f"https://{settings.tls_host}:{settings.port}"
        result_hint = "normalized_endpoint"
    else:
        result_hint = None

    hosts_ok, hosts_msg = apply_bridge_hosts(settings)
    result: dict[str, object] = {
        "endpoint": endpoint,
        "edge_ip": settings.edge_ip,
        "edge_port": settings.port,
        "hosts_ok": hosts_ok,
        "hosts_msg": hosts_msg,
    }
    if result_hint:
        result["hint"] = (
            "Edge proxy uses TLS stream — use https://api2.cursor.sh:PORT "
            "(not http://EDGE_IP). Endpoint was normalized for this test."
        )

    if settings.edge_ip:
        tcp_ok, tcp_err = tcp_reachable(settings.edge_ip, settings.port)
        result["tcp_ok"] = tcp_ok
        if not tcp_ok:
            result.update(
                {
                    "ok": False,
                    "error": (
                        f"Cannot reach edge server {settings.edge_ip}:{settings.port} "
                        f"({tcp_err}). Wrong IP after VPS move, or firewall blocks Iran."
                    ),
                }
            )
            return result

    probe_url = endpoint if endpoint.endswith("/") else endpoint + "/"
    started = time.time()
    try:
        ctx = ssl.create_default_context()
        req = Request(probe_url, method="GET")
        with urlopen(req, timeout=25, context=ctx) as response:
            body = response.read(512)
            elapsed_ms = int((time.time() - started) * 1000)
            text = body.decode("utf-8", errors="replace")
            result.update(
                {
                    "ok": response.status == 200 and "Cursor" in text,
                    "http_status": response.status,
                    "elapsed_ms": elapsed_ms,
                    "body_preview": text[:120],
                }
            )
    except URLError as exc:
        result.update({"ok": False, "error": str(exc.reason if hasattr(exc, "reason") else exc)})
    except OSError as exc:
        result.update({"ok": False, "error": str(exc)})

    if api_key and result.get("ok"):
        agent_bin = os.environ.get("AGENT_BIN", "")
        for candidate in (agent_bin, "/root/.local/bin/agent", "agent"):
            if not candidate:
                continue
            if Path(candidate).is_file() or shutil.which(candidate):
                agent_bin = candidate
                break
        else:
            agent_bin = ""
        if agent_bin:
            env = os.environ.copy()
            env["CURSOR_API_KEY"] = api_key
            env["PATH"] = "/root/.local/bin:" + env.get("PATH", "")
            apply_cursor_bridge_env(env)
            check_dir = Path("/tmp/agentcontrol-bridge-test")
            check_dir.mkdir(parents=True, exist_ok=True)
            proc = subprocess.Popen(
                [
                    agent_bin,
                    "worker",
                    "--worker-dir",
                    str(check_dir),
                    "--data-dir",
                    str(check_dir / "data"),
                    "--name",
                    "bridge-panel-test",
                    "start",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
            )
            auth_ok = False
            detail = ""
            deadline = time.time() + 22
            try:
                assert proc.stdout is not None
                while time.time() < deadline:
                    line = proc.stdout.readline()
                    if not line:
                        if proc.poll() is not None:
                            break
                        continue
                    detail += line
                    if "authenticated with api key" in line.lower():
                        auth_ok = True
                        break
                    if "api key is invalid" in line.lower():
                        auth_ok = False
                        break
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            result["worker_auth_ok"] = auth_ok
            if not auth_ok:
                result["ok"] = False
                result["worker_auth_detail"] = detail[-400:]

    return result
