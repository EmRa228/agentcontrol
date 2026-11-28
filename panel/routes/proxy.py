"""Route group: proxy."""

from flask import Blueprint

from . import _live

bp = Blueprint("proxy", __name__)


def install(panel_app):
    _live.install(globals(), panel_app)


@bp.get("/api/xray/client")
def api_xray_client_get():
    settings = load_client_settings()
    live = request.args.get("live") == "1"
    return jsonify(build_status_report(settings, live_test=live))


@bp.get("/api/xray/status")
def api_xray_status():
    settings = load_client_settings()
    live = request.args.get("live") == "1"
    return jsonify(build_status_report(settings, live_test=live))


@bp.post("/api/xray/client/toggle")
def api_xray_client_toggle():
    data = request.get_json(silent=True) or {}
    if "enabled" not in data:
        return jsonify({"error": "enabled is required"}), 400
    enabled = bool(data.get("enabled"))
    try:
        result = set_proxy_enabled(enabled, restart=bool(data.get("restart", enabled)))
    except (ValueError, FileNotFoundError) as exc:
        return jsonify({"error": str(exc)}), 400
    except OSError as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify(result)


@bp.post("/api/xray/client")
def api_xray_client_save():
    data = request.get_json(silent=True) or {}
    current = load_client_settings()
    if data.get("share_url") or data.get("vless_url"):
        share_url = str(data.get("share_url") or data.get("vless_url"))
        try:
            current = merge_share_url(current, share_url)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
    updates = {
        "enabled": bool(data.get("enabled", current.get("enabled", True))),
        "proxy_port": int(data.get("proxy_port", current.get("proxy_port", 30229))),
        "proxy_listen": str(data.get("proxy_listen", current.get("proxy_listen", "127.0.0.1"))),
        "protocol": str(data.get("protocol", current.get("protocol", "vless"))).strip(),
        "address": str(data.get("address", current.get("address", ""))).strip(),
        "port": int(data.get("port", current.get("port", 443))),
        "uuid": str(data.get("uuid", current.get("uuid", ""))).strip(),
        "password": str(data.get("password", current.get("password", ""))).strip(),
        "flow": str(data.get("flow", current.get("flow", ""))).strip(),
        "network": str(data.get("network", current.get("network", "tcp"))).strip() or "tcp",
        "security": str(data.get("security", current.get("security", "none"))).strip() or "none",
        "server_name": str(data.get("server_name", current.get("server_name", ""))).strip(),
        "fingerprint": str(data.get("fingerprint", current.get("fingerprint", "chrome"))).strip(),
        "public_key": str(data.get("public_key", current.get("public_key", ""))).strip(),
        "short_id": str(data.get("short_id", current.get("short_id", ""))).strip(),
        "spider_x": str(data.get("spider_x", current.get("spider_x", ""))).strip(),
        "alter_id": int(data.get("alter_id", current.get("alter_id", 0)) or 0),
        "method": str(data.get("method", current.get("method", ""))).strip(),
    }
    if data.get("config_path"):
        updates["config_path"] = str(data["config_path"])
    try:
        result = apply_client_settings(
            {**current, **updates},
            restart=bool(data.get("restart", True)),
            write_env=bool(data.get("write_env", True)),
        )
    except (ValueError, FileNotFoundError) as exc:
        return jsonify({"error": str(exc)}), 400
    except OSError as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify(result)


@bp.post("/api/xray/client/import")
def api_xray_client_import():
    imported = import_from_xray_config()
    if not imported:
        return jsonify({"error": "could not import from xray config"}), 404
    return jsonify({"settings": public_settings(imported)})


@bp.post("/api/xray/client/parse")
def api_xray_client_parse():
    data = request.get_json(silent=True) or {}
    share_url = str(data.get("share_url") or data.get("vless_url") or "").strip()
    if not share_url:
        return jsonify({"error": "share_url is required"}), 400
    try:
        settings = public_settings(merge_share_url(load_client_settings(), share_url))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"settings": settings})


@bp.post("/api/xray/client/test")
def api_xray_client_test():
    data = request.get_json(silent=True) or {}
    settings = load_client_settings()
    if data.get("share_url") or data.get("vless_url"):
        share_url = str(data.get("share_url") or data.get("vless_url"))
        try:
            settings = merge_share_url(settings, share_url)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
    elif data:
        settings = {**settings, **data}
    cursor_test = test_proxy(settings)
    cursor_api_test = test_cursor_api(settings)
    return jsonify(
        {
            "cursor_test": cursor_test,
            "cursor_api_test": cursor_api_test,
            "status": build_status_report(settings),
        }
    )


@bp.get("/api/proxy/pool")
def api_proxy_pool_get():
    migrate_legacy_to_pool()
    live = request.args.get("live") == "1"
    status = pool_status()
    if live and status.get("enabled"):
        status["live_test"] = test_all_proxies(load_pool_safe(), apply_each=False)
    return jsonify(status)


@bp.post("/api/proxy/pool")
def api_proxy_pool_save():
    data = request.get_json(silent=True) or {}
    try:
        if "enabled" in data and len(data) == 1:
            result = set_pool_enabled(bool(data["enabled"]))
        else:
            result = apply_pool_config(data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except (FileNotFoundError, OSError) as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify(result)


@bp.post("/api/proxy/pool/subscription")
def api_proxy_pool_subscription():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url") or "").strip()
    name = str(data.get("name") or "").strip()
    if not url:
        return jsonify({"error": "url required"}), 400
    try:
        entry = add_subscription(load_pool_safe(), url, name=name)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "subscription": entry, "status": pool_status()})


@bp.post("/api/proxy/pool/link")
def api_proxy_pool_link():
    data = request.get_json(silent=True) or {}
    share_url = str(data.get("share_url") or data.get("vless_url") or "").strip()
    name = str(data.get("name") or "").strip()
    if not share_url:
        return jsonify({"error": "share_url required"}), 400
    try:
        entry = add_proxy_link(load_pool_safe(), share_url, name=name)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "proxy": public_proxy_entry_safe(entry), "status": pool_status()})


@bp.post("/api/proxy/pool/refresh")
def api_proxy_pool_refresh():
    pool = refresh_all_subscriptions(load_pool_safe())
    return jsonify({"ok": True, "status": pool_status()})


@bp.post("/api/proxy/pool/test")
def api_proxy_pool_test():
    data = request.get_json(silent=True) or {}
    apply_each = bool(data.get("apply_each", True))
    report = test_all_proxies(load_pool_safe(), apply_each=apply_each)
    return jsonify({"ok": bool(report.get("working")), "report": report, "status": pool_status()})


@bp.post("/api/proxy/pool/ensure")
def api_proxy_pool_ensure():
    force = request.args.get("force") == "1"
    result = ensure_proxy_ready(force_reselect=force)
    code = 200 if result.get("ok") else 503
    return jsonify({**result, "status": pool_status()}), code


@bp.post("/api/proxy/pool/select")
def api_proxy_pool_select():
    result = select_and_apply_best(load_pool_safe())
    code = 200 if result.get("ok") else 503
    return jsonify({**result, "status": pool_status()}), code
