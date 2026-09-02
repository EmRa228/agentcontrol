import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from proxy_pool import (
    _parse_subscription_body,
    add_proxy_link,
    default_pool,
    load_pool,
    save_pool,
)
from xray_client import apply_proxy_env, load_runtime_proxy_env


class SubscriptionParseTests(unittest.TestCase):
    def test_plain_links(self):
        body = "vless://uuid@1.2.3.4:443?security=reality\n# comment\ntrojan://pw@host:443"
        links = _parse_subscription_body(body)
        self.assertEqual(len(links), 2)
        self.assertTrue(links[0].startswith("vless://"))

    def test_base64_subscription(self):
        import base64

        raw = "vless://a@b:443?security=none\nvmess://eyJhZGQiOiJ4In0"
        encoded = base64.b64encode(raw.encode()).decode()
        links = _parse_subscription_body(encoded)
        self.assertGreaterEqual(len(links), 1)


class ProxyPoolTests(unittest.TestCase):
    def test_add_proxy_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool_file = Path(tmp) / "pool.yaml"
            with mock.patch("proxy_pool.POOL_FILE", pool_file):
                pool = default_pool()
                save_pool(pool)
                entry = add_proxy_link(
                    load_pool(),
                    "vless://11111111-1111-1111-1111-111111111111@1.2.3.4:443?"
                    "security=reality&sni=example.com&pbk=abc&sid=&fp=chrome",
                    name="test",
                )
                self.assertEqual(entry["name"], "test")
                self.assertEqual(len(load_pool()["proxies"]), 1)


class ProxyEnvStrictTests(unittest.TestCase):
    def test_no_proxy_bypasses_cursor_domains(self):
        with mock.patch("xray_client._proxy_mode_enabled", return_value=True):
            with mock.patch(
                "xray_client.proxy_url",
                return_value="http://127.0.0.1:30229",
            ):
                env = load_runtime_proxy_env()
        self.assertEqual(env["NO_PROXY"], "localhost,127.0.0.1,::1")
        self.assertEqual(env["AGENTCONTROL_PROXY_MODE"], "1")

    def test_apply_proxy_env_disabled_strips_proxy(self):
        base = {"HTTP_PROXY": "http://127.0.0.1:30229", "HOME": "/root"}
        with mock.patch("xray_client._proxy_mode_enabled", return_value=False):
            env = apply_proxy_env(base.copy())
        self.assertNotIn("HTTP_PROXY", env)


if __name__ == "__main__":
    unittest.main()
