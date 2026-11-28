import tempfile
import unittest
from pathlib import Path

from panel import security
from panel.db import Store


class PasswordHashingTests(unittest.TestCase):
    def test_hash_and_verify(self):
        stored = security.hash_password("correct horse battery staple")
        self.assertTrue(security.password_is_hashed(stored))
        self.assertNotIn("correct horse", stored)
        self.assertTrue(security.verify_password("correct horse battery staple", stored))
        self.assertFalse(security.verify_password("wrong", stored))

    def test_legacy_plaintext_still_verifies(self):
        self.assertTrue(security.verify_password("old-password", "old-password"))
        self.assertFalse(security.verify_password("nope", "old-password"))

    def test_upgrade_legacy_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auth-password"
            path.write_text("legacy-pass\n", encoding="utf-8")
            self.assertTrue(security.upgrade_password_file(path, "legacy-pass"))
            stored = path.read_text(encoding="utf-8").strip()
            self.assertTrue(security.password_is_hashed(stored))
            self.assertTrue(security.verify_password("legacy-pass", stored))
            self.assertFalse(security.upgrade_password_file(path, "legacy-pass"))

    def test_write_password_file_never_plaintext(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auth-password"
            security.write_password_file(path, "s3cret-value")
            self.assertNotIn("s3cret-value", path.read_text(encoding="utf-8"))


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "panel.db")
        self.store.initialize()

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_and_verify(self):
        record = security.create_token(self.store, "fleet", "ops")
        self.assertTrue(record["token"].startswith("ac_"))
        actor = security.verify_token(record["token"], self.store)
        self.assertEqual(actor["name"], "fleet")
        self.assertEqual(actor["role"], "ops")
        self.assertEqual(self.store.token_list()[0]["name"], "fleet")

    def test_revoke(self):
        record = security.create_token(self.store, "temp", "read")
        self.assertTrue(self.store.token_revoke(record["id"]))
        self.assertIsNone(security.verify_token(record["token"], self.store))

    def test_invalid_role(self):
        with self.assertRaises(ValueError):
            security.create_token(self.store, "bad", "root")

    def test_unknown_token(self):
        self.assertIsNone(security.verify_token("ac_deadbeef_nope", self.store))

    def test_role_levels(self):
        self.assertTrue(security.role_allows("admin", "ops"))
        self.assertTrue(security.role_allows("ops", "read"))
        self.assertFalse(security.role_allows("read", "ops"))


class LoginLimiterTests(unittest.TestCase):
    def test_blocks_after_max_attempts(self):
        limiter = security.LoginLimiter(max_attempts=3, window_seconds=60, lockout_seconds=60)
        self.assertTrue(limiter.check("1.2.3.4")[0])
        for _ in range(2):
            limiter.failure("1.2.3.4")
        attempts, locked = limiter.failure("1.2.3.4")
        self.assertEqual(attempts, 3)
        self.assertEqual(locked, 60)
        allowed, retry = limiter.check("1.2.3.4")
        self.assertFalse(allowed)
        self.assertGreater(retry, 0)
        limiter.reset("1.2.3.4")
        self.assertTrue(limiter.check("1.2.3.4")[0])

    def test_other_clients_unaffected(self):
        limiter = security.LoginLimiter(max_attempts=2, window_seconds=60, lockout_seconds=60)
        limiter.failure("a")
        limiter.failure("a")
        self.assertFalse(limiter.check("a")[0])
        self.assertTrue(limiter.check("b")[0])


if __name__ == "__main__":
    unittest.main()
