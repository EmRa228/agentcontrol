import json
from email.message import Message
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as panel
import opencode_panel
import panel_sessions


class OpenCodePanelTests(unittest.TestCase):
    def test_binary_version_accepts_opencode_2_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "opencode"
            binary.touch()
            with mock.patch.object(opencode_panel.subprocess, "run") as run:
                run.return_value.stdout = "opencode v2.4.1\n"
                self.assertEqual(opencode_panel.binary_version(binary), "2.4.1")

    def test_missing_opencode_project_can_install_and_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = panel_sessions.SessionStore(Path(tmp) / "sessions.db")
            store.initialize()
            with (
                mock.patch.object(panel, "STATE_DIR", Path(tmp)),
                mock.patch.object(panel, "SESSIONS", store),
                mock.patch.object(panel, "read_panel_password", return_value="test-password"),
                mock.patch.object(panel, "folder_path", return_value=Path("/project")),
                mock.patch.object(panel, "touch_project"),
                mock.patch.object(opencode_panel, "_INSTALL_PROC", None),
                mock.patch.object(opencode_panel, "web_install_required", return_value=True) as missing,
                mock.patch.object(opencode_panel, "find_opencode_bin", return_value=None),
                mock.patch.object(opencode_panel.subprocess, "Popen") as launch,
                mock.patch.object(opencode_panel, "ensure_serve", return_value={"ok": True}) as ensure,
                mock.patch.object(panel.panel_totp, "totp_enabled", return_value=False),
                panel.app.test_client() as client,
            ):
                install_url = "/api/opencode/install"
                project_url = "/api/opencode/open/project"
                self.assertEqual(client.post(install_url).status_code, 401)
                client.set_cookie(panel.SESSION_COOKIE, store.create(3600))
                response = client.post(project_url)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json["status"], "install_required")
                ensure.assert_not_called()
                self.assertEqual(client.get(install_url).json["status"], "missing")

                process = launch.return_value
                process.poll.return_value = None
                self.assertEqual(client.post(install_url).status_code, 202)
                self.assertEqual(client.post(install_url).status_code, 202)
                self.assertEqual(client.get(install_url).json["status"], "installing")
                launch.assert_called_once()

                process.poll.return_value = 1
                self.assertEqual(client.get(install_url).json["status"], "failed")
                process.poll.return_value = 1
                self.assertEqual(client.post(install_url).status_code, 202)
                self.assertEqual(launch.call_count, 2)

                process.poll.return_value = 0
                missing.return_value = False
                self.assertEqual(client.get(install_url).json["status"], "ready")
                ready = client.post(project_url).json
                self.assertEqual(ready["status"], "ready")
                self.assertEqual(ready["path"], "/project")
                self.assertTrue(ready["opencode_path"].startswith("/api/fleet/sso?token="))
                self.assertEqual(ready["opencode_path"], ready["opencode_launch_path"])
                ensure.assert_called_once()

    def test_session_reload_serves_frontend_with_panel_cookie(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            web = root / "opencode-web"
            web.mkdir()
            (web / "index.html").write_text('<script src="/oc/assets/app.js"></script>')
            store = panel_sessions.SessionStore(root / "sessions.db")
            store.initialize()
            with (
                mock.patch.object(panel, "__file__", str(root / "app.py")),
                mock.patch.object(panel, "SESSIONS", store),
                mock.patch.object(panel, "read_panel_password", return_value="test-password"),
                mock.patch.object(opencode_panel, "proxy_request",
                                  return_value=(200, {"Content-Type": "application/json"}, b"{}", None)) as proxy,
                panel.app.test_client() as client,
            ):
                url = "/oc/server/c2VydmVy/session/ses_test"
                self.assertEqual(client.get(url).status_code, 401)
                response = client.post("/api/auth/login", json={"password": "test-password"})
                self.assertEqual(response.status_code, 200)
                for headers in ({"Accept": "text/html"}, {"Accept": "*/*"}):
                    with client.get(url, headers=headers) as response:
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.headers["Cache-Control"], "no-store")
                        self.assertIn(b"/oc/assets/app.js", response.data)
                proxy.assert_not_called()
                self.assertEqual(client.get("/oc/session/ses_test").json, {})
                proxy.assert_called_once()

    def test_public_path_encodes_directory(self):
        path = opencode_panel.opencode_public_path("/root/keshtirani")
        self.assertIn("/oc/", path)
        self.assertIn("directory=", path)

    def test_save_secrets_writes_provider_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {
                "opencode_secrets_file": f"{tmp}/secrets.json",
                "opencode_env_file": f"{tmp}/providers.env",
                "opencode_config_file": f"{tmp}/opencode.json",
            }
            opencode_panel.save_secrets(
                cfg,
                {
                    "anthropic_api_key": "sk-ant-test",
                    "opencode_go_key": "oc_sk_test",
                    "default_model": "opencode/claude",
                },
            )
            secrets = json.loads(Path(cfg["opencode_secrets_file"]).read_text(encoding="utf-8"))
            self.assertEqual(secrets["anthropic_api_key"], "sk-ant-test")
            env_text = Path(cfg["opencode_env_file"]).read_text(encoding="utf-8")
            self.assertIn("ANTHROPIC_API_KEY=sk-ant-test", env_text)
            self.assertIn("OPENCODE_API_KEY=oc_sk_test", env_text)
            config = json.loads(Path(cfg["opencode_config_file"]).read_text(encoding="utf-8"))
            self.assertIn("anthropic", config.get("providers", {}))

    @mock.patch("opencode_panel.serve_password", return_value="upstream-test")
    @mock.patch("opencode_panel.HTTPConnection")
    def test_proxy_relays_sse_without_forwarding_panel_credentials(self, connection, _password):
        upstream = connection.return_value
        response = upstream.getresponse.return_value
        response.status = 200
        response.headers = Message()
        response.headers["Content-Type"] = "text/event-stream"
        response.headers["Set-Cookie"] = "upstream-cookie=test"
        response.getheaders.return_value = list(response.headers.items())
        response.read1.side_effect = [b"data: first\n\n", b"data: second\n\n", b""]

        status, headers, stream, close = opencode_panel.proxy_request(
            {}, "panel-test", "global/event", "GET", b"",
            {"Cookie": "agentcontrol_auth=panel-test", "X-AgentControl-Auth": "panel-test"}, None,
        )

        self.assertEqual(status, 200)
        self.assertIn(("X-Accel-Buffering", "no"), headers)
        self.assertFalse(any(name.lower() == "set-cookie" for name, _ in headers))
        response.read1.assert_not_called()
        self.assertEqual(next(stream), b"data: first\n\n")
        response.read1.assert_called_once()
        sent = upstream.request.call_args.kwargs["headers"]
        self.assertEqual(sent["Authorization"], opencode_panel._basic_auth_header("upstream-test"))
        self.assertNotIn("Cookie", sent)
        self.assertNotIn("X-AgentControl-Auth", sent)
        stream.close()
        upstream.close.assert_called_once()

    def test_serve_password_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"opencode_serve_password_file": f"{tmp}/pw"}
            first = opencode_panel.serve_password(cfg, "panel-secret")
            second = opencode_panel.serve_password(cfg, None)
            self.assertEqual(first, second)
            self.assertEqual(first, "panel-secret")


if __name__ == "__main__":
    unittest.main()


class OpenCodeLaunchTests(unittest.TestCase):
    def test_launch_token_is_single_use(self):
        token_path = opencode_panel.create_launch_path("/root/demo")
        token = token_path.split("token=", 1)[1]
        self.assertEqual(opencode_panel.consume_launch_token(token), "/root/demo")
        self.assertIsNone(opencode_panel.consume_launch_token(token))

    def test_launch_token_expires(self):
        with mock.patch.object(opencode_panel.time, "time", return_value=1000.0):
            token_path = opencode_panel.create_launch_path("/root/demo")
        token = token_path.split("token=", 1)[1]
        with mock.patch.object(opencode_panel.time, "time", return_value=1000.0 + opencode_panel.LAUNCH_TOKEN_TTL + 1):
            self.assertIsNone(opencode_panel.consume_launch_token(token))

    def test_fleet_sso_sets_cookie_and_redirects(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = panel_sessions.SessionStore(Path(tmp) / "sessions.db")
            store.initialize()
            with (
                mock.patch.object(panel, "SESSIONS", store),
                mock.patch.object(panel, "read_panel_password", return_value="panel-password"),
                panel.app.test_client() as client,
            ):
                token = opencode_panel.create_launch_path("/root/demo").split("token=", 1)[1]
                response = client.get(f"/api/fleet/sso?token={token}")
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["Location"], "/oc/?directory=%2Froot%2Fdemo")
                self.assertIn(panel.SESSION_COOKIE, response.headers.get("Set-Cookie", ""))
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                replay = client.get(f"/api/fleet/sso?token={token}")
                self.assertEqual(replay.status_code, 401)

    def test_open_returns_launch_path_for_fleet(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(panel, "STATE_DIR", Path(tmp)),
                mock.patch.object(panel, "read_panel_password", return_value="panel-password"),
                mock.patch.object(panel, "folder_path", return_value=Path("/project")),
                mock.patch.object(panel, "touch_project"),
                mock.patch.object(panel.panel_totp, "totp_enabled", return_value=False),
                mock.patch.object(opencode_panel, "web_install_required", return_value=False),
                mock.patch.object(opencode_panel, "ensure_serve", return_value={"ok": True}),
                panel.app.test_client() as client,
            ):
                body = client.post(
                    "/api/opencode/open/project",
                    headers={"X-AgentControl-Auth": "panel-password"},
                ).json
                self.assertEqual(body["status"], "ready")
                self.assertIn("/api/fleet/sso?token=", body["opencode_launch_path"])


class OpenCodeConfigTests(unittest.TestCase):
    def test_provider_config_merges_and_preserves_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "opencode.json"
            config_path.write_text(
                '{"theme":"custom","providers":{"keep":{"settings":{}}},'
                '"permission":{"bash":"ask"},"permissions":[{"action":"bash","resource":"*","effect":"deny"}]}'
            )
            cfg = {
                "opencode_secrets_file": f"{tmp}/secrets.json",
                "opencode_env_file": f"{tmp}/providers.env",
                "opencode_config_file": str(config_path),
            }
            opencode_panel.save_secrets(cfg, {"openai_api_key": "sk-test"})
            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(config["theme"], "custom")
            self.assertIn("keep", config["providers"])
            self.assertIn("openai", config["providers"])
            self.assertEqual(config["permission"], {"bash": "ask"})
            self.assertEqual(config["permissions"], [{"action": "bash", "resource": "*", "effect": "deny"}])

    def test_install_required_when_binary_version_differs(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"state_dir": tmp, "opencode_bin": str(Path(tmp) / "opencode")}
            with (
                mock.patch.object(opencode_panel, "find_opencode_bin", return_value="/usr/bin/opencode"),
                mock.patch.object(opencode_panel, "binary_version", return_value="1.19.6"),
                mock.patch.object(opencode_panel, "web_build_version", return_value="1.19.6"),
                mock.patch.object(opencode_panel, "opencode_web_root", return_value=Path(tmp)),
            ):
                (Path(tmp) / "index.html").write_text("ok")
                self.assertTrue(opencode_panel.web_install_required(cfg))


class SessionAuthTests(unittest.TestCase):
    def _client(self, tmp):
        store = panel_sessions.SessionStore(Path(tmp) / "sessions.db")
        store.initialize()
        return store

    def test_login_sets_opaque_session_cookie_and_revoke_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._client(tmp)
            with (
                mock.patch.object(panel, "SESSIONS", store),
                mock.patch.object(panel, "read_panel_password", return_value="panel-password"),
                mock.patch.object(panel.panel_totp, "totp_enabled", return_value=False),
                panel.app.test_client() as client,
            ):
                res = client.post("/api/auth/login", json={"password": "panel-password"})
                self.assertEqual(res.status_code, 200)
                cookie = res.headers.get("Set-Cookie", "")
                self.assertIn(panel.SESSION_COOKIE, cookie)
                self.assertIn("HttpOnly", cookie)
                self.assertNotIn("panel-password", cookie)
                self.assertEqual(store.count(), 1)
                self.assertEqual(client.get("/api/auth/sessions").json["count"], 1)

                token = client.get_cookie(panel.SESSION_COOKIE).value
                revoked = client.post("/api/auth/sessions/revoke-all", json={})
                self.assertEqual(revoked.status_code, 200)
                self.assertEqual(revoked.json["revoked"], 1)
                self.assertFalse(store.validate(token))
                client.set_cookie(panel.SESSION_COOKIE, token)
                self.assertEqual(client.get("/api/auth/sessions").status_code, 401)

    def test_logout_revokes_only_current_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._client(tmp)
            with (
                mock.patch.object(panel, "SESSIONS", store),
                mock.patch.object(panel, "read_panel_password", return_value="panel-password"),
                mock.patch.object(panel.panel_totp, "totp_enabled", return_value=False),
                panel.app.test_client() as client,
            ):
                first = client.post("/api/auth/login", json={"password": "panel-password"})
                self.assertEqual(first.status_code, 200)
                token = client.get_cookie(panel.SESSION_COOKIE).value
                other = store.create(3600)
                self.assertEqual(store.count(), 2)
                out = client.post("/api/auth/logout")
                self.assertEqual(out.status_code, 200)
                self.assertFalse(store.validate(token))
                self.assertTrue(store.validate(other))

    def test_header_password_still_authorizes_fleet(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._client(tmp)
            with (
                mock.patch.object(panel, "SESSIONS", store),
                mock.patch.object(panel, "read_panel_password", return_value="panel-password"),
                panel.app.test_client() as client,
            ):
                res = client.get(
                    "/api/auth/sessions",
                    headers={"X-AgentControl-Auth": "panel-password"},
                )
                self.assertEqual(res.status_code, 200)
                self.assertEqual(store.count(), 0)
