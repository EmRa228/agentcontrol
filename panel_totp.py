"""Optional TOTP (RFC 6238) for AgentControl panel login."""

from __future__ import annotations

import json
import os
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
        return {"enabled": False, "secret": "", "pending_secret": ""}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"enabled": False, "secret": "", "pending_secret": ""}
    pending = data.get("pending_secret", "")
    return {
        "enabled": bool(data.get("enabled")),
        "secret": str(data.get("secret", "")).strip(),
        "pending_secret": str(pending or "").strip(),
    }


def save_totp_state(enabled: bool, secret: str, pending_secret: str = "") -> None:
    path = totp_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "enabled": bool(enabled),
        "secret": secret.strip(),
        "pending_secret": pending_secret.strip(),
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
    """Status for unauthenticated pages: never include the secret or its URI."""
    state = load_totp_state()
    secret = state.get("secret", "")
    return {
        "enabled": totp_enabled(),
        "configured": bool(secret),
        "pending": bool(state.get("pending_secret")),
    }


def begin_totp_setup(hostname: str) -> dict:
    """Create (or reuse) a pending secret and return everything the UI needs.

    The active authenticator is left untouched, so an enabled panel keeps
    working until the new secret is verified with a 6-digit code.
    """
    state = load_totp_state()
    secret = state.get("pending_secret") or generate_totp_secret()
    save_totp_state(state.get("enabled", False), state.get("secret", ""), secret)
    uri = provisioning_uri(secret, account=hostname)
    return {
        "secret": secret,
        "provisioning_uri": uri,
        "qr_data_uri": totp_qr_data_uri(uri),
        "enabled": bool(state.get("enabled")),
    }


def rotate_totp_secret(hostname: str) -> dict:
    """Start over with a brand new pending secret."""
    state = load_totp_state()
    secret = generate_totp_secret()
    save_totp_state(state.get("enabled", False), state.get("secret", ""), secret)
    uri = provisioning_uri(secret, account=hostname)
    return {
        "secret": secret,
        "provisioning_uri": uri,
        "qr_data_uri": totp_qr_data_uri(uri),
        "enabled": bool(state.get("enabled")),
    }


def enable_totp(secret: str, code: str) -> tuple[bool, str]:
    """Verify ``code`` for ``secret`` and make it the active factor."""
    if not verify_totp_code(secret, code):
        return False, "invalid authenticator code"
    save_totp_state(True, secret)
    return True, ""


def enable_pending_totp(code: str, fallback_secret: str = "") -> tuple[bool, str]:
    """Verify the pending (setup) secret and activate it."""
    state = load_totp_state()
    secret = state.get("pending_secret") or str(fallback_secret or "").strip()
    if not secret:
        return False, "no authenticator secret; start setup first"
    return enable_totp(secret, code)


def disable_totp() -> None:
    save_totp_state(False, "")


def totp_qr_data_uri(provisioning_uri_value: str) -> str:
    """Render the provisioning URI as an inline SVG QR code (segno)."""
    if not provisioning_uri_value:
        return ""
    try:
        import segno

        return segno.make(provisioning_uri_value, error="m").svg_data_uri(scale=4)
    except Exception:
        return ""
