import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import opencode_panel


class OpenCodePanelTests(unittest.TestCase):
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

    def test_serve_password_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"opencode_serve_password_file": f"{tmp}/pw"}
            first = opencode_panel.serve_password(cfg, "panel-secret")
            second = opencode_panel.serve_password(cfg, None)
            self.assertEqual(first, second)
            self.assertEqual(first, "panel-secret")


if __name__ == "__main__":
    unittest.main()