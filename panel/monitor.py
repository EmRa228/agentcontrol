"""Background monitor: health, alerts, metrics persistence."""

from __future__ import annotations

import os
import threading
import time
import traceback
from typing import Any, Callable

from . import metrics as metrics_mod


class Monitor:
    def __init__(
        self,
        *,
        store,
        notifier,
        settings,
        snapshot: Callable[[], dict[str, Any]],
        update_check: Callable[[], dict[str, Any]] | None = None,
        logger=None,
    ):
        self.store = store
        self.notifier = notifier
        self.settings = settings
        self.snapshot = snapshot
        self.update_check = update_check
        self.logger = logger
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._sampler = metrics_mod.Sampler(store, settings.metrics_interval_seconds)
        self._known_running: set[str] = set()
        self._expected_stop: dict[str, float] = {}
        self._last_health_save = 0.0
        self._last_update_check = 0.0
        self._last_metrics = 0.0
        self._lock = threading.Lock()

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if os.environ.get("PANEL_DISABLE_BACKGROUND") == "1":
            return
        if self._threads:
            return
        monitor = threading.Thread(target=self._monitor_loop, name="agentcontrol-monitor", daemon=True)
        sampler = threading.Thread(target=self._metrics_loop, name="agentcontrol-metrics", daemon=True)
        self._threads = [monitor, sampler]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stop.set()

    def note_stopped(self, name: str) -> None:
        with self._lock:
            self._expected_stop[name] = time.time() + 30
            self._known_running.discard(name)

    # -- loops -----------------------------------------------------------
    def _metrics_loop(self) -> None:
        while not self._stop.is_set():
            try:
                now = time.time()
                if now - self._last_metrics >= self.settings.metrics_interval_seconds:
                    metrics_mod.record_sample(self.store, self._sampler)
                    self._last_metrics = now
                    self.store.metrics_prune(self.settings.metrics_retention_hours * 3600)
            except Exception:
                self._log("metrics loop", traceback.format_exc())
            self._stop.wait(5)

    def _monitor_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                self._log("monitor loop", traceback.format_exc())
            self._stop.wait(max(10, self.settings.monitor_interval_seconds))

    # -- one pass --------------------------------------------------------
    def run_once(self) -> dict[str, Any]:
        snapshot = self.snapshot()
        now = time.time()
        self._check_health(snapshot)
        self._check_workers(snapshot)
        self._check_update(snapshot, now)
        if now - self._last_health_save >= 300:
            try:
                self.store.health_add({k: v for k, v in snapshot.items() if k != "workers"})
            except Exception:
                pass
            self._last_health_save = now
        return snapshot

    def _check_health(self, snapshot: dict[str, Any]) -> None:
        for check in snapshot.get("checks", []):
            key = check.get("key")
            status = check.get("status")
            if key in {"workers", "update"}:
                continue
            if status in {"warning", "critical"}:
                severity = "critical" if status == "critical" else "warning"
                self.notifier.notify(
                    "disk" if key == "disk" else ("cert" if key == "certificate" else key),
                    severity,
                    f"{key}: {check.get('message')}",
                    f"{snapshot.get('version')} on {snapshot.get('hostname', '')}: {check.get('message')}",
                    key=f"health:{key}",
                    detail={"check": key, "value": check.get("value")},
                )
            elif status == "ok":
                self.notifier.resolve(f"health:{key}", f"{key} recovered", check.get("message", ""))

    def _check_workers(self, snapshot: dict[str, Any]) -> None:
        workers = snapshot.get("workers") or {}
        running = {name for name, info in workers.items() if info.get("running")}
        now = time.time()
        with self._lock:
            for name, until in list(self._expected_stop.items()):
                if until < now:
                    self._expected_stop.pop(name, None)
            stopped = {name for name, info in workers.items() if not info.get("running")}
            missing = (self._known_running - running) - set(self._expected_stop) - stopped
            for name in missing:
                self.notifier.notify(
                    "worker_down",
                    "critical",
                    f"worker stopped: {name}",
                    f"The Cursor worker for {name} is no longer running.",
                    key=f"worker:{name}",
                    detail={"project": name},
                )
            self._known_running = running
        for name in running:
            self.notifier.resolve(f"worker:{name}", f"worker recovered: {name}", f"{name} is running again.")

    def _check_update(self, snapshot: dict[str, Any], now: float) -> None:
        if not self.update_check:
            return
        if now - self._last_update_check < self.settings.update_check_seconds:
            return
        self._last_update_check = now
        try:
            result = self.update_check()
        except Exception as exc:
            self._log("update check", str(exc))
            return
        version = (result.get("remote") or {}).get("version")
        if result.get("update_available") and version:
            self.notifier.notify(
                "update",
                "info",
                f"panel update available: {version}",
                f"Panel {snapshot.get('version')} can update to {version}.",
                key=f"update:{version}",
                detail={"version": version},
            )

    def _log(self, where: str, message: str) -> None:
        if self.logger:
            try:
                self.logger(f"monitor[{where}] {message}")
            except Exception:
                pass
