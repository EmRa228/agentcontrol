"""Route group: system."""

from flask import Blueprint

from . import _live

bp = Blueprint("system", __name__)


def install(panel_app):
    _live.install(globals(), panel_app)


@bp.get("/api/fleet/bundle")
def api_fleet_bundle():
    folders = list_folders()
    for item in folders:
        if item["running"]:
            item["ready"] = worker_ready(item["name"])
    migrate_legacy_to_pool()
    health = panel_health_snapshot()
    health.pop("workers", None)
    binary = opencode_panel.find_opencode_bin(CFG)
    return jsonify(
        {
            "system": fleet_system_summary(),
            "folders": fleet_folders_summary(folders),
            "scan_root": CFG["scan_root"],
            "version": load_version(),
            "health": health,
            "agent": {
                "version": agent_version(),
                "installed": bool(find_agent_bin()),
            },
            "proxy": fleet_proxy_summary(),
            "opencode": {
                "healthy": opencode_panel.is_serve_healthy(
                    CFG, opencode_panel.serve_password(CFG, read_panel_password())
                ),
                "proxy_prefix": "/oc",
                "version": opencode_panel.binary_version(binary),
                "pinned": opencode_panel.pinned_version(),
                "web_version": opencode_panel.web_build_version(),
            },
        }
    )


@bp.get("/api/agent/status")
def api_agent_status():
    log_file = STATE_DIR / "agent-update.log"
    tail = ""
    try:
        if log_file.is_file():
            tail = log_file.read_text(encoding="utf-8", errors="ignore")[-2000:]
    except OSError:
        pass
    return jsonify({"installed": bool(find_agent_bin()), "version": agent_version(), "log_tail": tail})


@bp.post("/api/agent/update")
def api_agent_update():
    log_file = STATE_DIR / "agent-update.log"
    env = {**os.environ, "PATH": "/root/.local/bin:" + os.environ.get("PATH", "")}
    try:
        with log_file.open("ab") as log:
            log.write(f"\n=== agent update at {datetime.now(timezone.utc).isoformat()} ===\n".encode())
            subprocess.Popen(
                ["bash", "-lc", "curl https://cursor.com/install -fsS | bash"],
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
    except OSError as exc:
        return jsonify({"error": str(exc)}), 500
    audit_log.record(STORE, "agent.update")
    return jsonify({"status": "started", "log": str(log_file)}), 202


@bp.get("/api/fleet/sso")
def api_fleet_sso():
    """Consume a Fleet launch token, sign the browser in, and open OpenCode."""
    if not auth_enabled():
        return redirect("/")
    token = request.args.get("token", "")
    directory = opencode_panel.consume_launch_token(token)
    if not directory:
        return Response(
            "This OpenCode link expired. Open the project from Fleet again.",
            status=401,
            content_type="text/plain",
        )
    if panel_totp.totp_enabled():
        return redirect("/")
    response = redirect(opencode_panel.opencode_public_path(directory), code=302)
    set_session_cookie(response, create_session())
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@bp.get("/api/caddy/allow")
def api_caddy_allow():
    # Caddy's On-Demand TLS "ask" endpoint; only answer the local Caddy.
    if request.remote_addr not in {"127.0.0.1", "::1"}:
        return Response(status=403)
    domain = str(request.args.get("domain", "")).strip()
    if not domain:
        return Response(status=400)
    return Response(status=200 if domain_resolves_here(domain) else 403)


@bp.get("/api/ready/<name>")
def api_ready(name: str):
    state = reconcile_state()
    info = state.get(name)
    if not info:
        return jsonify({"ready": False, "running": False}), 200
    running = bool(info.get("pid") and is_running(info["pid"]))
    log_tail = worker_log_session(worker_log_tail(info))[-2000:]
    log_errors = detect_worker_log_errors(log_tail)
    ready = worker_ready(name) if running else False
    probe = probe_worker_readyz(info["mgmt_port"]) if running and info.get("mgmt_port") else None
    return jsonify(
        {
            "ready": ready,
            "running": running,
            "agent_url": agent_url(name),
            "log_tail": log_tail,
            "log_errors": log_errors,
            "has_errors": bool(log_errors) and not ready,
            "readyz": probe,
        }
    )


@bp.get("/api/system")
def api_system():
    return jsonify(collect_system_info())


@bp.get("/api/system/history")
def api_system_history():
    range_key = str(request.args.get("range", "live")).lower()
    spans = {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800}
    if range_key in spans:
        return jsonify({"points": metrics_mod.history(STORE, spans[range_key]), "range": range_key})
    return jsonify({"points": list(METRICS_HISTORY), "range": "live"})


@bp.get("/api/update/check")
def api_update_check():
    try:
        result = check_panel_update()
    except (URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        return jsonify({"local": load_version(), "remote": None, "update_available": False, "error": str(exc)})
    result["in_progress"] = update_in_progress()
    return jsonify(result)


@bp.get("/api/update/status")
def api_update_status():
    local = load_version()
    remote = None
    try:
        remote = fetch_github_json("version.json")
    except (URLError, OSError, ValueError, json.JSONDecodeError):
        pass
    return jsonify(
        {
            "local": local,
            "remote": remote,
            "in_progress": update_in_progress(),
            "log_tail": read_update_log_tail(),
        }
    )


@bp.post("/api/update/apply")
def api_update_apply():
    result, code = start_panel_update()
    audit_log.record(STORE, "panel.update", detail={"status": result.get("status"), "code": code})
    return jsonify(result), code


@bp.get("/api/version")
@bp.get("/version.json")
def api_version():
    return jsonify(load_version())


@bp.get("/health")
def health():
    version = load_version()
    return jsonify(
        {
            "ok": True,
            "component": version.get("component", "panel"),
            "version": version.get("version", "0.0.0"),
        }
    )


@bp.get("/connectivity-test")
def connectivity_test():
    """Plain-text page for international reachability checks (no auth, no JS)."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = (
        "egress connectivity test\n"
        "service=agentcontrol\n"
        f"port={CFG['port']}\n"
        f"host={request.host}\n"
        f"time_utc={stamp}\n"
        "If you can read this from outside Iran, HTTP on this port works.\n"
    )
    return Response(body, mimetype="text/plain; charset=utf-8")


@bp.get("/connectivity-test-minimal.html")
def connectivity_test_minimal_html():
    """Minimal HTML (no scripts/stylesheets) for content-filter experiments."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    html = (
        "<!DOCTYPE html><html><head><meta charset=utf-8>"
        "<title>connectivity test</title></head><body>"
        "<p>egress connectivity test — agentcontrol port "
        f"{CFG['port']}</p><p>time: {stamp}</p>"
        "</body></html>"
    )
    return Response(html, mimetype="text/html; charset=utf-8")


@bp.get("/api/public/status")
def api_public_status():
    if not SETTINGS.status_page_enabled:
        return jsonify({"error": "status page disabled"}), 404
    return jsonify(_public_health(panel_health_snapshot()))


@bp.get("/status")
def public_status_page():
    if not SETTINGS.status_page_enabled:
        return Response("Status page disabled.", status=404)
    return render_template("status.html", version=load_version(), csp_nonce=g.csp_nonce)


@bp.get("/api/openapi.json")
def api_openapi():
    import re as _re

    paths: dict[str, dict] = {}
    for rule in app.url_map.iter_rules():
        if not rule.rule.startswith("/api/") or rule.rule.startswith("/api/v1/"):
            continue
        if getattr(rule, "websocket", False):
            continue
        path = _re.sub(r"<(?:\w+:)?(\w+)>", r"{\1}", rule.rule)
        methods = (rule.methods or set()) - {"HEAD", "OPTIONS"}
        for method in methods:
            role = required_role(rule.rule, method)
            paths.setdefault(path, {})[method.lower()] = {
                "summary": rule.endpoint,
                "security": [{"session": []}, {"X-AgentControl-Auth": []}, {"X-AgentControl-Token": []}],
                "x-required-role": role,
                "responses": {"200": {"description": "OK"}, "401": {"description": "Unauthorized"}},
            }
    return jsonify(
        {
            "openapi": "3.0.3",
            "info": {"title": "AgentControl API", "version": load_version().get("version", "0.0.0")},
            "servers": [{"url": "/api/v1"}],
            "components": {
                "securitySchemes": {
                    "session": {"type": "apiKey", "in": "cookie", "name": SESSION_COOKIE},
                    "X-AgentControl-Auth": {"type": "apiKey", "in": "header", "name": "X-AgentControl-Auth"},
                    "X-AgentControl-Token": {"type": "apiKey", "in": "header", "name": "X-AgentControl-Token"},
                }
            },
            "paths": paths,
        }
    )


@bp.get("/metrics")
def api_prometheus():
    if not SETTINGS.metrics_public:
        client = request.remote_addr or ""
        actor = resolve_actor()
        if client not in {"127.0.0.1", "::1"} and not (actor and security_mod.role_allows(actor["role"], security_mod.ROLE_READ)):
            return Response("unauthorized\n", status=401, mimetype="text/plain")
    points = STORE.metrics_since(time.time() - 3600)
    sample = points[-1] if points else metrics_mod.Sampler(STORE).sample()
    health = panel_health_snapshot()
    body = metrics_mod.prometheus_text(
        sample=sample,
        health=health,
        workers_running=health.get("workers_running", 0),
        workers_total=health.get("workers_total", 0),
        version=health.get("version", "0.0.0"),
        uptime=health.get("uptime_seconds", 0),
    )
    return Response(body, mimetype="text/plain; version=0.0.4; charset=utf-8")
