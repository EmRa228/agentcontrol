"""Route group: settings."""

from flask import Blueprint

from . import _live

bp = Blueprint("settings", __name__)


def install(panel_app):
    _live.install(globals(), panel_app)


@bp.get("/api/models")
def api_models():
    return jsonify({"models": list_agent_models()})


@bp.get("/api/settings")
def api_settings():
    return jsonify(
        {
            "default_model": CFG.get("default_model") or "",
            "idle_hours": round(CFG["idle_release_seconds"] / 3600, 1),
            "scan_root": CFG["scan_root"],
            "port": CFG["port"],
            "api_key_set": bool(read_api_key()),
            "cursor_bridge": public_bridge_settings(),
            "totp": panel_totp.public_totp_status(socket.gethostname()),
            "opencode": opencode_panel.opencode_status(CFG, STATE_DIR, read_panel_password()),
        }
    )


@bp.get("/api/settings/bridge")
def api_settings_bridge_get():
    return jsonify(public_bridge_settings())


@bp.post("/api/settings/bridge")
def api_settings_bridge_save():
    data = request.get_json(silent=True) or {}
    try:
        settings = save_bridge_from_panel(data)
    except (ValueError, OSError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "cursor_bridge": public_bridge_settings(), "endpoint": settings.endpoint})


@bp.post("/api/settings/bridge/test")
def api_settings_bridge_test():
    api_key = read_api_key()
    result = test_cursor_bridge(api_key=api_key)
    status = 200 if result.get("ok") else 502
    return jsonify(result), status


@bp.post("/api/settings/model")
def api_settings_model():
    data = request.get_json(silent=True) or {}
    model = str(data.get("model", "")).strip()
    save_config_patch({"default_model": model})
    return jsonify({"ok": True, "default_model": model})


@bp.get("/api/settings/opencode")
def api_settings_opencode_get():
    return jsonify(
        {
            "status": opencode_panel.opencode_status(CFG, STATE_DIR, read_panel_password()),
            "secrets": opencode_panel.public_secrets(opencode_panel.load_secrets(CFG)),
        }
    )


@bp.post("/api/settings/opencode")
def api_settings_opencode_save():
    data = request.get_json(silent=True) or {}
    secrets = opencode_panel.save_secrets(CFG, data)
    if data.get("restart_serve"):
        opencode_panel.start_serve_process(
            CFG, STATE_DIR, serve_password_for_panel(), force=True
        )
    return jsonify({"ok": True, "secrets": secrets})


@bp.post("/api/settings/opencode/remote/refresh")
def api_settings_opencode_remote_refresh():
    result = opencode_panel.try_refresh_remote_url(CFG, STATE_DIR, read_panel_password())
    code = 200 if result.get("ok") else 502
    return jsonify(result), code


@bp.get("/api/settings/totp")
def api_settings_totp_get():
    host = socket.gethostname()
    return totp_json(panel_totp.public_totp_status(host))


@bp.post("/api/settings/totp/begin")
def api_settings_totp_begin():
    # Generates a pending secret and returns a scannable QR code. The current
    # authenticator (if any) stays active until the code is verified.
    return totp_json(panel_totp.begin_totp_setup(socket.gethostname()))


@bp.post("/api/settings/totp/enable")
def api_settings_totp_enable():
    data = request.get_json(silent=True) or {}
    code = str(data.get("code", ""))
    fallback = str(data.get("secret", "")).strip()
    ok, err = panel_totp.enable_pending_totp(code, fallback)
    if not ok:
        return jsonify({"error": err or "invalid code"}), 400
    # A change to the second factor invalidates existing browser sessions so they
    # must re-authenticate with the new factor configuration.
    SESSIONS.revoke_all()
    resp = jsonify(
        {
            "ok": True,
            "totp": panel_totp.public_totp_status(socket.gethostname()),
            "reauthentication_required": True,
        }
    )
    return clear_session_cookie(resp)


@bp.post("/api/settings/totp/rotate")
def api_settings_totp_rotate():
    # Only creates a new pending secret, so rotating never locks the panel out
    # before the new secret has been verified.
    return totp_json(panel_totp.rotate_totp_secret(socket.gethostname()))


@bp.post("/api/settings/totp/disable")
def api_settings_totp_disable():
    panel_totp.disable_totp()
    SESSIONS.revoke_all()
    resp = jsonify(
        {
            "ok": True,
            "totp": panel_totp.public_totp_status(socket.gethostname()),
            "reauthentication_required": True,
        }
    )
    return clear_session_cookie(resp)
