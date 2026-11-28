"""Route group: ops."""

from flask import Blueprint

from . import _live

bp = Blueprint("ops", __name__)


def install(panel_app):
    _live.install(globals(), panel_app)


@bp.get("/api/audit")
def api_audit_list():
    limit = int(request.args.get("limit", 100))
    offset = int(request.args.get("offset", 0))
    rows = STORE.audit_list(limit=limit, offset=offset, action=request.args.get("action", ""), actor=request.args.get("actor", ""))
    return jsonify({"events": rows, "total": STORE.audit_count(), "limit": limit, "offset": offset})


@bp.get("/api/audit/export.csv")
def api_audit_export():
    import csv
    import io

    rows = STORE.audit_list(limit=1000)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["time", "actor", "role", "ip", "action", "target", "detail", "source"])
    for row in rows:
        writer.writerow([
            datetime.fromtimestamp(row["ts"], timezone.utc).isoformat(),
            row["actor"], row["role"], row["ip"], row["action"], row["target"], row["detail"], row["source"],
        ])
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=agentcontrol-audit.csv"},
    )


@bp.get("/api/tokens")
def api_tokens_list():
    return jsonify({"tokens": STORE.token_list()})


@bp.post("/api/tokens")
def api_tokens_create():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip() or "token"
    role = str(data.get("role", security_mod.ROLE_OPS)).strip()
    if role not in security_mod.ROLES:
        return jsonify({"error": f"role must be one of {', '.join(security_mod.ROLES)}"}), 400
    record = security_mod.create_token(STORE, name, role)
    audit_log.record(STORE, "token.created", target=record["id"], detail={"name": name, "role": role})
    return jsonify(record), 201


@bp.post("/api/tokens/<token_id>/revoke")
def api_tokens_revoke(token_id: str):
    ok = STORE.token_revoke(token_id)
    if ok:
        audit_log.record(STORE, "token.revoked", target=token_id)
    return jsonify({"ok": ok}), (200 if ok else 404)


@bp.get("/api/notifications")
def api_notifications_get():
    return jsonify({"config": NOTIFIER.config().to_dict(redact=True)})


@bp.post("/api/notifications")
def api_notifications_save():
    from panel.config import NotificationConfig, SEVERITIES

    data = request.get_json(silent=True) or {}
    current = NOTIFIER.config()
    severity = str(data.get("min_severity", current.min_severity)).lower()
    if severity not in SEVERITIES:
        return jsonify({"error": "min_severity must be info, warning or critical"}), 400
    try:
        throttle = max(60, int(data.get("throttle_seconds", current.throttle_seconds)))
    except (TypeError, ValueError):
        return jsonify({"error": "throttle_seconds must be a number"}), 400
    events = {str(k): bool(v) for k, v in (data.get("events") or current.events).items()}
    thresholds = dict(current.thresholds)
    thresholds.update({k: float(v) for k, v in (data.get("thresholds") or {}).items() if isinstance(v, (int, float, str))})
    merged = _merge_channels(current, data.get("channels") or {})
    config = NotificationConfig(
        enabled=bool(data.get("enabled", current.enabled)),
        min_severity=severity,
        throttle_seconds=throttle,
        events=events,
        channels=merged,
        thresholds=thresholds,
    )
    save_notifications(config, SETTINGS.notifications_file)
    NOTIFIER.reload()
    audit_log.record(STORE, "notifications.updated", detail=config.to_dict(redact=True))
    return jsonify({"ok": True, "config": config.to_dict(redact=True)})


@bp.post("/api/notifications/test")
def api_notifications_test():
    data = request.get_json(silent=True) or {}
    channel = str(data.get("channel", "")).strip()
    if not channel:
        return jsonify({"error": "channel required"}), 400
    result = NOTIFIER.test_channel(channel)
    return jsonify(result), (200 if result.get("ok") else 400)


@bp.get("/api/health")
def api_health():
    return jsonify(panel_health_snapshot())


@bp.get("/api/health/history")
def api_health_history():
    days = max(1, min(int(request.args.get("days", 7)), 30))
    return jsonify({"points": STORE.health_since(time.time() - days * 86400)})


@bp.get("/api/incidents")
def api_incidents():
    return jsonify({"incidents": STORE.incidents(limit=int(request.args.get("limit", 100)))})


@bp.route("/api/terminal/ws", websocket=True)
def api_terminal_ws():
    if not SETTINGS.terminal_enabled:
        return Response("terminal disabled", status=403)
    if not security_mod.role_allows(getattr(g, "actor_role", ""), security_mod.ROLE_ADMIN):
        return Response("terminal requires an admin role", status=403)
    audit_log.record(STORE, "terminal.opened")
    shell = str(CFG.get("terminal_shell") or "/bin/bash")
    status = terminal_mod.serve(
        request.environ,
        shell=shell,
        cwd=str(CFG.get("scan_root") or "/"),
        max_seconds=int(CFG.get("terminal_max_seconds") or 3600),
        logger=print,
    )
    return Response(status=status)
