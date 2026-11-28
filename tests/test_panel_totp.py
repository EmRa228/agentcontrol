import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pyotp

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
                code = pyotp.TOTP(secret).now()
                ok, err = panel_totp.enable_totp(secret, code)
                self.assertTrue(ok, err)
                self.assertTrue(panel_totp.verify_login_code(code))

    def test_begin_setup_creates_secret_and_qr_without_enabling(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "totp.json"
            with mock.patch.object(panel_totp, "DEFAULT_TOTP_FILE", path):
                payload = panel_totp.begin_totp_setup("panel.example")
                self.assertTrue(payload["secret"])
                self.assertIn("otpauth://totp/", payload["provisioning_uri"])
                self.assertTrue(payload["qr_data_uri"].startswith("data:image/svg+xml"))
                self.assertFalse(panel_totp.totp_enabled())
                # Re-opening setup before verifying reuses the same pending secret.
                self.assertEqual(panel_totp.begin_totp_setup("panel.example")["secret"], payload["secret"])

    def test_enable_pending_activates_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "totp.json"
            with mock.patch.object(panel_totp, "DEFAULT_TOTP_FILE", path):
                secret = panel_totp.begin_totp_setup("panel.example")["secret"]
                ok, err = panel_totp.enable_pending_totp("000000" if pyotp.TOTP(secret).now() != "000000" else "111111")
                self.assertFalse(ok)
                self.assertIn("invalid", err)
                code = pyotp.TOTP(secret).now()
                ok, err = panel_totp.enable_pending_totp(code)
                self.assertTrue(ok, err)
                self.assertTrue(panel_totp.totp_enabled())
                self.assertTrue(panel_totp.verify_login_code(code))
                self.assertFalse(panel_totp.load_totp_state()["pending_secret"])

    def test_rotate_keeps_current_factor_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "totp.json"
            with mock.patch.object(panel_totp, "DEFAULT_TOTP_FILE", path):
                old_secret = panel_totp.begin_totp_setup("panel.example")["secret"]
                code = pyotp.TOTP(old_secret).now()
                self.assertTrue(panel_totp.enable_pending_totp(code)[0])
                payload = panel_totp.rotate_totp_secret("panel.example")
                self.assertNotEqual(payload["secret"], old_secret)
                self.assertTrue(payload["enabled"])
                # The old authenticator still works until the new secret is verified.
                self.assertTrue(panel_totp.verify_login_code(pyotp.TOTP(old_secret).now()))
                new_code = pyotp.TOTP(payload["secret"]).now()
                self.assertTrue(panel_totp.enable_pending_totp(new_code)[0])
                self.assertFalse(panel_totp.verify_login_code(pyotp.TOTP(old_secret).now()))

    def test_public_status_does_not_leak_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "totp.json"
            with mock.patch.object(panel_totp, "DEFAULT_TOTP_FILE", path):
                secret = panel_totp.begin_totp_setup("panel.example")["secret"]
                self.assertTrue(panel_totp.enable_pending_totp(pyotp.TOTP(secret).now())[0])
                status = panel_totp.public_totp_status("panel.example")
                self.assertTrue(status["enabled"])
                self.assertNotIn("provisioning_uri", status)
                self.assertNotIn(secret, repr(status))

    def test_disable_clears_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "totp.json"
            with mock.patch.object(panel_totp, "DEFAULT_TOTP_FILE", path):
                panel_totp.begin_totp_setup("panel.example")
                panel_totp.disable_totp()
                state = panel_totp.load_totp_state()
                self.assertFalse(state["enabled"])
                self.assertEqual(state["secret"], "")
                self.assertEqual(state["pending_secret"], "")

    def test_qr_falls_back_to_empty_string(self):
        with mock.patch.dict("sys.modules", {"segno": None}):
            self.assertEqual(panel_totp.totp_qr_data_uri("otpauth://totp/x"), "")


if __name__ == "__main__":
    unittest.main()
