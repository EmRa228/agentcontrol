"""Route group: workers."""

from flask import Blueprint

from . import _live

bp = Blueprint("workers", __name__)


def install(panel_app):
    _live.install(globals(), panel_app)


@bp.post("/api/projects/touch/<name>")
def api_projects_touch(name: str):
    path = folder_path(name)
    if not path:
        return jsonify({"error": "folder not found"}), 404
    touched = touch_project(name)
    return jsonify({"ok": True, "name": name, "touched_at": touched})


@bp.get("/api/folders")
def api_folders():
    folders = list_folders()
    return jsonify({"folders": folders, "scan_root": CFG["scan_root"]})


@bp.post("/api/start/<name>")
def api_start(name: str):
    result, code = start_worker(name)
    audit_log.record(STORE, "worker.start", target=name, detail={"status": result.get("status"), "code": code})
    return jsonify(result), code


@bp.post("/api/restart/<name>")
def api_restart(name: str):
    result, code = restart_worker(name)
    audit_log.record(STORE, "worker.restart", target=name, detail={"status": result.get("status"), "code": code})
    return jsonify(result), code


@bp.post("/api/hide/<name>")
def api_hide(name: str):
    path = folder_path(name)
    if not path:
        return jsonify({"error": "folder not found"}), 404
    state = reconcile_state()
    if name in state:
        stop_worker(name)
    marker = path / IGNORE_MARKER
    marker.write_text("Hidden from AgentControl. Delete this file to show again.\n", encoding="utf-8")
    audit_log.record(STORE, "worker.hide", target=name)
    return jsonify({"status": "hidden", "name": name, "marker": str(marker)})


@bp.get("/api/projects/<name>/details")
def api_project_details(name: str):
    path = folder_path(name)
    if not path:
        return jsonify({"error": "folder not found"}), 404
    state = reconcile_state()
    info = state.get(name) or {}
    running = bool(info.get("pid") and is_running(info["pid"]))
    started_at = info.get("started_at")
    uptime = None
    if started_at:
        try:
            uptime = int((datetime.now(timezone.utc) - datetime.fromisoformat(started_at)).total_seconds())
        except ValueError:
            uptime = None
    log_file = STATE_DIR / f"{name}.log"
    size = None
    try:
        size = log_file.stat().st_size
    except OSError:
        pass
    return jsonify(
        {
            "name": name,
            "path": str(path),
            "running": running,
            "ready": worker_ready(name) if running else False,
            "pid": info.get("pid") if running else None,
            "worker_id": worker_id_for(name),
            "agent_url": agent_url(name),
            "started_at": started_at,
            "uptime_seconds": uptime,
            "mgmt_port": info.get("mgmt_port") if running else None,
            "log_file": str(log_file),
            "log_size": size,
            "mtime": folder_mtime(path),
            "git": git_details(path),
            "opencode_path": opencode_panel.opencode_public_path(str(path)),
        }
    )


@bp.get("/api/logs/<name>")
def api_logs(name: str):
    path = folder_path(name)
    if not path:
        return jsonify({"error": "folder not found"}), 404
    log_file = STATE_DIR / f"{name}.log"
    if not log_file.is_file():
        return jsonify({"lines": [], "size": 0, "path": str(log_file), "exists": False})
    try:
        tail_bytes = max(1024, min(int(request.args.get("bytes", 200000)), 2_000_000))
        max_lines = max(10, min(int(request.args.get("tail", 500)), 5000))
    except ValueError:
        tail_bytes, max_lines = 200000, 500
    query = str(request.args.get("q", "")).strip().lower()
    try:
        with log_file.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - tail_bytes))
            data = handle.read().decode("utf-8", errors="replace")
        lines = data.splitlines()
        if query:
            lines = [line for line in lines if query in line.lower()]
        lines = lines[-max_lines:]
        return jsonify(
            {
                "lines": lines,
                "size": size,
                "path": str(log_file),
                "exists": True,
                "mtime": log_file.stat().st_mtime,
            }
        )
    except OSError as exc:
        return jsonify({"error": str(exc)}), 500


@bp.get("/api/logs/<name>/download")
def api_logs_download(name: str):
    path = folder_path(name)
    if not path:
        return jsonify({"error": "folder not found"}), 404
    log_file = STATE_DIR / f"{name}.log"
    if not log_file.is_file():
        return jsonify({"error": "log not found"}), 404
    return send_from_directory(
        log_file.parent,
        log_file.name,
        as_attachment=True,
        download_name=f"{name}.log",
        mimetype="text/plain",
    )


@bp.get("/api/hidden")
def api_hidden():
    root = Path(CFG["scan_root"])
    hidden = []
    if root.is_dir():
        for entry in root.iterdir():
            if not entry.is_dir() or is_excluded(entry.name):
                continue
            marker = entry / IGNORE_MARKER
            if marker.is_file():
                try:
                    hidden.append({"name": entry.name, "hidden_at": marker.stat().st_mtime})
                except OSError:
                    hidden.append({"name": entry.name, "hidden_at": None})
    return jsonify({"hidden": hidden})


@bp.post("/api/unhide/<name>")
def api_unhide(name: str):
    path = folder_path(name)
    if not path:
        return jsonify({"error": "folder not found"}), 404
    marker = path / IGNORE_MARKER
    if not marker.is_file():
        return jsonify({"error": "folder is not hidden"}), 400
    marker.unlink(missing_ok=True)
    audit_log.record(STORE, "worker.unhide", target=name)
    return jsonify({"ok": True, "name": name})


@bp.post("/api/stop/<name>")
def api_stop(name: str):
    result, code = stop_worker(name)
    audit_log.record(STORE, "worker.stop", target=name, detail={"status": result.get("status"), "code": code})
    return jsonify(result), code


@bp.get("/api/status")
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
