"""Health snapshot and certificate/disk checks."""

from __future__ import annotations

import shutil
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def disk_usage(path: str) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return {"path": path, "percent": None, "free": None, "total": None}
    return {
        "path": path,
        "percent": round(usage.used / usage.total * 100, 1) if usage.total else 0.0,
        "free": usage.free,
        "total": usage.total,
        "free_gb": round(usage.free / (1024**3), 1),
    }


def certificate_files(state_dir: str | Path, explicit: str = "") -> list[Path]:
    found: list[Path] = []
    if explicit:
        path = Path(explicit)
        if path.is_file():
            found.append(path)
    root = Path(state_dir) / "caddy" / "storage" / "certificates"
    if root.is_dir():
        found.extend(sorted(root.glob("**/*.crt")))
    return found


def certificate_expiry(state_dir: str | Path, explicit: str = "") -> dict[str, Any]:
    files = certificate_files(state_dir, explicit)
    if not files:
        return {"found": False, "days_left": None, "not_after": None, "path": None}
    best = None
    for path in files:
        try:
            info = ssl._ssl._test_decode_cert(str(path))  # type: ignore[attr-defined]
            not_after = info.get("notAfter")
            expiry = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        except (OSError, ValueError, KeyError, AttributeError):
            continue
        days = (expiry - datetime.now(timezone.utc)).total_seconds() / 86400
        candidate = {"found": True, "days_left": round(days, 1), "not_after": expiry.isoformat(), "path": str(path)}
        if best is None or (candidate["days_left"] or 0) < (best["days_left"] or 0):
            best = candidate
    return best or {"found": False, "days_left": None, "not_after": None, "path": None}


def collect_health(
    *,
    state_dir: str | Path,
    version: str,
    uptime_seconds: float,
    workers_running: int,
    workers_total: int,
    opencode_healthy: bool | None,
    cert_file: str = "",
    disk_path: str = "/",
    warn_percent: float = 85.0,
    crit_percent: float = 95.0,
    cert_warn_days: int = 21,
    cert_crit_days: int = 7,
    update_available: bool = False,
    extra: dict | None = None,
) -> dict[str, Any]:
    disk = disk_usage(disk_path)
    cert = certificate_expiry(state_dir, cert_file)
    checks: list[dict[str, Any]] = []
    level = "ok"

    def add(key: str, status: str, message: str, value: Any = None) -> None:
        nonlocal level
        checks.append({"key": key, "status": status, "message": message, "value": value})
        if status == "critical":
            level = "critical"
        elif status == "warning" and level == "ok":
            level = "warning"

    if disk["percent"] is None:
        add("disk", "warning", "disk usage unavailable")
    elif disk["percent"] >= crit_percent:
        add("disk", "critical", f"disk {disk['percent']}% used", disk["percent"])
    elif disk["percent"] >= warn_percent:
        add("disk", "warning", f"disk {disk['percent']}% used", disk["percent"])
    else:
        add("disk", "ok", f"disk {disk['percent']}% used", disk["percent"])

    if not cert["found"]:
        add("certificate", "warning", "no TLS certificate found", None)
    elif cert["days_left"] is not None and cert["days_left"] <= cert_crit_days:
        add("certificate", "critical", f"certificate expires in {cert['days_left']:.0f} days", cert["days_left"])
    elif cert["days_left"] is not None and cert["days_left"] <= cert_warn_days:
        add("certificate", "warning", f"certificate expires in {cert['days_left']:.0f} days", cert["days_left"])
    else:
        add("certificate", "ok", f"certificate valid for {cert['days_left']:.0f} days", cert["days_left"])

    if opencode_healthy is False:
        add("opencode", "warning", "OpenCode serve is not healthy")
    elif opencode_healthy is True:
        add("opencode", "ok", "OpenCode serve is healthy")

    add("workers", "ok", f"{workers_running}/{workers_total} workers running", workers_running)
    if update_available:
        add("update", "info", "panel update available")

    return {
        "status": level,
        "ok": level == "ok",
        "version": version,
        "uptime_seconds": int(uptime_seconds),
        "workers_running": workers_running,
        "workers_total": workers_total,
        "opencode_healthy": opencode_healthy,
        "disk": disk,
        "certificate": cert,
        "update_available": update_available,
        "checks": checks,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        **(extra or {}),
    }
