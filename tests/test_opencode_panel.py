import json
from email.message import Message
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as panel
import opencode_panel


class OpenCodePanelTests(unittest.TestCase):
    def test_session_reload_serves_frontend_with_panel_cookie(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            web = root / "opencode-web"
            web.mkdir()
            (web / "index.html").write_text('<script src="/oc/assets/app.js"></script>')
            with (
                mock.patch.object(panel, "__file__", str(root / "app.py")),
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
