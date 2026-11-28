"""Route group: auth."""

from flask import Blueprint

from . import _live

bp = Blueprint("auth", __name__)


def install(panel_app):
    _live.install(globals(), panel_app)


@bp.get("/")
def index():
    ctx = {
        "port": CFG["port"],
        "scan_root": CFG["scan_root"],
        "version": load_version(),
        "hostname": socket.gethostname(),
        "ip": primary_ip() or "",
        "host_label": request.host or socket.gethostname(),
        "csp_nonce": g.csp_nonce,
    }
    if not auth_enabled():
        return render_template("gate_setup.html", **ctx)
    if not panel_session_ok():
        return render_template("gate_login.html", **ctx)
    return render_template("index.html", **ctx)


@bp.post("/api/auth/login")
def api_auth_login():
    if not auth_enabled():
        return jsonify({"ok": True, "auth_required": False})
    client = request.remote_addr or "unknown"
    allowed, retry_after = LOGIN_LIMITER.check(client)
    if not allowed:
        return jsonify(
            {
                "error": f"too many failed attempts — try again in {retry_after}s",
                "code": "rate_limited",
                "retry_after": retry_after,
            }
        ), 429, {"Retry-After": str(retry_after)}
    data = request.get_json(silent=True) or {}
    password = str(data.get("password", ""))
    totp_code = str(data.get("totp", ""))
    if not password_ok(password):
        attempts, locked_for = LOGIN_LIMITER.failure(client)
        security_mod.append_auth_log(AUTH_FAILURE_LOG, client, "wrong password", request.path)
        audit_log.record(STORE, "auth.login_failed", target=client, detail={"reason": "wrong_password", "attempts": attempts})
        if locked_for:
            NOTIFIER.notify(
                "login_fail",
                "warning",
                "panel login lockout",
                f"{attempts} failed sign-ins from {client}; blocked for {locked_for}s.",
                key=f"login-lockout:{client}",
                detail={"ip": client, "attempts": attempts, "locked_for": locked_for},
            )
            return jsonify(
                {
                    "error": f"too many failed attempts — try again in {locked_for}s",
                    "code": "rate_limited",
                    "retry_after": locked_for,
                }
            ), 429, {"Retry-After": str(locked_for)}
        return jsonify({"error": "wrong password", "auth_required": True}), 401
    if panel_totp.totp_enabled() and not panel_totp.verify_login_code(totp_code):
        attempts, locked_for = LOGIN_LIMITER.failure(client)
        security_mod.append_auth_log(AUTH_FAILURE_LOG, client, "wrong totp", request.path)
        audit_log.record(STORE, "auth.login_failed", target=client, detail={"reason": "wrong_totp", "attempts": attempts})
        if locked_for:
            return jsonify(
                {
                    "error": f"too many failed attempts — try again in {locked_for}s",
                    "code": "rate_limited",
                    "retry_after": locked_for,
                }
            ), 429, {"Retry-After": str(locked_for)}
        return jsonify({"error": "invalid authenticator code", "totp_required": True}), 401
    LOGIN_LIMITER.reset(client)
    g.actor_name = "panel"
    g.actor_role = security_mod.ROLE_ADMIN
    g.actor_source = "session"
    audit_log.record(STORE, "auth.login", target=client, detail={"totp": panel_totp.totp_enabled()})
    resp = jsonify({"ok": True, "auth_required": True})
    return set_session_cookie(resp, create_session())


@bp.post("/api/auth/logout")
def api_auth_logout():
    SESSIONS.revoke(session_token())
    audit_log.record(STORE, "auth.logout")
    resp = jsonify({"ok": True})
    return clear_session_cookie(resp)


@bp.get("/api/auth/sessions")
def api_auth_sessions():
    current = session_token()
    sessions = SESSIONS.list_sessions(current_token=current)
    return jsonify(
        {
            "count": len(sessions),
            "ttl_days": round(session_ttl_seconds() / 86400, 3),
            "sessions": sessions,
        }
    )


@bp.post("/api/auth/sessions/<session_id>/revoke")
def api_auth_session_revoke(session_id: str):
    revoked = SESSIONS.revoke_by_id(session_id)
    if revoked:
        audit_log.record(STORE, "auth.session_revoked", target=session_id)
    return jsonify({"ok": revoked, "revoked": 1 if revoked else 0}), (200 if revoked else 404)


@bp.post("/api/auth/sessions/revoke-all")
def api_auth_sessions_revoke_all():
    data = request.get_json(silent=True) or {}
    keep_current = bool(data.get("keep_current"))
    current = session_token()
    if keep_current and current:
        revoked = SESSIONS.revoke_all(except_token=current)
        audit_log.record(STORE, "auth.sessions_revoked", detail={"revoked": revoked, "kept_current": True})
        return jsonify({"ok": True, "revoked": revoked, "kept_current": True})
    revoked = SESSIONS.revoke_all()
    audit_log.record(STORE, "auth.sessions_revoked", detail={"revoked": revoked, "kept_current": False})
    resp = jsonify({"ok": True, "revoked": revoked, "kept_current": False})
    return clear_session_cookie(resp)


@bp.get("/api/auth/status")
def api_auth_status():
    return jsonify(
        {
            "auth_required": auth_enabled(),
            "api_key_set": api_key_configured(),
            "needs_password": not auth_enabled(),
            "needs_api_key": not api_key_configured(),
            "totp": panel_totp.public_totp_status(socket.gethostname()),
        }
    )


@bp.get("/api/setup/status")
def api_setup_status():
    return jsonify(
        {
            "needs_password": not auth_enabled(),
            "needs_api_key": not api_key_configured(),
            "ready": auth_enabled() and api_key_configured(),
        }
    )


@bp.post("/api/setup/password")
def api_setup_password():
    if auth_enabled():
        return jsonify({"error": "password already configured"}), 400
    if not setup_allowed():
        return jsonify({"error": "setup token required", "code": "setup_token_required"}), 403
    data = request.get_json(silent=True) or {}
    password = str(data.get("password", "")).strip()
    confirm = str(data.get("confirm", "")).strip()
    if len(password) < 4:
        return jsonify({"error": "password must be at least 4 characters"}), 400
    if password != confirm:
        return jsonify({"error": "passwords do not match"}), 400
    write_panel_password(password)
    audit_log.record(STORE, "setup.password", actor="first-run", role=security_mod.ROLE_ADMIN)
    resp = jsonify({"ok": True})
    return set_session_cookie(resp, create_session())


@bp.post("/api/setup/api-key")
def api_setup_api_key():
    data = request.get_json(silent=True) or {}
    api_key = str(data.get("api_key", "")).strip()
    if not api_key:
        return jsonify({"error": "api_key required"}), 400
    if api_key_configured():
        if not auth_enabled() or not panel_session_ok():
            return jsonify({"error": "unauthorized"}), 401
    elif not setup_allowed():
        return jsonify({"error": "setup token required", "code": "setup_token_required"}), 403

    verify = request.args.get("verify", "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }
    if verify:
        check = verify_cursor_api_key(api_key)
        if not check.get("valid"):
            payload: dict[str, object] = {
                "error": check.get("error") or "invalid API key",
            }
            if check.get("detail"):
                payload["detail"] = check["detail"]
            return jsonify(payload), 400

    write_api_key(api_key)
    return jsonify({"ok": True, "valid": True})
