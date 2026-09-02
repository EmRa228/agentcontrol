import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xray_client import default_client_settings, load_client_settings, set_proxy_enabled


class WizardDirectModeTests(unittest.TestCase):
    def test_default_client_settings_proxy_disabled(self):
        self.assertFalse(default_client_settings()["enabled"])

    def test_set_proxy_enabled_false_persists_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            client_file = Path(tmp) / "xray-client.yaml"
            env_file = Path(tmp) / "env"
            with (
                mock.patch("xray_client.DEFAULT_CLIENT_FILE", client_file),
                mock.patch("xray_client.write_runtime_env", side_effect=lambda _: env_file),
                mock.patch("xray_client.record_status"),
                mock.patch("xray_client.build_status_report", return_value={}),
            ):
                result = set_proxy_enabled(False, restart=False)

            self.assertTrue(result["ok"])
            self.assertFalse(result["enabled"])
            settings = load_client_settings()
            self.assertFalse(settings["enabled"])


if __name__ == "__main__":
    unittest.main()
