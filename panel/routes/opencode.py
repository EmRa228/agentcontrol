"""Route group: opencode."""

from flask import Blueprint

from . import _live

bp = Blueprint("opencode", __name__)


def install(panel_app):
    _live.install(globals(), panel_app)


@bp.get("/api/opencode/status")
def api_opencode_status():
    return jsonify(opencode_panel.opencode_status(CFG, STATE_DIR, read_panel_password()))


@bp.post("/api/opencode/ensure")
def api_opencode_ensure():
    result = opencode_panel.ensure_serve(CFG, STATE_DIR, read_panel_password())
    code = 200 if result.get("ok") else 503
    return jsonify(result), code


@bp.route("/api/opencode/install", methods=["GET", "POST"])
def api_opencode_install():
    denied = opencode_proxy_access()
    if denied is not None:
        return denied
    if request.method == "POST":
        result, code = opencode_panel.start_install(CFG, STATE_DIR)
        return jsonify(result), code
    return jsonify(opencode_panel.install_status(CFG, STATE_DIR))


@bp.get("/api/opencode/versions")
def api_opencode_versions():
    binary = opencode_panel.find_opencode_bin(CFG)
    pinned = opencode_panel.pinned_version()
    installed = opencode_panel.binary_version(binary)
    web = opencode_panel.web_build_version()
    latest = STORE.meta_get("opencode_latest", "")
    if request.args.get("refresh") == "1":
        try:
            releases = json.loads(urlopen(
                "https://api.github.com/repos/anomalyco/opencode/releases?per_page=30", timeout=10
            ).read().decode("utf-8"))
            versions = []
            for rel in releases:
                tag = str(rel.get("tag_name") or "").lstrip("v")
                if rel.get("prerelease") or not tag:
                    continue
                try:
                    versions.append((tuple(int(x) for x in tag.split(".")), tag))
                except ValueError:
                    continue
            if versions:
                latest = max(versions)[1]
                STORE.meta_set("opencode_latest", latest)
        except (URLError, OSError, ValueError, json.JSONDecodeError):
            pass
    patch_file = INSTALL_DIR / "scripts" / f"opencode-web-{latest}.patch"
    return jsonify(
        {
            "installed": installed,
            "web": web,
            "pinned": pinned,
            "latest": latest or None,
            "patch_available": patch_file.is_file(),
            "update_available": bool(latest and latest != installed),
        }
    )


@bp.post("/api/opencode/update")
def api_opencode_update():
    data = request.get_json(silent=True) or {}
    target = str(data.get("version") or "").strip()
    script = INSTALL_DIR / "scripts" / "update-opencode.sh"
    if not script.is_file():
        return jsonify({"error": "update script missing"}), 500
    log_file = STATE_DIR / "opencode-update.log"
    cmd = ["bash", str(script)]
    if target:
        cmd += ["--version", target]
    env = {**os.environ, "STATE_DIR": str(STATE_DIR), "INSTALL_DIR": str(INSTALL_DIR)}
    try:
        with log_file.open("ab") as log:
            log.write(f"\n=== update requested at {datetime.now(timezone.utc).isoformat()} ===\n".encode())
            subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
    except OSError as exc:
        return jsonify({"error": str(exc)}), 500
    audit_log.record(STORE, "opencode.update", detail={"version": target or "latest"})
    return jsonify({"status": "started", "version": target or "latest", "log": str(log_file)}), 202


@bp.post("/api/opencode/restart")
def api_opencode_restart():
    try:
        opencode_panel.kill_serve_process(STATE_DIR)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    audit_log.record(STORE, "opencode.restart")
    return jsonify({"ok": True})


@bp.post("/api/opencode/open/<name>")
def api_opencode_open(name: str):
    path = folder_path(name)
    if not path:
        return jsonify({"error": "folder not found"}), 404
    if opencode_panel.web_install_required(CFG):
        return jsonify(
            {
                "ok": False,
                "status": "install_required",
                "version": opencode_panel.pinned_version(),
                "error": "OpenCode needs to be installed.",
            }
        ), 409
    touch_project(name)
    ensure = opencode_panel.ensure_serve(CFG, STATE_DIR, read_panel_password())
    if not ensure.get("ok"):
        return jsonify(ensure), 503
    public_path = opencode_panel.opencode_public_path(str(path))
    payload = {
        "status": "ready",
        "name": name,
        "path": str(path),
        "opencode_path": public_path,
        "opencode_url": request.host_url.rstrip("/") + public_path,
        "public_base": request.host_url.rstrip("/"),
        "opencode_version": opencode_panel.pinned_version(),
    }
    if auth_enabled() and not panel_totp.totp_enabled():
        launch_path = opencode_panel.create_launch_path(str(path))
        payload["opencode_launch_path"] = launch_path
        # Older Fleet workers ignore opencode_launch_path and open opencode_path directly.
        # Use the same one-time sign-in handoff there so the OpenCode button never lands on
        # an unauthenticated /oc/ page ("Sign in to AgentControl first").
        payload["opencode_path"] = launch_path
    return jsonify(payload)


@bp.get("/oc")
def opencode_proxy_redirect():
    denied = opencode_proxy_access()
    if denied is not None:
        return denied
    query = request.query_string.decode("ascii")
    return redirect("/oc/" + ("?" + query if query else ""), code=308)


@bp.route("/oc/", defaults={"subpath": ""}, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@bp.route("/oc/<path:subpath>", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def opencode_proxy(subpath: str):
    denied = opencode_proxy_access()
    if denied is not None:
        return denied
    web_root = Path(__file__).resolve().parent / "opencode-web"
    if request.method in {"GET", "HEAD"}:
        asset = safe_join(str(web_root), subpath)
        session_route = re.fullmatch(r"server/[^/]+/session/[^/]+", subpath)
        navigation = (
            not subpath
            or session_route is not None
            or request.headers.get("Sec-Fetch-Mode") == "navigate"
            or request.accept_mimetypes.best == "text/html"
        )
        if subpath and asset and Path(asset).is_file():
            response = send_from_directory(web_root, subpath)
            response.headers["Cache-Control"] = "private, no-cache"
            return response
        if subpath.startswith("assets/"):
            return Response("OpenCode asset not found.", status=404)
        if navigation:
            if not (web_root / "index.html").is_file():
                return Response("OpenCode web build missing. Run scripts/build-opencode-web.sh.", status=503)
            response = send_from_directory(web_root, "index.html")
            response.headers["Cache-Control"] = "no-store"
            return response
    status, headers, payload, close = opencode_panel.proxy_request(
        CFG, read_panel_password(), subpath, request.method, request.query_string,
        dict(request.headers), request.get_data(cache=False) or None,
    )
    response = Response(payload, status=status, headers=headers)
    if close is not None:
        response.call_on_close(close)
    return response


@bp.route("/oc/pty/<terminal>/connect", websocket=True)
def opencode_terminal_proxy(terminal: str):
    return opencode_terminal_proxy_for(terminal, "pty")


@bp.route("/oc/api/pty/<terminal>/connect", websocket=True)
def opencode_api_terminal_proxy(terminal: str):
    return opencode_terminal_proxy_for(terminal, "api/pty")


@bp.route("/oc/api/experimental/persistent-pty/<terminal>/connect", websocket=True)
def opencode_persistent_terminal_proxy(terminal: str):
    return opencode_terminal_proxy_for(terminal, "api/experimental/persistent-pty")
