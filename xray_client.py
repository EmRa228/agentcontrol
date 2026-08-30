"""Manage xray client (VLESS+Reality outbound + local HTTP inbound) for AgentControl."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

import yaml

DEFAULT_XRAY_CONFIG = Path("/usr/local/etc/xray/config.json")
DEFAULT_CLIENT_FILE = Path("/etc/agentcontrol/xray-client.yaml")
INBOUND_TAG = "agentcontrol-http-in"
OUTBOUND_TAG = "reality-out"
DEFAULT_PROXY_PORT = 30229
DEFAULT_PROXY_LISTEN = "127.0.0.1"


def _paths() -> tuple[Path, Path]:
    config_path = Path(os.environ.get("XRAY_CONFIG", str(DEFAULT_XRAY_CONFIG)))
    client_file = Path(os.environ.get("XRAY_CLIENT_FILE", str(DEFAULT_CLIENT_FILE)))
    return config_path, client_file


def default_client_settings() -> dict[str, Any]:
    return {
        "enabled": True,
        "config_path": str(DEFAULT_XRAY_CONFIG),
        "proxy_listen": DEFAULT_PROXY_LISTEN,
        "proxy_port": DEFAULT_PROXY_PORT,
        "outbound_tag": OUTBOUND_TAG,
        "protocol": "vless",
        "address": "",
        "port": 443,
        "uuid": "",
        "flow": "xtls-rprx-vision",
        "server_name": "",
        "fingerprint": "chrome",
        "public_key": "",
        "short_id": "",
        "spider_x": "/",
    }


def _merge_settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    settings = default_client_settings()
    if raw:
        settings.update({k: v for k, v in raw.items() if v is not None})
    settings["proxy_port"] = int(settings.get("proxy_port") or DEFAULT_PROXY_PORT)
    settings["port"] = int(settings.get("port") or 443)
    settings["enabled"] = bool(settings.get("enabled", True))
    return settings


def load_client_settings() -> dict[str, Any]:
    _, client_file = _paths()
    if client_file.is_file():
        with open(client_file, encoding="utf-8") as f:
            return _merge_settings(yaml.safe_load(f) or {})
    imported = import_from_xray_config()
    if imported:
        return imported
    return default_client_settings()


def save_client_settings(settings: dict[str, Any]) -> dict[str, Any]:
    _, client_file = _paths()
    merged = _merge_settings(settings)
    client_file.parent.mkdir(parents=True, exist_ok=True)
    with open(client_file, "w", encoding="utf-8") as f:
        yaml.safe_dump(merged, f, default_flow_style=False, sort_keys=False)
    os.chmod(client_file, 0o600)
    return merged


def import_from_xray_config() -> dict[str, Any] | None:
    config_path, _ = _paths()
    if not config_path.is_file():
        return None
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    settings = default_client_settings()
    settings["config_path"] = str(config_path)

    for inbound in config.get("inbounds", []):
        if inbound.get("tag") == INBOUND_TAG:
            settings["proxy_listen"] = inbound.get("listen", DEFAULT_PROXY_LISTEN)
            settings["proxy_port"] = int(inbound.get("port", DEFAULT_PROXY_PORT))
            break

    outbound = None
    for item in config.get("outbounds", []):
        if item.get("tag") == OUTBOUND_TAG:
            outbound = item
            break
    if not outbound and config.get("outbounds"):
        outbound = config["outbounds"][0]
        settings["outbound_tag"] = str(outbound.get("tag") or OUTBOUND_TAG)

    if not outbound or outbound.get("protocol") != "vless":
        return settings if settings.get("address") else None

    ob_settings = outbound.get("settings") or {}
    settings["address"] = str(ob_settings.get("address") or "")
    settings["port"] = int(ob_settings.get("port") or 443)
    settings["uuid"] = str(ob_settings.get("id") or "")
    settings["flow"] = str(ob_settings.get("flow") or "xtls-rprx-vision")

    stream = outbound.get("streamSettings") or {}
    reality = stream.get("realitySettings") or {}
    settings["server_name"] = str(reality.get("serverName") or "")
    settings["fingerprint"] = str(reality.get("fingerprint") or "chrome")
    settings["public_key"] = str(reality.get("password") or "")
    settings["short_id"] = str(reality.get("shortId") or "")
    settings["spider_x"] = str(reality.get("spiderX") or "/")
    return settings


def _backup_config(config_path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = config_path.with_name(f"{config_path.name}.before-agentcontrol-{stamp}")
    shutil.copy2(config_path, backup)
    return backup


def build_outbound(settings: dict[str, Any]) -> dict[str, Any]:
    return {
        "tag": settings.get("outbound_tag") or OUTBOUND_TAG,
        "protocol": "vless",
        "settings": {
            "address": settings["address"],
            "port": int(settings["port"]),
            "id": settings["uuid"],
            "encryption": "none",
            "flow": settings.get("flow") or "xtls-rprx-vision",
            "level": 0,
        },
        "streamSettings": {
            "method": "raw",
            "security": "reality",
            "realitySettings": {
                "serverName": settings.get("server_name") or "",
                "fingerprint": settings.get("fingerprint") or "chrome",
                "password": settings.get("public_key") or "",
                "shortId": settings.get("short_id") or "",
                "spiderX": settings.get("spider_x") or "/",
            },
            "sockopt": {
                "tcpKeepAliveIdle": 45,
                "tcpKeepAliveInterval": 15,
                "tcpcongestion": "bbr",
            },
        },
        "mux": {"enabled": False},
    }


def build_inbound(settings: dict[str, Any]) -> dict[str, Any]:
    return {
        "tag": INBOUND_TAG,
        "listen": settings.get("proxy_listen") or DEFAULT_PROXY_LISTEN,
        "port": int(settings.get("proxy_port") or DEFAULT_PROXY_PORT),
        "protocol": "http",
        "settings": {"allowTransparent": False, "userLevel": 0},
    }


def validate_settings(settings: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not settings.get("enabled"):
        return errors
    for key in ("address", "uuid", "server_name", "public_key"):
        if not str(settings.get(key) or "").strip():
            errors.append(f"{key} is required")
    port = int(settings.get("port") or 0)
    if port < 1 or port > 65535:
        errors.append("port must be between 1 and 65535")
    proxy_port = int(settings.get("proxy_port") or 0)
    if proxy_port < 1 or proxy_port > 65535:
        errors.append("proxy_port must be between 1 and 65535")
    return errors


def apply_to_xray_config(settings: dict[str, Any]) -> dict[str, Any]:
    merged = _merge_settings(settings)
    errors = validate_settings(merged)
    if errors:
        raise ValueError("; ".join(errors))

    config_path = Path(merged.get("config_path") or DEFAULT_XRAY_CONFIG)
    if not config_path.is_file():
        raise FileNotFoundError(f"xray config not found: {config_path}")

    backup = _backup_config(config_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    inbound = build_inbound(merged)
    outbound = build_outbound(merged)

    inbounds = config.setdefault("inbounds", [])
    found_in = False
    for idx, item in enumerate(inbounds):
        if item.get("tag") == INBOUND_TAG:
            inbounds[idx] = inbound
            found_in = True
            break
    if not found_in:
        inbounds.append(inbound)

    outbounds = config.setdefault("outbounds", [])
    outbound_tag = outbound["tag"]
    found_out = False
    for idx, item in enumerate(outbounds):
        if item.get("tag") == outbound_tag:
            outbounds[idx] = outbound
            found_out = True
            break
    if not found_out:
        outbounds.append(outbound)

    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    save_client_settings(merged)
    return {
        "backup": str(backup),
        "config_path": str(config_path),
        "proxy_url": proxy_url(merged),
        "inbound_tag": INBOUND_TAG,
        "outbound_tag": outbound_tag,
    }


def proxy_url(settings: dict[str, Any] | None = None) -> str:
    merged = _merge_settings(settings or load_client_settings())
    listen = merged.get("proxy_listen") or DEFAULT_PROXY_LISTEN
    port = int(merged.get("proxy_port") or DEFAULT_PROXY_PORT)
    return f"http://{listen}:{port}"


def write_runtime_env(proxy_url_value: str | None) -> Path:
    env_path = Path("/etc/agentcontrol/env")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    if proxy_url_value:
        content = (
            f"HTTP_PROXY={proxy_url_value}\n"
            f"HTTPS_PROXY={proxy_url_value}\n"
            f"http_proxy={proxy_url_value}\n"
            f"https_proxy={proxy_url_value}\n"
            "NO_PROXY=localhost,127.0.0.1\n"
        )
    else:
        content = "# Direct mode — no HTTP proxy\nNO_PROXY=localhost,127.0.0.1\n"
    env_path.write_text(content, encoding="utf-8")
    os.chmod(env_path, 0o600)
    return env_path


def restart_xray() -> tuple[bool, str]:
    commands = [
        ["systemctl", "restart", "xray"],
        ["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "systemctl", "restart", "xray"],
        ["service", "xray", "restart"],
    ]
    last_error = ""
    for cmd in commands:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
            if result.returncode == 0:
                time.sleep(1)
                return True, cmd[0]
        except (OSError, subprocess.SubprocessError) as exc:
            last_error = str(exc)
            continue
        last_error = (result.stderr or result.stdout or "restart failed").strip()
    return False, last_error or "could not restart xray"


def proxy_listening(settings: dict[str, Any] | None = None) -> bool:
    import socket

    merged = _merge_settings(settings or load_client_settings())
    listen = str(merged.get("proxy_listen") or DEFAULT_PROXY_LISTEN)
    port = int(merged.get("proxy_port") or DEFAULT_PROXY_PORT)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1)
    try:
        sock.connect((listen, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def test_proxy(settings: dict[str, Any] | None = None, url: str = "https://cursor.com") -> dict[str, Any]:
    merged = _merge_settings(settings or load_client_settings())
    proxy = proxy_url(merged)
    started = time.time()
    try:
        opener = build_opener(ProxyHandler({"http": proxy, "https": proxy}))
        with opener.open(url, timeout=20) as response:
            elapsed_ms = int((time.time() - started) * 1000)
            return {
                "ok": True,
                "status": response.status,
                "url": url,
                "proxy": proxy,
                "elapsed_ms": elapsed_ms,
            }
    except URLError as exc:
        return {
            "ok": False,
            "url": url,
            "proxy": proxy,
            "error": str(exc.reason if hasattr(exc, "reason") else exc),
        }


def apply_client_settings(
    settings: dict[str, Any],
    *,
    restart: bool = True,
    write_env: bool = True,
) -> dict[str, Any]:
    merged = save_client_settings(settings)
    if not merged.get("enabled"):
        if write_env:
            write_runtime_env(None)
        return {"ok": True, "enabled": False, "proxy_url": None}

    applied = apply_to_xray_config(merged)
    restarted = False
    restart_detail = ""
    if restart:
        restarted, restart_detail = restart_xray()

    if write_env:
        write_runtime_env(applied["proxy_url"])

    listening = proxy_listening(merged)
    test = test_proxy(merged) if listening else {"ok": False, "error": "proxy port not listening"}

    return {
        "ok": True,
        "enabled": True,
        "applied": applied,
        "restarted": restarted,
        "restart_detail": restart_detail,
        "listening": listening,
        "test": test,
        "settings": public_settings(merged),
    }


def public_settings(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    merged = _merge_settings(settings or load_client_settings())
    return {
        "enabled": merged.get("enabled", True),
        "config_path": merged.get("config_path"),
        "proxy_listen": merged.get("proxy_listen"),
        "proxy_port": merged.get("proxy_port"),
        "proxy_url": proxy_url(merged),
        "outbound_tag": merged.get("outbound_tag"),
        "address": merged.get("address"),
        "port": merged.get("port"),
        "uuid": merged.get("uuid"),
        "flow": merged.get("flow"),
        "server_name": merged.get("server_name"),
        "fingerprint": merged.get("fingerprint"),
        "public_key": merged.get("public_key"),
        "short_id": merged.get("short_id"),
        "spider_x": merged.get("spider_x"),
        "listening": proxy_listening(merged),
    }
