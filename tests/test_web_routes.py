import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("STATE_DIR", tempfile.mkdtemp(prefix="agentcontrol-routes-"))
os.environ.setdefault("PANEL_DISABLE_BACKGROUND", "1")

import app  # noqa: E402
from panel import security  # noqa: E402


class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.password_file = Path(cls.tmp.name) / "auth-password"
        security.write_password_file(cls.password_file, "test-password-123")
        cls.original_password_file = app.CFG.get("auth_password_file")
        app.CFG["auth_password_file"] = str(cls.password_file)
        cls.token = security.create_token(app.STORE, "route-test", "admin")
        cls.headers = {"X-AgentControl-Token": cls.token["token"]}
        cls.client = app.app.test_client()
        session_token = app.SESSIONS.create(3600, ip="127.0.0.1", ua="test")
        cls.client.set_cookie("agentcontrol_session", session_token)

    @classmethod
    def tearDownClass(cls):
        app.STORE.token_revoke(cls.token["id"])
        app.CFG["auth_password_file"] = cls.original_password_file
        cls.tmp.cleanup()

    def test_health_open(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertIn("version", response.get_json())

    def test_index_uses_external_module_and_meta(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"ac-hostname", response.data)
        self.assertIn(b"/static/panel.js", response.data)
        self.assertIn("Content-Security-Policy", response.headers)
        self.assertNotIn("unsafe-inline'", response.headers["Content-Security-Policy"].split("script-src")[1].split(";")[0])

    def test_static_panel_js(self):
        response = self.client.get("/static/panel.js")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"ACTIONS", response.data)

    def test_public_status(self):
        response = self.client.get("/api/public/status")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertIn("status", data)
        self.assertIn("checks", data)

    def test_status_page(self):
        response = self.client.get("/status")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"AgentControl status", response.data)

    def test_token_auth_and_v1_alias(self):
        response = self.client.get("/api/folders", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        v1 = self.client.get("/api/v1/folders", headers=self.headers)
        self.assertEqual(v1.status_code, 200)

    def test_missing_token_rejected(self):
        anon = app.app.test_client()
        response = anon.get("/api/health")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["code"], "unauthorized")

    def test_health_endpoint(self):
        response = self.client.get("/api/health", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("checks", response.get_json())

    def test_openapi(self):
        response = self.client.get("/api/openapi.json")
        self.assertEqual(response.status_code, 200)
        spec = response.get_json()
        self.assertEqual(spec["openapi"], "3.0.3")
        self.assertIn("/api/health", spec["paths"])

    def test_prometheus(self):
        response = self.client.get("/metrics", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"agentcontrol_up 1", response.data)

    def test_audit_endpoint(self):
        response = self.client.get("/api/audit?limit=5", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("events", response.get_json())

    def test_notifications_roundtrip(self):
        response = self.client.get("/api/notifications", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("config", response.get_json())

    def test_project_details_404(self):
        response = self.client.get("/api/projects/does-not-exist/details", headers=self.headers)
        self.assertEqual(response.status_code, 404)

    def test_setup_endpoint_requires_token_for_remote(self):
        environ = {"REMOTE_ADDR": "203.0.113.9"}
        response = self.client.post("/api/setup/password", json={"password": "abcdefgh", "confirm": "abcdefgh"}, environ_base=environ)
        self.assertIn(response.status_code, {400, 403})

    def test_cross_origin_post_rejected(self):
        response = self.client.post(
            "/api/tokens",
            json={"name": "x", "role": "read"},
            headers={**self.headers, "Origin": "https://evil.example"},
        )
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
