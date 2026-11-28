"""Password hashing, API tokens, rate limiting and auth logging."""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

try:
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

    _HASHER: PasswordHasher | None = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
except Exception:  # pragma: no cover - fallback when argon2-cffi is unavailable
    _HASHER = None
    InvalidHashError = VerificationError = VerifyMismatchError = ValueError

ROLE_READ = "read"
ROLE_OPS = "ops"
ROLE_ADMIN = "admin"
ROLES = (ROLE_READ, ROLE_OPS, ROLE_ADMIN)
ROLE_LEVEL = {ROLE_READ: 1, ROLE_OPS: 2, ROLE_ADMIN: 3}
TOKEN_PREFIX = "ac"


def hash_password(password: str) -> str:
    if _HASHER is None:
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
        return "scrypt$" + salt.hex() + "$" + digest.hex()
    return _HASHER.hash(password)


def password_is_hashed(value: str) -> bool:
    return value.startswith("$argon2") or value.startswith("scrypt$")


def verify_password(password: str, stored: str) -> bool:
    if not stored:
        return False
    if password_is_hashed(stored):
        if stored.startswith("scrypt$"):
            try:
                _, salt_hex, digest_hex = stored.split("$", 2)
                digest = hashlib.scrypt(
                    password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1
                )
                return secrets.compare_digest(digest.hex(), digest_hex)
            except (ValueError, TypeError):
                return False
        if _HASHER is None:
            return False
        try:
            return bool(_HASHER.verify(stored, password))
        except (VerifyMismatchError, VerificationError, InvalidHashError, ValueError):
            return False
        except Exception:
            return False
    return secrets.compare_digest(password, stored)


def read_password_file(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def write_password_file(path: Path, password: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(hash_password(password) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def upgrade_password_file(path: Path, candidate: str) -> bool:
    """Rewrite a legacy plaintext password as a hash after a successful login."""
    current = read_password_file(path)
    if current is None or password_is_hashed(current):
        return False
    if not verify_password(candidate, current):
        return False
    write_password_file(path, candidate)
    return True


# ---------------------------------------------------------------------------
# API tokens


def _token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_token(store, name: str, role: str = ROLE_OPS) -> dict:
    if role not in ROLES:
        raise ValueError(f"unknown role: {role}")
    token_id = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    raw = f"{TOKEN_PREFIX}_{token_id}_{secret}"
    store.token_create(token_id, name or role, role, _token_hash(raw))
    return {"id": token_id, "name": name or role, "role": role, "token": raw}


def parse_token(raw: str) -> tuple[str, str] | None:
    if not raw or not raw.startswith(TOKEN_PREFIX + "_"):
        return None
    parts = raw.split("_", 2)
    if len(parts) != 3 or not parts[1] or not parts[2]:
        return None
    return parts[1], raw


def verify_token(raw: str, store) -> dict | None:
    parsed = parse_token(raw)
    if parsed is None:
        return None
    token_id, full = parsed
    row = store.token_lookup(_token_hash(full))
    if row is None or row.get("revoked_at"):
        return None
    if row.get("id") != token_id:
        return None
    return {"type": "token", "name": row["name"], "role": row["role"], "id": row["id"]}


def role_allows(role: str, needed: str) -> bool:
    return ROLE_LEVEL.get(role, 0) >= ROLE_LEVEL.get(needed, 99)


# ---------------------------------------------------------------------------
# Login rate limiting


class LoginLimiter:
    """Sliding-window failures per client with a fixed lockout."""

    def __init__(self, max_attempts: int = 10, window_seconds: int = 300, lockout_seconds: int = 300):
        self.max_attempts = max(1, int(max_attempts))
        self.window_seconds = max(1, int(window_seconds))
        self.lockout_seconds = max(1, int(lockout_seconds))
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._locked: dict[str, float] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> None:
        queue = self._failures.get(key)
        if not queue:
            return
        cutoff = now - self.window_seconds
        while queue and queue[0] < cutoff:
            queue.popleft()

    def check(self, key: str) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            until = self._locked.get(key)
            if until and until > now:
                return False, max(1, int(until - now))
            if until:
                self._locked.pop(key, None)
            self._prune(key, now)
            return True, 0

    def failure(self, key: str) -> tuple[int, int]:
        """Record a failure; returns (attempts_in_window, retry_after_seconds)."""
        now = time.monotonic()
        with self._lock:
            queue = self._failures[key]
            queue.append(now)
            self._prune(key, now)
            attempts = len(queue)
            if attempts >= self.max_attempts:
                self._locked[key] = now + self.lockout_seconds
                queue.clear()
                return attempts, self.lockout_seconds
            return attempts, 0

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
            self._locked.pop(key, None)

    def remaining(self, key: str) -> int:
        now = time.monotonic()
        with self._lock:
            until = self._locked.get(key)
            return max(0, int(until - now)) if until else 0


def append_auth_log(path: Path, ip: str, reason: str, path_hint: str = "") -> None:
    """Write one line per failure; fail2ban's filter matches `auth failure ip=<HOST>`."""
    stamp = datetime.now(timezone.utc).isoformat()
    safe_ip = (ip or "unknown").replace("\n", " ")[:80]
    safe_reason = (reason or "login").replace("\n", " ")[:120]
    safe_path = (path_hint or "").replace("\n", " ")[:120]
    line = f"{stamp} agentcontrol auth failure ip={safe_ip} reason={safe_reason} path={safe_path}\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
        try:
            path.chmod(0o640)
        except OSError:
            pass
    except OSError:
        pass


def unsafe_path(path: str) -> bool:
    return ".." in Path(path).parts
