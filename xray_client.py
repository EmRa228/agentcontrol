"""Manage xray client (share-link outbound + local HTTP inbound) for AgentControl."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import ProxyHandler, build_opener

import yaml

DEFAULT_XRAY_CONFIG = Path("/usr/local/etc/xray/config.json")
DEFAULT_CLIENT_FILE = Path("/etc/agentcontrol/xray-client.yaml")
STATUS_FILE = Path(os.environ.get("XRAY_STATUS_FILE", "/var/lib/agentcontrol/xray-status.json"))
INBOUND_TAG = "agentcontrol-http-in"
OUTBOUND_TAG = "reality-out"
DEFAULT_PROXY_PORT = 30229
DEFAULT_PROXY_LISTEN = "127.0.0.1"
SUPPORTED_PROTOCOLS = {"vless", "trojan", "vmess", "ss", "socks"}


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
        "password": "",
        "flow": "",
        "network": "tcp",
        "security": "reality",
        "server_name": "",
        "fingerprint": "chrome",
        "public_key": "",
        "short_id": "",
        "spider_x": "",
        "alter_id": 0,
        "method": "",
        "path": "",
        "host": "",
        "share_url": "",
    }


def _first_param(params: dict[str, list[str]], *keys: str) -> str:
    for key in keys:
        values = params.get(key)
        if values and str(values[0]).strip():
            return str(values[0]).strip()
    return ""


def _base_settings(protocol: str, address: str, port: int) -> dict[str, Any]:
    return {
        "protocol": protocol,
        "address": address,
        "port": int(port),
        "uuid": "",
        "password": "",
        "flow": "",
        "network": "tcp",
        "security": "none",
        "server_name": "",
        "fingerprint": "chrome",
        "public_key": "",
        "short_id": "",
        "spider_x": "",
        "alter_id": 0,
        "method": "",
        "path": "",
        "host": "",
    }


def _parse_common_transport(params: dict[str, list[str]], settings: dict[str, Any]) -> None:
    network = _first_param(params, "type", "network")
    security = _first_param(params, "security")
    if network in {"tcp", "ws", "grpc", "h2", "http", "quic"}:
        settings["network"] = network
    elif security in {"tcp", "ws", "grpc", "h2", "http", "quic"}:
        settings["network"] = security
        security = _first_param(params, "security")
    if security in {"none", "tls", "reality"}:
        settings["security"] = security
    settings["server_name"] = _first_param(params, "sni", "serverName", "peer", "host") or settings["server_name"]
    settings["fingerprint"] = _first_param(params, "fp", "fingerprint") or settings["fingerprint"]
    settings["path"] = _first_param(params, "path")
    settings["host"] = _first_param(params, "host", "Host")


def parse_vless_url(url: str) -> dict[str, Any]:
    return parse_share_url(url)


def _parse_vless_url(parsed, raw: str) -> dict[str, Any]:
    if not parsed.hostname:
        raise ValueError("missing server host in vless URL")
    uuid = unquote(parsed.username or "").strip()
    if not uuid:
        raise ValueError("missing UUID in vless URL")

    params = parse_qs(parsed.query, keep_blank_values=False)
    settings = _base_settings("vless", parsed.hostname, parsed.port or 443)
    settings["uuid"] = uuid
    settings["flow"] = _first_param(params, "flow")
    settings["security"] = _first_param(params, "security") or "reality"
    _parse_common_transport(params, settings)
    settings["public_key"] = _first_param(params, "pbk", "publicKey", "public_key")
    settings["short_id"] = _first_param(params, "sid", "shortId", "short_id")
    settings["spider_x"] = _first_param(params, "spx", "spiderX", "spider_x")

    encryption = _first_param(params, "encryption")
    if encryption and encryption != "none":
        raise ValueError(f"unsupported encryption: {encryption}")
    if settings["security"] == "reality":
        if not settings["server_name"]:
            raise ValueError("reality SNI (sni=) is required")
        if not settings["public_key"]:
            raise ValueError("reality public key (pbk=) is required")
    settings["share_url"] = raw
    return settings


def _parse_trojan_url(parsed, raw: str) -> dict[str, Any]:
    if not parsed.hostname:
        raise ValueError("missing server host in trojan URL")
    password = unquote(parsed.username or "").strip()
    if not password:
        raise ValueError("missing trojan password")

    params = parse_qs(parsed.query, keep_blank_values=False)
    settings = _base_settings("trojan", parsed.hostname, parsed.port or 443)
    settings["password"] = password
    settings["security"] = _first_param(params, "security") or "none"
    _parse_common_transport(params, settings)
    settings["share_url"] = raw
    return settings


def _parse_vmess_url(raw: str) -> dict[str, Any]:
    payload = raw.split("://", 1)[1].strip()
    if "?" in payload:
        payload = payload.split("?", 1)[0]
    if "#" in payload:
        payload = payload.split("#", 1)[0]
    padded = payload + "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("invalid vmess share link") from exc

    address = str(data.get("add") or data.get("address") or "").strip()
    if not address:
        raise ValueError("missing server address in vmess link")
    uuid = str(data.get("id") or "").strip()
    if not uuid:
        raise ValueError("missing UUID in vmess link")

    settings = _base_settings("vmess", address, int(data.get("port") or 443))
    settings["uuid"] = uuid
    settings["alter_id"] = int(data.get("aid") or data.get("alterId") or 0)
    settings["network"] = str(data.get("net") or data.get("type") or "tcp")
    tls = str(data.get("tls") or "").strip().lower()
    settings["security"] = "tls" if tls in {"tls", "1", "true"} else "none"
    settings["server_name"] = str(data.get("sni") or data.get("host") or "")
    settings["host"] = str(data.get("host") or "")
    settings["path"] = str(data.get("path") or "")
    settings["share_url"] = raw
    return settings


def _parse_shadowsocks_url(parsed, raw: str) -> dict[str, Any]:
    if parsed.username and parsed.password and parsed.hostname:
        method = unquote(parsed.username)
        password = unquote(parsed.password)
        address = parsed.hostname
        port = parsed.port or 8388
    else:
        body = raw.split("://", 1)[1].strip()
        if "@" in body:
            creds, endpoint = body.rsplit("@", 1)
        else:
            creds, endpoint = body, ""
        if "#" in endpoint:
            endpoint = endpoint.split("#", 1)[0]
        padded = creds + "=" * (-len(creds) % 4)
        try:
            decoded = base64.urlsafe_b64decode(padded).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("invalid shadowsocks share link") from exc
        if "@" not in decoded:
            raise ValueError("invalid shadowsocks share link")
        method_password, endpoint = decoded.split("@", 1)
        method, password = method_password.split(":", 1)
        if ":" in endpoint:
            address, port_text = endpoint.rsplit(":", 1)
            port = int(port_text)
        else:
            address, port = endpoint, 8388

    if not method or not password or not address:
        raise ValueError("invalid shadowsocks share link")

    params = parse_qs(parsed.query, keep_blank_values=False) if parsed.query else {}
    settings = _base_settings("ss", address, port)
    settings["method"] = method
    settings["password"] = password
    settings["network"] = _first_param(params, "type", "network") or "tcp"
    settings["share_url"] = raw
    return settings


def _parse_socks_url(parsed, raw: str) -> dict[str, Any]:
    if not parsed.hostname:
        raise ValueError("missing server host in socks URL")
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    if not username and not password:
        raise ValueError("missing socks credentials")
    settings = _base_settings("socks", parsed.hostname, parsed.port or 1080)
    settings["uuid"] = username
    settings["password"] = password
    settings["share_url"] = raw
    return settings


def parse_share_url(url: str) -> dict[str, Any]:
    """Parse an xray-compatible share link (vless/trojan/vmess/ss/socks)."""
    raw = (url or "").strip()
    if not raw:
        raise ValueError("share URL is empty")
    if "://" not in raw:
        raise ValueError("share URL must include a protocol scheme")

    parsed = urlparse(raw)
    protocol = (parsed.scheme or "").lower()
    if protocol not in SUPPORTED_PROTOCOLS:
        raise ValueError(
            f"unsupported protocol: {protocol} (supported: {', '.join(sorted(SUPPORTED_PROTOCOLS))})"
        )

    if protocol == "vless":
        return _parse_vless_url(parsed, raw)
    if protocol == "trojan":
        return _parse_trojan_url(parsed, raw)
    if protocol == "vmess":
        return _parse_vmess_url(raw)
    if protocol == "ss":
        return _parse_shadowsocks_url(parsed, raw)
    return _parse_socks_url(parsed, raw)


def build_share_url(settings: dict[str, Any]) -> str:
    """Serialize settings back to a share link when possible."""
    merged = _merge_settings(settings)
    if merged.get("share_url"):
        return str(merged["share_url"])

    protocol = str(merged.get("protocol") or "vless").lower()
    if protocol == "vless":
        return _build_vless_url(merged)
    if protocol == "trojan":
        return _build_trojan_url(merged)
    if protocol == "vmess":
        return _build_vmess_url(merged)
    if protocol == "ss":
        return _build_shadowsocks_url(merged)
    raise ValueError(f"cannot build share URL for protocol: {protocol}")


def build_vless_url(settings: dict[str, Any]) -> str:
    return _build_vless_url(_merge_settings(settings))


def _build_vless_url(settings: dict[str, Any]) -> str:
    address = str(settings.get("address") or "").strip()
    uuid = str(settings.get("uuid") or "").strip()
    if not address or not uuid:
        raise ValueError("address and uuid are required")

    port = int(settings.get("port") or 443)
    security = str(settings.get("security") or "reality")
    query_parts = [f"security={security}", "encryption=none"]
    network = str(settings.get("network") or "tcp").strip()
    if network:
        query_parts.append(f"type={network}")
    flow = str(settings.get("flow") or "").strip()
    if flow:
        query_parts.append(f"flow={flow}")
    server_name = str(settings.get("server_name") or "").strip()
    if server_name:
        query_parts.append(f"sni={server_name}")
    public_key = str(settings.get("public_key") or "").strip()
    if public_key:
        query_parts.append(f"pbk={public_key}")
    short_id = str(settings.get("short_id") or "").strip()
    if short_id:
        query_parts.append(f"sid={short_id}")
    fingerprint = str(settings.get("fingerprint") or "chrome").strip()
    if fingerprint:
        query_parts.append(f"fp={fingerprint}")
    return f"vless://{uuid}@{address}:{port}?{'&'.join(query_parts)}"


def _build_trojan_url(settings: dict[str, Any]) -> str:
    address = str(settings.get("address") or "").strip()
    password = str(settings.get("password") or "").strip()
    if not address or not password:
        raise ValueError("address and password are required")
    port = int(settings.get("port") or 443)
    security = str(settings.get("security") or "none")
    network = str(settings.get("network") or "tcp")
    query_parts = [f"security={security}", f"type={network}"]
    server_name = str(settings.get("server_name") or "").strip()
    if server_name:
        query_parts.append(f"sni={server_name}")
    return f"trojan://{quote(password, safe='')}@{address}:{port}?{'&'.join(query_parts)}"


def _build_vmess_url(settings: dict[str, Any]) -> str:
    payload = {
        "v": "2",
        "ps": "agentcontrol",
        "add": settings.get("address"),
        "port": str(settings.get("port") or 443),
        "id": settings.get("uuid"),
        "aid": str(settings.get("alter_id") or 0),
        "net": settings.get("network") or "tcp",
        "type": "none",
        "host": settings.get("host") or "",
        "path": settings.get("path") or "",
        "tls": "tls" if settings.get("security") == "tls" else "",
        "sni": settings.get("server_name") or "",
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")
    return f"vmess://{encoded.rstrip('=')}"


def _build_shadowsocks_url(settings: dict[str, Any]) -> str:
    method = str(settings.get("method") or "").strip()
    password = str(settings.get("password") or "").strip()
    address = str(settings.get("address") or "").strip()
    if not method or not password or not address:
        raise ValueError("method, password and address are required")
    port = int(settings.get("port") or 8388)
    creds = base64.urlsafe_b64encode(f"{method}:{password}".encode("utf-8")).decode("ascii").rstrip("=")
    return f"ss://{creds}@{address}:{port}"


def merge_share_url(settings: dict[str, Any] | None, share_url: str) -> dict[str, Any]:
    """Merge any supported share link into existing client settings."""
    merged = _merge_settings(settings)
    parsed = parse_share_url(share_url)
    merged.update(parsed)
    merged["enabled"] = True
    return merged


def merge_vless_url(settings: dict[str, Any] | None, vless_url: str) -> dict[str, Any]:
    return merge_share_url(settings, vless_url)


def _merge_settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    settings = default_client_settings()
    if raw:
        settings.update({k: v for k, v in raw.items() if v is not None})
    settings["proxy_port"] = int(settings.get("proxy_port") or DEFAULT_PROXY_PORT)
    settings["port"] = int(settings.get("port") or 443)
    settings["enabled"] = bool(settings.get("enabled", True))
    settings["alter_id"] = int(settings.get("alter_id") or 0)
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


def _import_stream_settings(stream: dict[str, Any], settings: dict[str, Any]) -> None:
    settings["network"] = str(stream.get("network") or stream.get("method") or "tcp")
    settings["security"] = str(stream.get("security") or "none")
    tls = stream.get("tlsSettings") or {}
    reality = stream.get("realitySettings") or {}
    settings["server_name"] = str(
        reality.get("serverName") or tls.get("serverName") or settings.get("server_name") or ""
    )
    settings["fingerprint"] = str(
        reality.get("fingerprint") or tls.get("fingerprint") or settings.get("fingerprint") or "chrome"
    )
    settings["public_key"] = str(reality.get("publicKey") or reality.get("password") or "")
    settings["short_id"] = str(reality.get("shortId") or "")
    settings["spider_x"] = str(reality.get("spiderX") or "")
    ws = stream.get("wsSettings") or {}
    settings["path"] = str(ws.get("path") or settings.get("path") or "")
    settings["host"] = str((ws.get("headers") or {}).get("Host") or settings.get("host") or "")


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

    if not outbound:
        return settings if settings.get("address") else None

    protocol = str(outbound.get("protocol") or "").lower()
    settings["protocol"] = protocol
    ob_settings = outbound.get("settings") or {}

    if protocol == "vless":
        vnext = ob_settings.get("vnext") or []
        if vnext:
            node = vnext[0]
            users = node.get("users") or [{}]
            user = users[0] if users else {}
            settings["address"] = str(node.get("address") or "")
            settings["port"] = int(node.get("port") or 443)
            settings["uuid"] = str(user.get("id") or "")
            settings["flow"] = str(user.get("flow") or "")
        else:
            settings["address"] = str(ob_settings.get("address") or "")
            settings["port"] = int(ob_settings.get("port") or 443)
            settings["uuid"] = str(ob_settings.get("id") or "")
            settings["flow"] = str(ob_settings.get("flow") or "")
    elif protocol == "trojan":
        servers = ob_settings.get("servers") or []
        if servers:
            node = servers[0]
            settings["address"] = str(node.get("address") or "")
            settings["port"] = int(node.get("port") or 443)
            settings["password"] = str(node.get("password") or "")
    elif protocol == "vmess":
        vnext = ob_settings.get("vnext") or []
        if vnext:
            node = vnext[0]
            users = node.get("users") or [{}]
            user = users[0] if users else {}
            settings["address"] = str(node.get("address") or "")
            settings["port"] = int(node.get("port") or 443)
            settings["uuid"] = str(user.get("id") or "")
            settings["alter_id"] = int(user.get("alterId") or 0)
    elif protocol == "shadowsocks":
        servers = ob_settings.get("servers") or []
        if servers:
            node = servers[0]
            settings["address"] = str(node.get("address") or "")
            settings["port"] = int(node.get("port") or 8388)
            settings["method"] = str(node.get("method") or "")
            settings["password"] = str(node.get("password") or "")
    else:
        return settings if settings.get("address") else None

    _import_stream_settings(outbound.get("streamSettings") or {}, settings)
    return settings


def _backup_config(config_path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = config_path.with_name(f"{config_path.name}.before-agentcontrol-{stamp}")
    shutil.copy2(config_path, backup)
    return backup


def _stream_settings(settings: dict[str, Any]) -> dict[str, Any]:
    network = str(settings.get("network") or "tcp").strip() or "tcp"
    security = str(settings.get("security") or "none").strip() or "none"
    stream: dict[str, Any] = {"network": network, "security": security}

    if security == "tls":
        tls_settings: dict[str, Any] = {
            "serverName": settings.get("server_name") or settings.get("address") or "",
        }
        if settings.get("fingerprint"):
            tls_settings["fingerprint"] = settings["fingerprint"]
        stream["tlsSettings"] = tls_settings
    elif security == "reality":
        reality_settings: dict[str, Any] = {
            "serverName": settings.get("server_name") or "",
            "fingerprint": settings.get("fingerprint") or "chrome",
            "publicKey": settings.get("public_key") or "",
            "shortId": settings.get("short_id") or "",
        }
        spider_x = str(settings.get("spider_x") or "").strip()
        if spider_x:
            reality_settings["spiderX"] = spider_x
        stream["realitySettings"] = reality_settings

    if network == "ws":
        headers = {}
        if settings.get("host"):
            headers["Host"] = settings["host"]
        ws_settings: dict[str, Any] = {"path": settings.get("path") or "/"}
        if headers:
            ws_settings["headers"] = headers
        stream["wsSettings"] = ws_settings
    elif network == "grpc":
        stream["grpcSettings"] = {"serviceName": settings.get("path") or settings.get("host") or ""}

    stream["sockopt"] = {
        "tcpKeepAliveIdle": 45,
        "tcpKeepAliveInterval": 15,
        "tcpcongestion": "bbr",
    }
    return stream


def build_outbound(settings: dict[str, Any]) -> dict[str, Any]:
    protocol = str(settings.get("protocol") or "vless").lower()
    tag = settings.get("outbound_tag") or OUTBOUND_TAG
    stream = _stream_settings(settings)

    if protocol == "trojan":
        return {
            "tag": tag,
            "protocol": "trojan",
            "settings": {
                "servers": [
                    {
                        "address": settings["address"],
                        "port": int(settings["port"]),
                        "password": settings["password"],
                    }
                ]
            },
            "streamSettings": stream,
            "mux": {"enabled": False},
        }

    if protocol == "vmess":
        return {
            "tag": tag,
            "protocol": "vmess",
            "settings": {
                "vnext": [
                    {
                        "address": settings["address"],
                        "port": int(settings["port"]),
                        "users": [
                            {
                                "id": settings["uuid"],
                                "alterId": int(settings.get("alter_id") or 0),
                                "security": "auto",
                            }
                        ],
                    }
                ]
            },
            "streamSettings": stream,
            "mux": {"enabled": False},
        }

    if protocol == "ss":
        return {
            "tag": tag,
            "protocol": "shadowsocks",
            "settings": {
                "servers": [
                    {
                        "address": settings["address"],
                        "port": int(settings["port"]),
                        "method": settings["method"],
                        "password": settings["password"],
                    }
                ]
            },
            "streamSettings": stream,
            "mux": {"enabled": False},
        }

    if protocol == "socks":
        return {
            "tag": tag,
            "protocol": "socks",
            "settings": {
                "servers": [
                    {
                        "address": settings["address"],
                        "port": int(settings["port"]),
                        "users": [
                            {
                                "user": settings.get("uuid") or "",
                                "pass": settings.get("password") or "",
                            }
                        ],
                    }
                ]
            },
            "streamSettings": stream,
            "mux": {"enabled": False},
        }

    user: dict[str, Any] = {
        "id": settings["uuid"],
        "encryption": "none",
        "level": 0,
    }
    flow = str(settings.get("flow") or "").strip()
    if flow:
        user["flow"] = flow

    return {
        "tag": tag,
        "protocol": "vless",
        "settings": {
            "vnext": [
                {
                    "address": settings["address"],
                    "port": int(settings["port"]),
                    "users": [user],
                }
            ]
        },
        "streamSettings": stream,
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

    protocol = str(settings.get("protocol") or "vless").lower()
    if not str(settings.get("address") or "").strip():
        errors.append("address is required")

    port = int(settings.get("port") or 0)
    if port < 1 or port > 65535:
        errors.append("port must be between 1 and 65535")

    proxy_port = int(settings.get("proxy_port") or 0)
    if proxy_port < 1 or proxy_port > 65535:
        errors.append("proxy_port must be between 1 and 65535")

    if protocol == "vless":
        if not str(settings.get("uuid") or "").strip():
            errors.append("uuid is required")
        if settings.get("security") == "reality":
            if not str(settings.get("server_name") or "").strip():
                errors.append("server_name is required")
            if not str(settings.get("public_key") or "").strip():
                errors.append("public_key is required")
    elif protocol == "trojan":
        if not str(settings.get("password") or "").strip():
            errors.append("password is required")
    elif protocol == "vmess":
        if not str(settings.get("uuid") or "").strip():
            errors.append("uuid is required")
    elif protocol == "ss":
        if not str(settings.get("method") or "").strip():
            errors.append("method is required")
        if not str(settings.get("password") or "").strip():
            errors.append("password is required")
    elif protocol == "socks":
        if not str(settings.get("password") or "").strip():
            errors.append("password is required")

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


_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NODE_USE_ENV_PROXY",
)


def load_runtime_proxy_env() -> dict[str, str]:
    """Load HTTP proxy variables for Cursor agent / Node processes."""
    if not load_client_settings().get("enabled"):
        return {}

    env_path = Path("/etc/agentcontrol/env")
    values: dict[str, str] = {}
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()

    proxy = (
        values.get("HTTPS_PROXY")
        or values.get("https_proxy")
        or values.get("HTTP_PROXY")
        or values.get("http_proxy")
        or ""
    )
    if not proxy and load_client_settings().get("enabled"):
        proxy = proxy_url()

    if not proxy:
        return {}

    return {
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
        "http_proxy": proxy,
        "https_proxy": proxy,
        "ALL_PROXY": proxy,
        "all_proxy": proxy,
        "NODE_USE_ENV_PROXY": "1",
        "NO_PROXY": values.get("NO_PROXY", "localhost,127.0.0.1"),
    }


def apply_proxy_env(env: dict[str, str] | None = None) -> dict[str, str]:
    merged = dict(env or os.environ)
    if not load_client_settings().get("enabled"):
        for key in _PROXY_ENV_KEYS:
            merged.pop(key, None)
        return merged
    merged.update(load_runtime_proxy_env())
    return merged


def set_proxy_enabled(enabled: bool, *, restart: bool = False) -> dict[str, Any]:
    """Enable or disable the xray HTTP proxy for Cursor traffic."""
    settings = load_client_settings()
    settings["enabled"] = bool(enabled)
    return apply_client_settings(settings, restart=restart, write_env=True)


def write_runtime_env(proxy_url_value: str | None) -> Path:
    env_path = Path("/etc/agentcontrol/env")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    if proxy_url_value:
        content = (
            f"HTTP_PROXY={proxy_url_value}\n"
            f"HTTPS_PROXY={proxy_url_value}\n"
            f"http_proxy={proxy_url_value}\n"
            f"https_proxy={proxy_url_value}\n"
            f"ALL_PROXY={proxy_url_value}\n"
            f"all_proxy={proxy_url_value}\n"
            "NODE_USE_ENV_PROXY=1\n"
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
            result = {
                "ok": True,
                "status": response.status,
                "url": url,
                "proxy": proxy,
                "elapsed_ms": elapsed_ms,
            }
    except URLError as exc:
        result = {
            "ok": False,
            "url": url,
            "proxy": proxy,
            "error": str(exc.reason if hasattr(exc, "reason") else exc),
        }
    record_status(merged, cursor_test=result)
    return result


def xray_service_active() -> bool:
    for cmd in (
        ["systemctl", "is-active", "--quiet", "xray"],
        ["pgrep", "-x", "xray"],
    ):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=5, check=False)
            if result.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


def read_xray_journal_tail(lines: int = 25) -> str:
    commands = [
        ["journalctl", "-u", "xray", "-n", str(lines), "--no-pager"],
        [
            "nsenter",
            "-t",
            "1",
            "-m",
            "-u",
            "-i",
            "-n",
            "-p",
            "journalctl",
            "-u",
            "xray",
            "-n",
            str(lines),
            "--no-pager",
        ],
    ]
    for cmd in commands:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
            if result.returncode == 0 and (result.stdout or "").strip():
                return result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
    return ""


def record_status(
    settings: dict[str, Any] | None,
    *,
    cursor_test: dict[str, Any] | None = None,
    cursor_api_test: dict[str, Any] | None = None,
    restart_ok: bool | None = None,
    restart_detail: str = "",
) -> dict[str, Any]:
    merged = _merge_settings(settings or load_client_settings())
    listening = proxy_listening(merged)
    status = load_status()
    status.update(
        {
            "at": datetime.now(timezone.utc).isoformat(),
            "enabled": merged.get("enabled", True),
            "proxy_url": proxy_url(merged),
            "listening": listening,
            "xray_active": xray_service_active(),
        }
    )
    if cursor_test is not None:
        status["cursor_test"] = cursor_test
    if cursor_api_test is not None:
        status["cursor_api_test"] = cursor_api_test
    if restart_ok is not None:
        status["restart_ok"] = restart_ok
        status["restart_detail"] = restart_detail
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    return status


def load_status() -> dict[str, Any]:
    if STATUS_FILE.is_file():
        try:
            return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    merged = load_client_settings()
    return {
        "at": None,
        "enabled": merged.get("enabled", True),
        "proxy_url": proxy_url(merged),
        "listening": proxy_listening(merged),
        "xray_active": xray_service_active(),
        "cursor_test": None,
        "cursor_api_test": None,
    }


def test_cursor_api(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Test the same host agent workers use (api2.cursor.sh)."""
    return test_proxy(settings, url="https://api2.cursor.sh")


def build_status_report(settings: dict[str, Any] | None = None, *, live_test: bool = False) -> dict[str, Any]:
    merged = _merge_settings(settings or load_client_settings())
    status = load_status()
    status["settings"] = public_settings(merged)
    status["listening"] = proxy_listening(merged)
    status["xray_active"] = xray_service_active()
    status["proxy_url"] = proxy_url(merged)
    status["journal_tail"] = read_xray_journal_tail()
    status["worker_errors"] = recent_worker_errors()
    if live_test and merged.get("enabled"):
        status["cursor_test"] = test_proxy(merged)
        status["cursor_api_test"] = test_cursor_api(merged)
    return status


def recent_worker_errors(limit: int = 5) -> list[dict[str, str]]:
    state_dir = Path(os.environ.get("STATE_DIR", "/var/lib/agentcontrol"))
    errors: list[dict[str, str]] = []
    if not state_dir.is_dir():
        return errors
    markers = ("failed to reach", "error", "✗", "proxy", "unauthorized", "denied")
    for log_file in sorted(state_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            text = log_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        tail = text[-4000:]
        hit_lines = [
            line.strip()
            for line in tail.splitlines()
            if line.strip() and any(marker in line.lower() for marker in markers)
        ]
        if hit_lines:
            errors.append(
                {
                    "project": log_file.stem,
                    "tail": "\n".join(hit_lines[-8:]),
                }
            )
        if len(errors) >= limit:
            break
    return errors


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
        record_status(merged, restart_ok=None, restart_detail="direct mode")
        return {
            "ok": True,
            "enabled": False,
            "proxy_url": None,
            "settings": public_settings(merged),
            "status": build_status_report(merged),
        }

    applied = apply_to_xray_config(merged)
    restarted = False
    restart_detail = ""
    if restart:
        restarted, restart_detail = restart_xray()

    if write_env:
        write_runtime_env(applied["proxy_url"])

    listening = proxy_listening(merged)
    cursor_test = test_proxy(merged) if listening else {"ok": False, "error": "proxy port not listening"}
    cursor_api_test = test_cursor_api(merged) if listening else {"ok": False, "error": "proxy port not listening"}

    record_status(
        merged,
        cursor_test=cursor_test,
        cursor_api_test=cursor_api_test,
        restart_ok=restarted,
        restart_detail=restart_detail,
    )

    return {
        "ok": True,
        "enabled": True,
        "applied": applied,
        "restarted": restarted,
        "restart_detail": restart_detail,
        "listening": listening,
        "test": cursor_test,
        "cursor_api_test": cursor_api_test,
        "settings": public_settings(merged),
        "status": build_status_report(merged),
    }


def public_settings(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    merged = _merge_settings(settings or load_client_settings())
    public = {
        "enabled": merged.get("enabled", True),
        "config_path": merged.get("config_path"),
        "proxy_listen": merged.get("proxy_listen"),
        "proxy_port": merged.get("proxy_port"),
        "proxy_url": proxy_url(merged),
        "outbound_tag": merged.get("outbound_tag"),
        "protocol": merged.get("protocol"),
        "address": merged.get("address"),
        "port": merged.get("port"),
        "uuid": merged.get("uuid"),
        "password": merged.get("password"),
        "flow": merged.get("flow") or "",
        "network": merged.get("network") or "tcp",
        "security": merged.get("security") or "none",
        "server_name": merged.get("server_name"),
        "fingerprint": merged.get("fingerprint"),
        "public_key": merged.get("public_key"),
        "short_id": merged.get("short_id"),
        "spider_x": merged.get("spider_x") or "",
        "alter_id": merged.get("alter_id") or 0,
        "method": merged.get("method") or "",
        "listening": proxy_listening(merged),
    }
    try:
        public["share_url"] = build_share_url(merged)
    except ValueError:
        public["share_url"] = merged.get("share_url") or ""
    public["vless_url"] = public["share_url"]
    return public
