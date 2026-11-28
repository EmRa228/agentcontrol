"""SQLite-backed browser sessions for the AgentControl panel.

Sessions replace the legacy practice of storing the panel password inside the
browser cookie. Only a SHA-256 hash of each random token is persisted, so the
database does not reveal usable credentials and every session can be revoked
individually or all at once.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

SESSION_COOKIE = "agentcontrol_session"
DEFAULT_SESSION_DAYS = 30
TOUCH_INTERVAL_SECONDS = 60.0


class SessionStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=10)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA busy_timeout=5000")
        return con

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, closing(self._connect()) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    last_seen  REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    ip         TEXT NOT NULL DEFAULT '',
                    ua         TEXT NOT NULL DEFAULT ''
                )
                """
            )
            con.commit()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create(self, ttl_seconds: float, *, ip: str = "", ua: str = "") -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock, closing(self._connect()) as con:
            con.execute(
                """
                INSERT INTO sessions (token_hash, created_at, last_seen, expires_at, ip, ua)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (self._hash(token), now, now, now + ttl_seconds, ip[:200], ua[:400]),
            )
            con.commit()
        return token

    def validate(self, token: str | None) -> bool:
        if not token:
            return False
        digest = self._hash(token)
        now = time.time()
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                "SELECT last_seen, expires_at FROM sessions WHERE token_hash = ?",
                (digest,),
            ).fetchone()
            if row is None:
                return False
            last_seen, expires_at = row
            if expires_at <= now:
                con.execute("DELETE FROM sessions WHERE token_hash = ?", (digest,))
                con.commit()
                return False
            if now - last_seen >= TOUCH_INTERVAL_SECONDS:
                con.execute(
                    "UPDATE sessions SET last_seen = ? WHERE token_hash = ?",
                    (now, digest),
                )
                con.commit()
            return True

    def revoke(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock, closing(self._connect()) as con:
            cur = con.execute("DELETE FROM sessions WHERE token_hash = ?", (self._hash(token),))
            con.commit()
            return cur.rowcount > 0

    def revoke_all(self, *, except_token: str | None = None) -> int:
        with self._lock, closing(self._connect()) as con:
            if except_token:
                cur = con.execute(
                    "DELETE FROM sessions WHERE token_hash != ?",
                    (self._hash(except_token),),
                )
            else:
                cur = con.execute("DELETE FROM sessions")
            con.commit()
            return int(cur.rowcount or 0)

    def list_sessions(self, *, current_token: str | None = None) -> list[dict]:
        now = time.time()
        current_hash = self._hash(current_token) if current_token else ""
        with self._lock, closing(self._connect()) as con:
            rows = con.execute(
                """SELECT token_hash, created_at, last_seen, expires_at, ip, ua
                   FROM sessions WHERE expires_at > ? ORDER BY last_seen DESC""",
                (now,),
            ).fetchall()
        sessions = []
        for row in rows:
            token_hash = row[0]
            sessions.append(
                {
                    "id": token_hash[:16],
                    "created_at": row[1],
                    "last_seen": row[2],
                    "expires_at": row[3],
                    "ip": row[4],
                    "ua": row[5],
                    "current": bool(current_hash and token_hash == current_hash),
                }
            )
        return sessions

    def revoke_by_id(self, session_id: str) -> bool:
        session_id = (session_id or "").strip()
        if len(session_id) < 8 or not all(ch in "0123456789abcdef" for ch in session_id):
            return False
        with self._lock, closing(self._connect()) as con:
            cur = con.execute(
                "DELETE FROM sessions WHERE token_hash LIKE ?",
                (session_id + "%",),
            )
            con.commit()
            return bool(cur.rowcount)

    def prune(self) -> int:
        now = time.time()
        with self._lock, closing(self._connect()) as con:
            cur = con.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            con.commit()
            return int(cur.rowcount or 0)

    def count(self) -> int:
        now = time.time()
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                "SELECT COUNT(*) FROM sessions WHERE expires_at > ?",
                (now,),
            ).fetchone()
            return int(row[0]) if row else 0
