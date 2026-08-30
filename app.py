#!/usr/bin/env python3
"""agentcontrol — lightweight panel to start/stop Cursor agent workers."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import secrets
import signal
import shutil
import socket
import subprocess
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import urlopen

import yaml
from flask import Flask, jsonify, render_template, request

IGNORE_MARKER = ".agentcontrol-ignore"
WORKER_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
CURSOR_AGENTS_URL = "https://cursor.com/agents#workerId={worker_id}"
MGMT_PORT_BASE = 32000
MGMT_PORT_RANGE = 800

CONFIG_SEARCH = [
    Path("/etc/agentcontrol/config.yaml"),
    Path(__file__).resolve().parent / "config.yaml",
    Path(__file__).resolve().parent / "config.example.yaml",
]


CONFIG_FILE: Path | None = None


def load_config() -> dict:
    global CONFIG_FILE
    for path in CONFIG_SEARCH:
        if path.is_file():
            with open(path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            CONFIG_FILE = path
            break
    else:
        cfg = {}
        CONFIG_FILE = CONFIG_SEARCH[0]

    cfg.setdefault("port", int(os.environ.get("PORT", 30228)))
    cfg.setdefault("scan_root", os.environ.get("SCAN_ROOT", "/root"))
    cfg.setdefault("idle_release_seconds", int(os.environ.get("IDLE_RELEASE_SECONDS", 43200)))
    cfg.setdefault("api_key_file", os.environ.get("API_KEY_FILE", "/etc/agentcontrol/api-key"))
    cfg.setdefault("exclude_prefixes", ["."])
    cfg.setdefault("exclude_dirs", [])
    cfg.setdefault("state_dir", os.environ.get("STATE_DIR", "/var/lib/agentcontrol"))
    cfg.setdefault("agent_bin", os.environ.get("AGENT_BIN", ""))
    cfg.setdefault("worker_ready_timeout", int(os.environ.get("WORKER_READY_TIMEOUT", 45)))
    cfg.setdefault("auth_password_file", os.environ.get("AUTH_PASSWORD_FILE", "/etc/agentcontrol/auth-password"))
    cfg.setdefault("default_model", os.environ.get("DEFAULT_MODEL", ""))

    if os.environ.get("PORT"):
        cfg["port"] = int(os.environ["PORT"])

    return cfg


def save_config_patch(updates: dict) -> None:
    path = CONFIG_FILE or CONFIG_SEARCH[0]
    current: dict = {}
    if path.is_file():
        with open(path, encoding="utf-8") as f:
            current = yaml.safe_load(f) or {}
    current.update(updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(current, f, default_flow_style=False, sort_keys=False)
    CFG.update(updates)


CFG = load_config()
STATE_DIR = Path(CFG["state_dir"])
STATE_FILE = STATE_DIR / "workers.json"
METRICS_HISTORY: deque = deque(maxlen=1800)
_CPU_CACHE = {"percent": 0.0, "cores": 1, "at": 0.0}
_DOCKER_CACHE = {"data": None, "at": 0.0}
_NET_CACHE = {"rx": 0, "tx": 0, "at": 0.0}
app = Flask(__name__)


def read_panel_password() -> str | None:
    path = Path(CFG["auth_password_file"])
    if path.is_file():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    return None


def auth_enabled() -> bool:
    return bool(read_panel_password())


def extract_auth_token() -> str:
    header = request.headers.get("X-AgentControl-Auth", "")
    if header:
        return header.strip()
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def password_ok(candidate: str) -> bool:
    expected = read_panel_password()
    if not expected:
        return True
    return secrets.compare_digest(candidate, expected)


def write_panel_password(password: str) -> None:
    path = Path(CFG["auth_password_file"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(password.strip(), encoding="utf-8")
    os.chmod(path, 0o600)


def write_api_key(key: str) -> None:
    path = Path(CFG["api_key_file"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key.strip(), encoding="utf-8")
    os.chmod(path, 0o600)


def api_key_configured() -> bool:
    return bool(read_api_key())


@app.before_request
def require_panel_auth():
    open_endpoints = {
        "health",
        "index",
        "api_auth_login",
        "api_auth_status",
        "api_setup_status",
        "api_setup_password",
    }
    if request.endpoint in open_endpoints:
        return None
    if request.endpoint == "api_setup_api_key" and not api_key_configured():
        return None
    if not request.path.startswith("/api/"):
        return None
    if not auth_enabled():
        return jsonify({"error": "setup_required", "needs_password": True}), 403
    if password_ok(extract_auth_token()):
        return None
    return jsonify({"error": "unauthorized"}), 401


def worker_id_for(folder: str) -> str:
    return str(uuid.uuid5(WORKER_NAMESPACE, f"agentcontrol:{folder}"))


def worker_name(folder: str) -> str:
    return f"agentcontrol-{folder}"


def agent_url(folder: str) -> str:
    worker_id = worker_id_for(folder)
    model = (CFG.get("default_model") or "").strip()
    if model:
        return f"https://cursor.com/agents?model={quote(model)}#workerId={worker_id}"
    return CURSOR_AGENTS_URL.format(worker_id=worker_id)


def list_agent_models() -> list[dict]:
    agent_bin = find_agent_bin()
    if not agent_bin:
        return []
    env = os.environ.copy()
    api_key = read_api_key()
    if api_key:
        env["CURSOR_API_KEY"] = api_key
    try:
        result = subprocess.run(
            [agent_bin, "--list-models"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    models: list[dict] = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line or line.lower().startswith("available models"):
            continue
        if " - " in line:
            model_id, label = line.split(" - ", 1)
        else:
            model_id, label = line, line
        models.append({"id": model_id.strip(), "label": label.strip()})
    return models


def mgmt_port(name: str) -> int:
    digest = hashlib.md5(name.encode(), usedforsecurity=False).hexdigest()
    return MGMT_PORT_BASE + (int(digest[:4], 16) % MGMT_PORT_RANGE)


def find_agent_bin() -> str | None:
    if CFG.get("agent_bin"):
        p = Path(CFG["agent_bin"])
        if p.is_file():
            return str(p)
    found = shutil.which("agent")
    if found:
        return found
    for candidate in (
        Path.home() / ".local/bin/agent",
        Path("/root/.local/bin/agent"),
        Path("/usr/local/bin/agent"),
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def relative_time_en(ts: float) -> str:
    delta = max(0, time.time() - ts)
    if delta < 45:
        return "just now"
    if delta < 3600:
        m = int(delta / 60)
        return f"{m}m ago"
    if delta < 86400:
        h = int(delta / 3600)
        return f"{h}h ago"
    if delta < 2592000:
        d = int(delta / 86400)
        return f"{d}d ago"
    if delta < 31536000:
        mo = int(delta / 2592000)
        return f"{mo}mo ago"
    y = int(delta / 31536000)
    return f"{y}y ago"


def read_api_key() -> str | None:
    path = Path(CFG["api_key_file"])
    if not path.is_file():
        return None
    key = path.read_text(encoding="utf-8").strip()
    return key or None


def fmt_bytes(num: int | float) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(num)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def fmt_duration_en(seconds: float) -> str:
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def read_os_name() -> str:
    try:
        with open("/etc/os-release", encoding="utf-8") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.platform()


def primary_ip() -> str:
    try:
        result = subprocess.run(
            ["hostname", "-I"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        ip = result.stdout.strip().split()
        if ip:
            return ip[0]
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "—"


def cpu_usage_percent(sample_seconds: float = 0.25) -> tuple[float, int]:
    now = time.time()
    if now - _CPU_CACHE["at"] < 1.5 and _CPU_CACHE["at"] > 0:
        return _CPU_CACHE["percent"], _CPU_CACHE["cores"]

    def read_cpu() -> tuple[int, int]:
        with open("/proc/stat", encoding="utf-8") as f:
            parts = f.readline().split()[1:]
        values = [int(v) for v in parts]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        return total, idle

    total1, idle1 = read_cpu()
    time.sleep(sample_seconds)
    total2, idle2 = read_cpu()
    total_delta = total2 - total1
    idle_delta = idle2 - idle1
    cores = os.cpu_count() or 1
    if total_delta <= 0:
        percent = _CPU_CACHE["percent"]
    else:
        percent = round(max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100)), 1)
    _CPU_CACHE.update({"percent": percent, "cores": cores, "at": time.time()})
    return percent, cores


def memory_stats() -> dict:
    data: dict[str, int] = {}
    with open("/proc/meminfo", encoding="utf-8") as f:
        for line in f:
            key, value = line.split(":", 1)
            data[key] = int(value.split()[0]) * 1024
    total = data.get("MemTotal", 0)
    available = data.get("MemAvailable", data.get("MemFree", 0))
    used = max(0, total - available)
    swap_total = data.get("SwapTotal", 0)
    swap_free = data.get("SwapFree", 0)
    swap_used = max(0, swap_total - swap_free)
    return {
        "total": total,
        "used": used,
        "percent": round((used / total) * 100, 1) if total else 0,
        "swap_total": swap_total,
        "swap_used": swap_used,
        "swap_percent": round((swap_used / swap_total) * 100, 1) if swap_total else 0,
    }


def disk_stats(path: str) -> dict:
    usage = shutil.disk_usage(path)
    percent = round((usage.used / usage.total) * 100, 1) if usage.total else 0
    return {
        "path": path,
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
        "percent": percent,
    }


def load_average() -> list[float]:
    with open("/proc/loadavg", encoding="utf-8") as f:
        return [float(x) for x in f.read().split()[:3]]


def load_percent(load_1: float, cores: int) -> float:
    if cores <= 0:
        return 0.0
    return round(min(999.0, (load_1 / cores) * 100), 1)


def network_stats() -> dict:
    now = time.time()
    rx_total = tx_total = 0
    with open("/proc/net/dev", encoding="utf-8") as f:
        for line in f.readlines()[2:]:
            parts = line.split()
            if len(parts) < 10:
                continue
            iface = parts[0].rstrip(":")
            if iface == "lo":
                continue
            rx_total += int(parts[1])
            tx_total += int(parts[9])

    rx_rate = tx_rate = 0.0
    if _NET_CACHE["at"] > 0:
        dt = now - _NET_CACHE["at"]
        if dt > 0:
            rx_rate = max(0.0, (rx_total - _NET_CACHE["rx"]) / dt)
            tx_rate = max(0.0, (tx_total - _NET_CACHE["tx"]) / dt)

    _NET_CACHE.update({"rx": rx_total, "tx": tx_total, "at": now})
    total_rate = rx_rate + tx_rate
    return {
        "rx_bytes_total": rx_total,
        "tx_bytes_total": tx_total,
        "rx_rate": rx_rate,
        "tx_rate": tx_rate,
        "total_rate": total_rate,
        "rx_rate_human": fmt_bytes(rx_rate) + "/s",
        "tx_rate_human": fmt_bytes(tx_rate) + "/s",
        "total_rate_human": fmt_bytes(total_rate) + "/s",
    }


def parse_docker_percent(value: str) -> float:
    try:
        return float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return 0.0


def parse_docker_size_bytes(text: str) -> int:
    text = str(text).strip().upper()
    match = re.match(r"([\d.]+)\s*([A-Z]+)", text)
    if not match:
        return 0
    value = float(match.group(1))
    unit = match.group(2)
    multipliers = {
        "B": 1,
        "KB": 1000,
        "KIB": 1024,
        "MB": 1000**2,
        "MIB": 1024**2,
        "GB": 1000**3,
        "GIB": 1024**3,
        "TB": 1000**4,
        "TIB": 1024**4,
    }
    return int(value * multipliers.get(unit, 1))


def parse_docker_mem_used_bytes(mem_usage: str) -> int:
    if not mem_usage or mem_usage == "—":
        return 0
    return parse_docker_size_bytes(mem_usage.split("/")[0].strip())


def docker_inspect_labels() -> dict[str, dict]:
    if not shutil.which("docker"):
        return {}
    try:
        result = subprocess.run(
            [
                "docker",
                "ps",
                "-q",
                "--format",
                "{{.ID}}",
            ],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
        ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not ids:
            return {}
        inspect = subprocess.run(
            [
                "docker",
                "inspect",
                *ids,
                "--format",
                "{{.Name}}|{{index .Config.Labels \"com.docker.compose.project\"}}|{{index .Config.Labels \"com.docker.compose.service\"}}",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}

    labels: dict[str, dict] = {}
    for line in inspect.stdout.splitlines():
        parts = line.strip().split("|", 2)
        if not parts:
            continue
        name = parts[0].lstrip("/")
        labels[name] = {
            "compose_project": parts[1] if len(parts) > 1 and parts[1] else None,
            "compose_service": parts[2] if len(parts) > 2 and parts[2] else None,
        }
    return labels


def enrich_container_shares(containers: list[dict]) -> tuple[dict, list[dict]]:
    total_cpu = sum(c["cpu_percent"] for c in containers)
    total_mem = sum(c["mem_bytes"] for c in containers)
    totals = {
        "cpu_percent": round(total_cpu, 2),
        "mem_bytes": total_mem,
        "mem_human": fmt_bytes(total_mem),
    }
    for container in containers:
        container["cpu_share"] = round((container["cpu_percent"] / total_cpu) * 100, 1) if total_cpu > 0 else 0.0
        container["mem_share"] = round((container["mem_bytes"] / total_mem) * 100, 1) if total_mem > 0 else 0.0
    return totals, containers


def docker_grouped_stats() -> dict:
    if not shutil.which("docker"):
        return {"containers": [], "groups": [], "totals": {"cpu_percent": 0, "mem_bytes": 0, "mem_human": "0 B"}}

    labels = docker_inspect_labels()
    try:
        result = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {"containers": [], "groups": [], "totals": {"cpu_percent": 0, "mem_bytes": 0, "mem_human": "0 B"}}

    containers: list[dict] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = item.get("Name") or item.get("Container") or "unknown"
        meta = labels.get(name, {})
        mem_usage = item.get("MemUsage") or "—"
        containers.append(
            {
                "name": name,
                "service": meta.get("compose_service") or name,
                "compose_project": meta.get("compose_project"),
                "cpu_percent": parse_docker_percent(item.get("CPUPerc", "0")),
                "mem_percent": parse_docker_percent(item.get("MemPerc", "0")),
                "mem_bytes": parse_docker_mem_used_bytes(mem_usage),
                "mem_usage": mem_usage,
                "net_io": item.get("NetIO") or "—",
            }
        )

    totals, containers = enrich_container_shares(containers)
    grouped: dict[str, dict] = {}
    for container in containers:
        project = container.get("compose_project")
        if project:
            group_id = f"compose:{project}"
            group_type = "compose"
            group_label = project
        else:
            group_id = f"standalone:{container['name']}"
            group_type = "standalone"
            group_label = container["name"]

        if group_id not in grouped:
            grouped[group_id] = {
                "id": group_id,
                "label": group_label,
                "type": group_type,
                "containers": [],
                "cpu_percent": 0.0,
                "mem_bytes": 0,
            }
        group = grouped[group_id]
        group["containers"].append(container)
        group["cpu_percent"] += container["cpu_percent"]
        group["mem_bytes"] += container["mem_bytes"]

    groups: list[dict] = []
    for group in grouped.values():
        group["cpu_percent"] = round(group["cpu_percent"], 2)
        group["mem_human"] = fmt_bytes(group["mem_bytes"])
        group["container_count"] = len(group["containers"])
        group["cpu_share"] = round((group["cpu_percent"] / totals["cpu_percent"]) * 100, 1) if totals["cpu_percent"] > 0 else 0.0
        group["mem_share"] = round((group["mem_bytes"] / totals["mem_bytes"]) * 100, 1) if totals["mem_bytes"] > 0 else 0.0
        group["containers"].sort(key=lambda c: (c["cpu_percent"], c["mem_bytes"]), reverse=True)
        groups.append(group)

    groups.sort(key=lambda g: (g["cpu_percent"], g["mem_bytes"]), reverse=True)
    containers.sort(key=lambda c: (c["cpu_percent"], c["mem_bytes"]), reverse=True)
    return {"containers": containers, "groups": groups, "totals": totals}


def docker_container_stats() -> list[dict]:
    return docker_grouped_stats()["containers"]


def uptime_seconds() -> float:
    with open("/proc/uptime", encoding="utf-8") as f:
        return float(f.read().split()[0])


def agent_version() -> str | None:
    agent_bin = find_agent_bin()
    if not agent_bin:
        return None
    try:
        result = subprocess.run(
            [agent_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        return (result.stdout or result.stderr).strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def docker_stats() -> dict:
    now = time.time()
    if _DOCKER_CACHE["data"] is not None and now - _DOCKER_CACHE["at"] < 10:
        return _DOCKER_CACHE["data"]
    if not shutil.which("docker"):
        return {"available": False}
    try:
        running = subprocess.run(
            ["docker", "ps", "-q"],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
        all_ps = subprocess.run(
            ["docker", "ps", "-aq"],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
        version = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        running_n = len([x for x in running.stdout.splitlines() if x.strip()])
        total_n = len([x for x in all_ps.stdout.splitlines() if x.strip()])
        grouped = docker_grouped_stats()
        data = {
            "available": True,
            "version": (version.stdout or "").strip() or None,
            "running": running_n,
            "total": total_n,
            "stopped": max(0, total_n - running_n),
            "storage": [],
            "containers": grouped["containers"],
            "groups": grouped["groups"],
            "totals": grouped["totals"],
        }
        _DOCKER_CACHE.update({"data": data, "at": now})
        return data
    except (OSError, subprocess.SubprocessError):
        return {
            "available": True,
            "running": None,
            "total": None,
            "stopped": None,
            "storage": [],
            "containers": [],
            "groups": [],
            "totals": {"cpu_percent": 0, "mem_bytes": 0, "mem_human": "0 B"},
        }


def record_metrics(snapshot: dict) -> None:
    docker = snapshot.get("docker") or {}
    network = snapshot.get("network") or {}
    swap_pct = snapshot["memory"].get("swap_percent")
    METRICS_HISTORY.append(
        {
            "t": time.time(),
            "cpu": snapshot["cpu"]["percent"],
            "load_pct": snapshot["cpu"]["load_percent"],
            "ram": snapshot["memory"]["percent"],
            "disk": snapshot["disk_root"]["percent"],
            "swap": swap_pct if swap_pct is not None else 0,
            "net": network.get("total_rate") or 0,
            "docker_running": docker.get("running") or 0,
        }
    )


def collect_system_info() -> dict:
    mem = memory_stats()
    root_disk = disk_stats("/")
    scan_root = CFG["scan_root"]
    scan_disk = disk_stats(scan_root) if scan_root != "/" else None
    state = reconcile_state()
    folders = list_folders()
    running_workers = [name for name, info in state.items() if info.get("pid") and is_running(info["pid"])]
    loads = load_average()
    cpu_percent, cpu_cores = cpu_usage_percent()
    net = network_stats()

    result = {
        "hostname": socket.gethostname(),
        "ip": primary_ip(),
        "os": read_os_name(),
        "kernel": platform.release(),
        "uptime_seconds": uptime_seconds(),
        "uptime_human": fmt_duration_en(uptime_seconds()),
        "cpu": {
            "percent": cpu_percent,
            "cores": cpu_cores,
            "load_1": loads[0],
            "load_5": loads[1],
            "load_15": loads[2],
            "load_percent": load_percent(loads[0], cpu_cores),
        },
        "memory": {
            "used": mem["used"],
            "total": mem["total"],
            "percent": mem["percent"],
            "used_human": fmt_bytes(mem["used"]),
            "total_human": fmt_bytes(mem["total"]),
            "swap_used_human": fmt_bytes(mem["swap_used"]) if mem["swap_total"] else None,
            "swap_percent": mem["swap_percent"] if mem["swap_total"] else None,
        },
        "disk_root": {
            **root_disk,
            "used_human": fmt_bytes(root_disk["used"]),
            "total_human": fmt_bytes(root_disk["total"]),
            "free_human": fmt_bytes(root_disk["free"]),
        },
        "disk_scan_root": (
            {
                **scan_disk,
                "used_human": fmt_bytes(scan_disk["used"]),
                "total_human": fmt_bytes(scan_disk["total"]),
                "free_human": fmt_bytes(scan_disk["free"]),
            }
            if scan_disk
            else None
        ),
        "agent": {
            "installed": bool(find_agent_bin()),
            "path": find_agent_bin(),
            "version": agent_version(),
            "api_key_set": bool(read_api_key()),
        },
        "panel": {
            "port": CFG["port"],
            "scan_root": CFG["scan_root"],
            "idle_hours": round(CFG["idle_release_seconds"] / 3600, 1),
            "project_count": len(folders),
            "running_workers": len(running_workers),
            "running_worker_names": running_workers,
        },
        "docker": docker_stats(),
        "network": net,
        "history_points": len(METRICS_HISTORY),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    record_metrics(result)
    return result


def ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def load_state() -> dict:
    ensure_state_dir()
    if not STATE_FILE.is_file():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict) -> None:
    ensure_state_dir()
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def reconcile_state() -> dict:
    state = load_state()
    changed = False
    for name, info in list(state.items()):
        pid = info.get("pid")
        if not pid or not is_running(pid):
            del state[name]
            changed = True
    if changed:
        save_state(state)
    return state


def is_folder_ignored(path: Path) -> bool:
    return (path / IGNORE_MARKER).is_file()


def is_excluded(name: str) -> bool:
    if name in CFG.get("exclude_dirs", []):
        return True
    for prefix in CFG.get("exclude_prefixes", []):
        if name.startswith(prefix):
            return True
    return False


def folder_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def list_folders() -> list[dict]:
    root = Path(CFG["scan_root"])
    if not root.is_dir():
        return []

    state = reconcile_state()
    folders = []
    for entry in root.iterdir():
        if not entry.is_dir() or is_excluded(entry.name) or is_folder_ignored(entry):
            continue
        name = entry.name
        info = state.get(name, {})
        pid = info.get("pid")
        running = bool(pid and is_running(pid))
        mtime = folder_mtime(entry)
        folders.append(
            {
                "name": name,
                "path": str(entry),
                "running": running,
                "worker_id": worker_id_for(name),
                "agent_url": agent_url(name),
                "started_at": info.get("started_at"),
                "pid": pid if running else None,
                "mtime": mtime,
                "mtime_relative": relative_time_en(mtime),
            }
        )

    folders.sort(key=lambda item: item["mtime"], reverse=True)
    return folders


def folder_path(name: str) -> Path | None:
    if "/" in name or name in (".", "..") or is_excluded(name):
        return None
    path = Path(CFG["scan_root"]) / name
    if path.is_dir():
        return path
    return None


def worker_ready(name: str) -> bool:
    state = reconcile_state()
    info = state.get(name)
    if not info:
        return False
    pid = info.get("pid")
    if not pid or not is_running(pid):
        return False

    port = info.get("mgmt_port")
    if port:
        try:
            with urlopen(f"http://127.0.0.1:{port}/readyz", timeout=1) as resp:
                return resp.status == 200
        except (URLError, OSError, ValueError):
            pass

    log_file = info.get("log_file")
    if log_file and Path(log_file).is_file():
        try:
            tail = Path(log_file).read_text(encoding="utf-8", errors="ignore")[-4000:]
            markers = ("connected", "Connected", "workerId", "bridge", "ready")
            if any(marker in tail for marker in markers) and "error" not in tail.lower()[-500:]:
                return True
        except OSError:
            pass

    started_at = info.get("started_at")
    if started_at:
        try:
            started = datetime.fromisoformat(started_at)
            age = (datetime.now(timezone.utc) - started).total_seconds()
            return age >= 8
        except ValueError:
            pass
    return False


def start_worker(name: str) -> tuple[dict, int]:
    path = folder_path(name)
    if not path:
        return {"error": "folder not found"}, 404

    agent_bin = find_agent_bin()
    if not agent_bin:
        return {"error": "agent CLI not found — run install.sh again"}, 500

    api_key = read_api_key()
    if not api_key:
        return {
            "error": "Cursor API key missing. Set it: echo YOUR_KEY | sudo tee /etc/agentcontrol/api-key"
        }, 500

    state = reconcile_state()
    existing = state.get(name)
    if existing and existing.get("pid") and is_running(existing["pid"]):
        return {
            "status": "already_running",
            "name": name,
            "worker_id": worker_id_for(name),
            "agent_url": agent_url(name),
            "ready": worker_ready(name),
            "pid": existing["pid"],
        }, 200

    try:
        os.utime(path, None)
    except OSError:
        pass

    worker_id = worker_id_for(name)
    port = mgmt_port(name)
    env = os.environ.copy()
    env["CURSOR_AGENT_WORKER_ID"] = worker_id
    env["CURSOR_API_KEY"] = api_key
    env["PATH"] = "/root/.local/bin:" + env.get("PATH", "")
    default_model = (CFG.get("default_model") or "").strip()
    if default_model:
        env["CURSOR_MODEL"] = default_model

    log_file = STATE_DIR / f"{name}.log"
    cmd = [
        agent_bin,
        "worker",
        "--worker-dir",
        str(path),
        "--name",
        worker_name(name),
        "--idle-release-timeout",
        str(CFG["idle_release_seconds"]),
        "--management-addr",
        f"127.0.0.1:{port}",
        "start",
        "--verbose",
    ]

    try:
        with open(log_file, "ab") as log:
            proc = subprocess.Popen(
                cmd,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
    except OSError as exc:
        return {"error": str(exc)}, 500

    state[name] = {
        "pid": proc.pid,
        "worker_id": worker_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "log_file": str(log_file),
        "mgmt_port": port,
    }
    save_state(state)

    return {
        "status": "started",
        "name": name,
        "worker_id": worker_id,
        "agent_url": agent_url(name),
        "ready": False,
        "pid": proc.pid,
    }, 200


def stop_worker(name: str) -> tuple[dict, int]:
    if not folder_path(name):
        return {"error": "folder not found"}, 404

    state = reconcile_state()
    info = state.get(name)
    if not info or not info.get("pid"):
        return {"status": "not_running", "name": name}, 200

    pid = info["pid"]
    if is_running(pid):
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except ProcessLookupError:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    del state[name]
    save_state(state)
    return {"status": "stopped", "name": name, "pid": pid}, 200


@app.get("/")
def index():
    return render_template("index.html", port=CFG["port"], scan_root=CFG["scan_root"])


@app.post("/api/auth/login")
def api_auth_login():
    if not auth_enabled():
        return jsonify({"ok": True, "auth_required": False})
    data = request.get_json(silent=True) or {}
    password = str(data.get("password", ""))
    if password_ok(password):
        return jsonify({"ok": True, "auth_required": True})
    return jsonify({"error": "wrong password", "auth_required": True}), 401


@app.get("/api/auth/status")
def api_auth_status():
    return jsonify(
        {
            "auth_required": auth_enabled(),
            "api_key_set": api_key_configured(),
            "needs_password": not auth_enabled(),
            "needs_api_key": not api_key_configured(),
        }
    )


@app.get("/api/setup/status")
def api_setup_status():
    return jsonify(
        {
            "needs_password": not auth_enabled(),
            "needs_api_key": not api_key_configured(),
            "ready": auth_enabled() and api_key_configured(),
        }
    )


@app.post("/api/setup/password")
def api_setup_password():
    if auth_enabled():
        return jsonify({"error": "password already configured"}), 400
    data = request.get_json(silent=True) or {}
    password = str(data.get("password", "")).strip()
    confirm = str(data.get("confirm", "")).strip()
    if len(password) < 4:
        return jsonify({"error": "password must be at least 4 characters"}), 400
    if password != confirm:
        return jsonify({"error": "passwords do not match"}), 400
    write_panel_password(password)
    return jsonify({"ok": True})


@app.post("/api/setup/api-key")
def api_setup_api_key():
    data = request.get_json(silent=True) or {}
    api_key = str(data.get("api_key", "")).strip()
    if not api_key:
        return jsonify({"error": "api_key required"}), 400
    if api_key_configured():
        if not auth_enabled() or not password_ok(extract_auth_token()):
            return jsonify({"error": "unauthorized"}), 401
    write_api_key(api_key)
    return jsonify({"ok": True})


@app.get("/api/models")
def api_models():
    return jsonify({"models": list_agent_models()})


@app.get("/api/settings")
def api_settings():
    return jsonify(
        {
            "default_model": CFG.get("default_model") or "",
            "idle_hours": round(CFG["idle_release_seconds"] / 3600, 1),
            "scan_root": CFG["scan_root"],
            "port": CFG["port"],
        }
    )


@app.post("/api/settings/model")
def api_settings_model():
    data = request.get_json(silent=True) or {}
    model = str(data.get("model", "")).strip()
    save_config_patch({"default_model": model})
    return jsonify({"ok": True, "default_model": model})


@app.get("/api/folders")
def api_folders():
    folders = list_folders()
    for item in folders:
        if item["running"]:
            item["ready"] = worker_ready(item["name"])
    return jsonify({"folders": folders, "scan_root": CFG["scan_root"]})


@app.post("/api/start/<name>")
def api_start(name: str):
    result, code = start_worker(name)
    return jsonify(result), code


@app.post("/api/hide/<name>")
def api_hide(name: str):
    path = folder_path(name)
    if not path:
        return jsonify({"error": "folder not found"}), 404
    state = reconcile_state()
    if name in state:
        stop_worker(name)
    marker = path / IGNORE_MARKER
    marker.write_text("Hidden from AgentControl. Delete this file to show again.\n", encoding="utf-8")
    return jsonify({"status": "hidden", "name": name, "marker": str(marker)})


@app.post("/api/stop/<name>")
def api_stop(name: str):
    result, code = stop_worker(name)
    return jsonify(result), code


@app.get("/api/ready/<name>")
def api_ready(name: str):
    state = reconcile_state()
    info = state.get(name)
    if not info:
        return jsonify({"ready": False, "running": False}), 200
    running = bool(info.get("pid") and is_running(info["pid"]))
    ready = worker_ready(name) if running else False
    log_tail = ""
    log_file = info.get("log_file")
    if log_file and Path(log_file).is_file():
        try:
            log_tail = Path(log_file).read_text(encoding="utf-8", errors="ignore")[-800:]
        except OSError:
            pass
    return jsonify(
        {
            "ready": ready,
            "running": running,
            "agent_url": agent_url(name),
            "log_tail": log_tail,
        }
    )


@app.get("/api/status")
def api_status():
    return jsonify(
        {
            "workers": reconcile_state(),
            "agent_bin": find_agent_bin(),
            "api_key_set": bool(read_api_key()),
            "config": {
                "port": CFG["port"],
                "scan_root": CFG["scan_root"],
                "idle_release_seconds": CFG["idle_release_seconds"],
            },
        }
    )


@app.get("/api/system")
def api_system():
    return jsonify(collect_system_info())


@app.get("/api/system/history")
def api_system_history():
    return jsonify({"points": list(METRICS_HISTORY)})


@app.get("/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    ensure_state_dir()
    app.run(host="0.0.0.0", port=int(CFG["port"]), debug=False)
