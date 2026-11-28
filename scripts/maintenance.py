#!/usr/bin/env python3
"""Periodic maintenance: retention, backups, build cleanup, disk/cert alerts."""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from panel.config import build_settings  # noqa: E402
from panel.db import Store  # noqa: E402
from panel.health import certificate_expiry, disk_usage  # noqa: E402
from panel.notify import Notifier  # noqa: E402

CONFIG_CANDIDATES = [
    Path(os.environ.get("CONFIG_FILE", "/etc/agentcontrol/config.yaml")),
    ROOT / "config.yaml",
    ROOT / "config.example.yaml",
]
BACKUP_KEEP = int(os.environ.get("BACKUP_KEEP", "7"))
BUILD_KEEP = int(os.environ.get("BUILD_KEEP", "3"))
LOCK = Path(os.environ.get("STATE_DIR", "/var/lib/agentcontrol")) / "maintenance.lock"


def load_settings():
    for path in CONFIG_CANDIDATES:
        if path.is_file():
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            return build_settings(raw), path
    return build_settings({}), CONFIG_CANDIDATES[0]


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    line = f"{stamp} {message}"
    print(line)
    state = Path(os.environ.get("STATE_DIR", "/var/lib/agentcontrol"))
    try:
        state.mkdir(parents=True, exist_ok=True)
        with (state / "maintenance.log").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def take_lock() -> bool:
    try:
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        if LOCK.is_file():
            pid = int(LOCK.read_text(encoding="utf-8").strip() or 0)
            if pid:
                try:
                    os.kill(pid, 0)
                    return False
                except OSError:
                    pass
        LOCK.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except (OSError, ValueError):
        return True


def backup(settings, state_dir: Path) -> Path | None:
    backups = state_dir / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    archive = backups / f"agentcontrol-etc-{stamp}.tar.gz"
    try:
        with tarfile.open(archive, "w:gz") as tar:
            tar.add("/etc/agentcontrol", arcname="agentcontrol")
        archive.chmod(0o600)
    except OSError as exc:
        log(f"backup failed: {exc}")
        archive = None  # type: ignore[assignment]
    for name in ("panel.db", "sessions.db"):
        source = state_dir / name
        if not source.is_file():
            continue
        target = backups / f"{name}.{stamp}.bak"
        try:
            with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
                src.backup(dst)
            target.chmod(0o600)
        except sqlite3.Error as exc:
            log(f"sqlite backup {name} failed: {exc}")
    keep = max(1, BACKUP_KEEP)
    artifacts = sorted(backups.glob("agentcontrol-etc-*.tar.gz")) + sorted(backups.glob("*.bak"))
    for old in artifacts[:-keep * 2]:
        try:
            old.unlink()
        except OSError:
            pass
    return archive


def cleanup_builds(repo: Path) -> int:
    builds = repo / ".opencode-web-builds"
    if not builds.is_dir():
        return 0
    link = repo / "opencode-web"
    current = ""
    try:
        current = os.readlink(link)
    except OSError:
        pass
    directories = sorted((p for p in builds.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    for directory in directories[max(1, BUILD_KEEP):]:
        if current and current.endswith(directory.name):
            continue
        shutil.rmtree(directory, ignore_errors=True)
        removed += 1
    return removed


def alerts(settings, state_dir: Path, store: Store) -> None:
    notifier = Notifier(store, settings.notifications_file)
    disk = disk_usage("/")
    percent = disk.get("percent")
    if percent is not None and percent >= settings.disk_crit_percent:
        notifier.notify("disk", "critical", f"disk {percent}% used", f"Free space: {disk.get('free_gb')} GB", key="health:disk")
    elif percent is not None and percent >= settings.disk_warn_percent:
        notifier.notify("disk", "warning", f"disk {percent}% used", f"Free space: {disk.get('free_gb')} GB", key="health:disk")
    cert = certificate_expiry(state_dir, str(settings.raw.get("tls_cert_file") or ""))
    days = cert.get("days_left")
    if days is not None and days <= settings.cert_crit_days:
        notifier.notify("cert", "critical", f"certificate expires in {days:.0f} days", cert.get("path") or "", key="health:certificate")
    elif days is not None and days <= settings.cert_warn_days:
        notifier.notify("cert", "warning", f"certificate expires in {days:.0f} days", cert.get("path") or "", key="health:certificate")
    # Caddy uses HTTP-01 (TLS-ALPN is disabled for the custom port), so port 80
    # must be free when it renews. Warn while there is still time to fix it.
    if days is not None and days <= 30 and not settings.raw.get("tls_cert_file"):
        import socket

        busy = False
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(1)
                busy = sock.connect_ex(("127.0.0.1", 80)) == 0
        except OSError:
            busy = False
        if busy:
            notifier.notify(
                "cert",
                "critical",
                "ACME renewal may fail: port 80 is busy",
                "Caddy needs port 80 free for the HTTP-01 challenge. Stop the other service or configure a static certificate.",
                key="health:acme-port80",
            )
        else:
            notifier.resolve("health:acme-port80", "ACME port 80 is free", "Caddy can bind port 80 for the next renewal.")


def version_notifications(settings, state_dir: Path, store: Store) -> None:
    """Nightly version check: notify only, never apply."""
    import json
    import subprocess
    from urllib.request import urlopen

    notifier = Notifier(store, settings.notifications_file)
    def newer(a: str, b: str) -> bool:
        def parts(value):
            out = []
            for part in (value or "0").split("."):
                try:
                    out.append(int(part))
                except ValueError:
                    out.append(0)
            return tuple(out)
        return parts(a) > parts(b)

    # Panel
    try:
        with urlopen("https://raw.githubusercontent.com/EmRa228/agentcontrol/main/version.json", timeout=10) as response:
            remote = json.loads(response.read().decode("utf-8"))
        local = json.loads((ROOT / "version.json").read_text(encoding="utf-8"))
        if newer(str(remote.get("version")), str(local.get("version"))):
            notifier.notify(
                "update", "info", f"panel update available: {remote.get('version')}",
                f"Installed {local.get('version')} → {remote.get('version')}",
                key=f"update:panel:{remote.get('version')}",
            )
    except Exception as exc:  # noqa: BLE001
        log(f"panel version check failed: {exc}")

    # OpenCode
    try:
        with urlopen("https://api.github.com/repos/anomalyco/opencode/releases?per_page=30", timeout=10) as response:
            releases = json.loads(response.read().decode("utf-8"))
        candidates = []
        for rel in releases:
            tag = str(rel.get("tag_name") or "").lstrip("v")
            if rel.get("prerelease") or not tag:
                continue
            try:
                candidates.append((tuple(int(x) for x in tag.split(".")), tag))
            except ValueError:
                continue
        latest = max(candidates)[1] if candidates else ""
        pinned = ""
        versions_file = ROOT / "scripts" / "opencode-versions.sh"
        for line in versions_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("opencode_version="):
                pinned = line.split("=", 1)[1].strip()
        if latest and pinned and newer(latest, pinned):
            patch = ROOT / "scripts" / f"opencode-web-{latest}.patch"
            notifier.notify(
                "update", "info", f"OpenCode {latest} available",
                f"Installed {pinned}. " + ("Patch ready." if patch.is_file() else "Frontend patch still required."),
                key=f"update:opencode:{latest}",
            )
    except Exception as exc:  # noqa: BLE001
        log(f"opencode version check failed: {exc}")

    # Agent CLI
    try:
        agent = shutil.which("agent") or "/root/.local/bin/agent"
        if Path(agent).is_file():
            result = subprocess.run([agent, "--version"], capture_output=True, text=True, timeout=10)
            log(f"agent CLI version: {(result.stdout or '').strip()}")
    except Exception as exc:  # noqa: BLE001
        log(f"agent version check failed: {exc}")


def main() -> int:
    if not take_lock():
        log("maintenance already running")
        return 0
    try:
        settings, config_path = load_settings()
        state_dir = Path(settings.state_dir)
        store = Store(state_dir / "panel.db")
        store.initialize()
        log(f"maintenance start config={config_path} state={state_dir}")
        store.metrics_prune(settings.metrics_retention_hours * 3600)
        store.health_prune(30)
        store.audit_prune(90)
        archive = backup(settings, state_dir)
        log(f"backup: {archive}" if archive else "backup: skipped")
        removed = cleanup_builds(ROOT)
        if removed:
            log(f"removed {removed} old OpenCode build(s)")
        alerts(settings, state_dir, store)
        version_notifications(settings, state_dir, store)
        log("maintenance done")
        return 0
    except Exception as exc:  # noqa: BLE001
        log(f"maintenance failed: {exc}")
        return 1
    finally:
        try:
            LOCK.unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
