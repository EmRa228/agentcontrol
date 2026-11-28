"""Persistent metric samples and Prometheus exposition."""

from __future__ import annotations

import os
import time
from typing import Any


def _read_cpu() -> tuple[int, int]:
    with open("/proc/stat", encoding="utf-8") as handle:
        parts = handle.readline().split()[1:]
    values = [int(part) for part in parts]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def _memory() -> dict[str, float]:
    data: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                key, value = line.split(":", 1)
                data[key] = int(value.split()[0]) * 1024
    except (OSError, ValueError):
        return {"percent": 0.0, "swap_percent": 0.0}
    total = data.get("MemTotal", 0)
    available = data.get("MemAvailable", data.get("MemFree", 0))
    used = max(0, total - available)
    swap_total = data.get("SwapTotal", 0)
    swap_free = data.get("SwapFree", 0)
    return {
        "percent": round(used / total * 100, 1) if total else 0.0,
        "swap_percent": round((swap_total - swap_free) / swap_total * 100, 1) if swap_total else 0.0,
    }


def _network() -> tuple[int, int]:
    rx = tx = 0
    try:
        with open("/proc/net/dev", encoding="utf-8") as handle:
            for line in handle.readlines()[2:]:
                name, rest = line.split(":", 1)
                if name.strip() == "lo":
                    continue
                fields = rest.split()
                rx += int(fields[0])
                tx += int(fields[8])
    except (OSError, ValueError, IndexError):
        pass
    return rx, tx


class Sampler:
    def __init__(self, store, interval: int = 15):
        self.store = store
        self.interval = max(5, int(interval))
        self._last = (0, 0, 0.0)
        self._cpu_cache = (0.0, 0.0)

    def sample(self) -> dict[str, Any]:
        total1, idle1 = _read_cpu()
        time.sleep(0.2)
        total2, idle2 = _read_cpu()
        delta_total = total2 - total1
        cpu = 0.0
        if delta_total > 0:
            cpu = round(max(0.0, min(100.0, (1 - (idle2 - idle1) / delta_total) * 100)), 1)
        memory = _memory()
        try:
            import shutil

            usage = shutil.disk_usage("/")
            disk = round(usage.used / usage.total * 100, 1) if usage.total else 0.0
        except OSError:
            disk = 0.0
        rx, tx = _network()
        now = time.time()
        rate = 0.0
        if self._last[2] > 0:
            elapsed = max(0.001, now - self._last[2])
            rate = round(((rx - self._last[0]) + (tx - self._last[1])) / elapsed, 1)
        self._last = (rx, tx, now)
        try:
            with open("/proc/loadavg", encoding="utf-8") as handle:
                load = float(handle.read().split()[0])
        except (OSError, ValueError, IndexError):
            load = 0.0
        cores = os.cpu_count() or 1
        return {
            "t": now,
            "cpu": cpu,
            "load_pct": round(load / cores * 100, 1),
            "ram": memory["percent"],
            "disk": disk,
            "swap": memory["swap_percent"],
            "net": rate,
            "load": load,
            "cores": cores,
            "rx_bytes": rx,
            "tx_bytes": tx,
        }


def record_sample(store, sampler: Sampler) -> dict[str, Any]:
    sample = sampler.sample()
    store.metrics_add(sample)
    return sample


def history(store, seconds: int, *, max_points: int = 2000) -> list[dict]:
    since = time.time() - max(60, min(seconds, 30 * 86400))
    points = store.metrics_since(since)
    if len(points) <= max_points:
        return points
    stride = max(1, len(points) // max_points)
    return points[::stride]


def prometheus_text(*, sample: dict, health: dict, workers_running: int, workers_total: int, version: str, uptime: float) -> str:
    lines = [
        "# HELP agentcontrol_up Panel process is up",
        "# TYPE agentcontrol_up gauge",
        "agentcontrol_up 1",
        "# HELP agentcontrol_info Build information",
        "# TYPE agentcontrol_info gauge",
        f'agentcontrol_info{{version="{version}"}} 1',
        "# HELP agentcontrol_uptime_seconds Process uptime",
        "# TYPE agentcontrol_uptime_seconds gauge",
        f"agentcontrol_uptime_seconds {uptime:.0f}",
        "# HELP agentcontrol_cpu_percent CPU usage percent",
        "# TYPE agentcontrol_cpu_percent gauge",
        f"agentcontrol_cpu_percent {sample.get('cpu', 0)}",
        "# HELP agentcontrol_load_percent Load average as percent of cores",
        "# TYPE agentcontrol_load_percent gauge",
        f"agentcontrol_load_percent {sample.get('load_pct', 0)}",
        "# HELP agentcontrol_memory_percent RAM usage percent",
        "# TYPE agentcontrol_memory_percent gauge",
        f"agentcontrol_memory_percent {sample.get('ram', 0)}",
        "# HELP agentcontrol_disk_percent Disk usage percent",
        "# TYPE agentcontrol_disk_percent gauge",
        f'agentcontrol_disk_percent{{path="/"}} {sample.get("disk", 0)}',
        "# HELP agentcontrol_swap_percent Swap usage percent",
        "# TYPE agentcontrol_swap_percent gauge",
        f"agentcontrol_swap_percent {sample.get('swap', 0)}",
        "# HELP agentcontrol_network_rate_bytes_per_second Aggregate network rate",
        "# TYPE agentcontrol_network_rate_bytes_per_second gauge",
        f"agentcontrol_network_rate_bytes_per_second {sample.get('net', 0)}",
        "# HELP agentcontrol_workers_running Running Cursor workers",
        "# TYPE agentcontrol_workers_running gauge",
        f"agentcontrol_workers_running {workers_running}",
        "# HELP agentcontrol_workers_total Project folders",
        "# TYPE agentcontrol_workers_total gauge",
        f"agentcontrol_workers_total {workers_total}",
        "# HELP agentcontrol_opencode_healthy OpenCode serve health (1/0)",
        "# TYPE agentcontrol_opencode_healthy gauge",
        f"agentcontrol_opencode_healthy {1 if health.get('opencode_healthy') else 0}",
        "# HELP agentcontrol_certificate_days_left Days until the certificate expires",
        "# TYPE agentcontrol_certificate_days_left gauge",
        f"agentcontrol_certificate_days_left {health.get('certificate', {}).get('days_left') or -1}",
        "# HELP agentcontrol_update_available Panel update available (1/0)",
        "# TYPE agentcontrol_update_available gauge",
        f"agentcontrol_update_available {1 if health.get('update_available') else 0}",
    ]
    return "\n".join(lines) + "\n"
