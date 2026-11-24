"""Optional TOTP (RFC 6238) for AgentControl panel login."""

from __future__ import annotations

import base64
import json
import os
import secrets
from pathlib import Path

import pyotp

DEFAULT_TOTP_FILE = Path(
    os.environ.get("AGENTCONTROL_TOTP_FILE", "/etc/agentcontrol/totp.json")
)


def totp_config_path() -> Path:
    return DEFAULT_TOTP_FILE


def load_totp_state() -> dict:
    path = totp_config_path()
    if not path.is_file():
        return {"enabled": False, "secret": ""}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"enabled": False, "secret": ""}
    return {
        "enabled": bool(data.get("enabled")),
        "secret": str(data.get("secret", "")).strip(),
    }


def save_totp_state(enabled: bool, secret: str) -> None:
    path = totp_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "enabled": bool(enabled),
        "secret": secret.strip(),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def totp_enabled() -> bool:
    state = load_totp_state()
    return bool(state.get("enabled") and state.get("secret"))


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, account: str, issuer: str = "AgentControl") -> str:
    totp = pyotp.TOTP(secret)
    return totp.provisioning_uri(name=account, issuer_name=issuer)


def verify_totp_code(secret: str, code: str, *, valid_window: int = 1) -> bool:
    if not secret or not code:
        return False
    cleaned = "".join(ch for ch in str(code).strip() if ch.isdigit())
    if len(cleaned) != 6:
        return False
    totp = pyotp.TOTP(secret)
    try:
        return bool(totp.verify(cleaned, valid_window=valid_window))
    except (TypeError, ValueError):
        return False


def verify_login_code(code: str) -> bool:
    state = load_totp_state()
    if not state.get("enabled"):
        return True
    return verify_totp_code(state.get("secret", ""), code)


def public_totp_status(hostname: str) -> dict:
    state = load_totp_state()
    enabled = totp_enabled()
    uri = ""
    secret = state.get("secret", "")
    if enabled and secret:
        uri = provisioning_uri(secret, account=hostname)
    return {
        "enabled": enabled,
        "configured": bool(secret),
        "provisioning_uri": uri if enabled else "",
    }


def rotate_totp_secret() -> str:
    secret = generate_totp_secret()
    save_totp_state(False, secret)
    return secret


def enable_totp(secret: str, code: str) -> tuple[bool, str]:
    if not verify_totp_code(secret, code):
        return False, "invalid authenticator code"
    save_totp_state(True, secret)
    return True, ""


def disable_totp() -> None:
    save_totp_state(False, "")


def totp_qr_data_uri(provisioning_uri: str) -> str:
    """Minimal inline SVG placeholder; browsers use provisioning_uri in authenticator apps."""
    _ = provisioning_uri
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120">'
        '<rect width="120" height="120" fill="#111"/>'
        '<text x="50%" y="50%" fill="#8b93a7" font-size="10" text-anchor="middle">'
        "Scan URI in app</text></svg>"
    )
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def touch_pending_secret() -> str:
    """Return secret for setup (not yet enabled)."""
    state = load_totp_state()
    secret = state.get("secret") or generate_totp_secret()
    if not state.get("secret"):
        save_totp_state(False, secret)
    return secret