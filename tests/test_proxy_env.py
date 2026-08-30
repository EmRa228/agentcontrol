import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xray_client import apply_proxy_env, load_runtime_proxy_env


class ProxyEnvTests(unittest.TestCase):
    def test_apply_proxy_env_disabled_strips_proxy(self):
        base = {
            "HTTP_PROXY": "http://127.0.0.1:30229",
            "HTTPS_PROXY": "http://127.0.0.1:30229",
            "NODE_USE_ENV_PROXY": "1",
            "HOME": "/root",
        }
        with mock.patch("xray_client.load_client_settings", return_value={"enabled": False}):
            env = apply_proxy_env(base.copy())
        self.assertNotIn("HTTP_PROXY", env)
        self.assertNotIn("NODE_USE_ENV_PROXY", env)
        self.assertEqual(env["HOME"], "/root")

    def test_load_runtime_proxy_env_disabled_returns_empty(self):
        with mock.patch("xray_client.load_client_settings", return_value={"enabled": False}):
            self.assertEqual(load_runtime_proxy_env(), {})


if __name__ == "__main__":
    unittest.main()
