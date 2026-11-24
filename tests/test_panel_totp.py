import tempfile
import unittest
from pathlib import Path
from unittest import mock

import panel_totp


class PanelTotpTests(unittest.TestCase):
    def test_verify_disabled_allows_login(self):
        with mock.patch.object(panel_totp, "load_totp_state", return_value={"enabled": False, "secret": ""}):
            self.assertTrue(panel_totp.verify_login_code(""))

    def test_enable_and_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "totp.json"
            with mock.patch.object(panel_totp, "DEFAULT_TOTP_FILE", path):
                secret = panel_totp.generate_totp_secret()
                import pyotp

                code = pyotp.TOTP(secret).now()
                ok, err = panel_totp.enable_totp(secret, code)
                self.assertTrue(ok, err)
                self.assertTrue(panel_totp.verify_login_code(code))


if __name__ == "__main__":
    unittest.main()