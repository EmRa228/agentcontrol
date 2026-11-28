"""SQLite persistence for workers, audit, tokens, metrics, health and incidents."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA busy_timeout=5000")
        return con

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, closing(self._connect()) as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workers (
                    name TEXT PRIMARY KEY,
                    pid INTEGER,
                    worker_id TEXT,
                    started_at TEXT,
                    log_file TEXT,
                    mgmt_port INTEGER,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    actor TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT '',
                    ip TEXT NOT NULL DEFAULT '',
                    ua TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL,
                    target TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'panel'
                );
                CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts DESC);
                CREATE TABLE IF NOT EXISTS tokens (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL,
                    last_used_at REAL,
                    revoked_at REAL
                );
                CREATE TABLE IF NOT EXISTS metrics (
                    ts REAL PRIMARY KEY,
                    cpu REAL,
                    load_pct REAL,
                    ram REAL,
                    disk REAL,
                    swap REAL,
                    net REAL,
                    docker_running INTEGER
                );
                CREATE TABLE IF NOT EXISTS incidents (
                    key TEXT PRIMARY KEY,
                    event TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'open',
                    first_ts REAL NOT NULL,
                    last_ts REAL NOT NULL,
                    last_notified_ts REAL,
                    message TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS health (
                    ts REAL PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                """
            )
            con.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
            con.commit()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    # -- workers ---------------------------------------------------------
    def workers(self) -> dict[str, dict[str, Any]]:
        with self._lock, closing(self._connect()) as con:
            rows = con.execute(
                "SELECT name, pid, worker_id, started_at, log_file, mgmt_port FROM workers"
            ).fetchall()
        return {
            row["name"]: {
                "pid": row["pid"],
                "worker_id": row["worker_id"],
                "started_at": row["started_at"],
                "log_file": row["log_file"],
                "mgmt_port": row["mgmt_port"],
            }
            for row in rows
        }

    def replace_workers(self, state: dict[str, dict[str, Any]]) -> None:
        now = time.time()
        with self._lock, closing(self._connect()) as con:
            con.execute("DELETE FROM workers")
            for name, info in state.items():
                con.execute(
                    """INSERT INTO workers (name, pid, worker_id, started_at, log_file, mgmt_port, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        name,
                        info.get("pid"),
                        info.get("worker_id"),
                        info.get("started_at"),
                        info.get("log_file"),
                        info.get("mgmt_port"),
                        now,
                    ),
                )
            con.commit()

    # -- audit -----------------------------------------------------------
    def audit_add(
        self,
        action: str,
        *,
        actor: str = "",
        role: str = "",
        ip: str = "",
        ua: str = "",
        target: str = "",
        detail: str = "",
        source: str = "panel",
    ) -> None:
        with self._lock, closing(self._connect()) as con:
            con.execute(
                """INSERT INTO audit (ts, actor, role, ip, ua, action, target, detail, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (time.time(), actor, role, ip[:200], ua[:400], action, target[:200], detail[:2000], source),
            )
            con.commit()

    def audit_list(self, limit: int = 100, offset: int = 0, *, action: str = "", actor: str = "") -> list[dict]:
        clauses, params = [], []
        if action:
            clauses.append("action = ?")
            params.append(action)
        if actor:
            clauses.append("actor = ?")
            params.append(actor)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock, closing(self._connect()) as con:
            rows = con.execute(
                f"SELECT * FROM audit {where} ORDER BY ts DESC LIMIT ? OFFSET ?",
                (*params, max(1, min(limit, 1000)), max(0, offset)),
            ).fetchall()
        return [dict(row) for row in rows]

    def audit_count(self) -> int:
        with self._lock, closing(self._connect()) as con:
            return int(con.execute("SELECT COUNT(*) FROM audit").fetchone()[0])

    def audit_prune(self, days: int) -> int:
        cutoff = time.time() - max(1, days) * 86400
        with self._lock, closing(self._connect()) as con:
            cur = con.execute("DELETE FROM audit WHERE ts < ?", (cutoff,))
            con.commit()
            return int(cur.rowcount or 0)

    # -- tokens ----------------------------------------------------------
    def token_create(self, token_id: str, name: str, role: str, token_hash: str) -> None:
        with self._lock, closing(self._connect()) as con:
            con.execute(
                "INSERT INTO tokens (id, name, role, token_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                (token_id, name[:100], role, token_hash, time.time()),
            )
            con.commit()

    def token_list(self) -> list[dict]:
        with self._lock, closing(self._connect()) as con:
            rows = con.execute(
                "SELECT id, name, role, created_at, last_used_at, revoked_at FROM tokens ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def token_lookup(self, token_hash: str) -> dict | None:
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                "SELECT id, name, role, revoked_at FROM tokens WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            con.execute("UPDATE tokens SET last_used_at = ? WHERE id = ?", (time.time(), row["id"]))
            con.commit()
        return dict(row)

    def token_revoke(self, token_id: str) -> bool:
        with self._lock, closing(self._connect()) as con:
            cur = con.execute("UPDATE tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (time.time(), token_id))
            con.commit()
            return bool(cur.rowcount)

    # -- metrics ---------------------------------------------------------
    def metrics_add(self, sample: dict[str, Any]) -> None:
        with self._lock, closing(self._connect()) as con:
            con.execute(
                """INSERT OR REPLACE INTO metrics (ts, cpu, load_pct, ram, disk, swap, net, docker_running)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    sample["t"], sample.get("cpu"), sample.get("load_pct"), sample.get("ram"),
                    sample.get("disk"), sample.get("swap"), sample.get("net"), sample.get("docker_running"),
                ),
            )
            con.commit()

    def metrics_since(self, since: float, limit: int = 20000) -> list[dict]:
        with self._lock, closing(self._connect()) as con:
            rows = con.execute(
                "SELECT * FROM metrics WHERE ts >= ? ORDER BY ts ASC LIMIT ?",
                (since, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def metrics_prune(self, keep_seconds: int) -> None:
        cutoff = time.time() - keep_seconds
        with self._lock, closing(self._connect()) as con:
            con.execute("DELETE FROM metrics WHERE ts < ?", (cutoff,))
            con.commit()

    # -- incidents -------------------------------------------------------
    def incident_get(self, key: str) -> dict | None:
        with self._lock, closing(self._connect()) as con:
            row = con.execute("SELECT * FROM incidents WHERE key = ?", (key,)).fetchone()
        return dict(row) if row else None

    def incident_upsert(self, key: str, event: str, severity: str, message: str, detail: str = "", *, state: str = "open") -> None:
        now = time.time()
        with self._lock, closing(self._connect()) as con:
            con.execute(
                """INSERT INTO incidents (key, event, severity, state, first_ts, last_ts, message, detail)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                     event=excluded.event, severity=excluded.severity, state=excluded.state,
                     last_ts=excluded.last_ts, message=excluded.message, detail=excluded.detail""",
                (key, event, severity, state, now, now, message[:500], detail[:2000]),
            )
            con.commit()

    def incident_mark_notified(self, key: str) -> None:
        with self._lock, closing(self._connect()) as con:
            con.execute("UPDATE incidents SET last_notified_ts = ? WHERE key = ?", (time.time(), key))
            con.commit()

    def incidents(self, limit: int = 100) -> list[dict]:
        with self._lock, closing(self._connect()) as con:
            rows = con.execute("SELECT * FROM incidents ORDER BY last_ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    # -- health ----------------------------------------------------------
    def health_add(self, payload: dict[str, Any]) -> None:
        with self._lock, closing(self._connect()) as con:
            con.execute(
                "INSERT OR REPLACE INTO health (ts, payload) VALUES (?, ?)",
                (time.time(), json.dumps(payload, separators=(",", ":"))),
            )
            con.commit()

    def health_since(self, since: float) -> list[dict]:
        with self._lock, closing(self._connect()) as con:
            rows = con.execute("SELECT ts, payload FROM health WHERE ts >= ? ORDER BY ts ASC", (since,)).fetchall()
        out = []
        for row in rows:
            try:
                out.append({"t": row["ts"], **json.loads(row["payload"])})
            except (TypeError, ValueError):
                continue
        return out

    def health_prune(self, days: int) -> int:
        cutoff = time.time() - max(1, days) * 86400
        with self._lock, closing(self._connect()) as con:
            cur = con.execute("DELETE FROM health WHERE ts < ?", (cutoff,))
            con.commit()
            return int(cur.rowcount or 0)

    # -- meta ------------------------------------------------------------
    def meta_get(self, key: str, default: str = "") -> str:
        with self._lock, closing(self._connect()) as con:
            row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def meta_set(self, key: str, value: str) -> None:
        with self._lock, closing(self._connect()) as con:
            con.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            con.commit()
