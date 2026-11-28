import tempfile
import unittest
from pathlib import Path

import yaml

from panel.config import load_notifications, save_notifications


class NotificationConfigTests(unittest.TestCase):
    def test_defaults_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = load_notifications(Path(tmp) / "missing.yaml")
            self.assertTrue(config.enabled)
            self.assertEqual(config.min_severity, "warning")
            self.assertTrue(config.event_enabled("disk"))
            self.assertFalse(config.channel("telegram").enabled)

    def test_load_and_redact(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notifications.yaml"
            path.write_text(
                yaml.safe_dump(
                    {
                        "enabled": True,
                        "min_severity": "critical",
                        "events": {"disk": False},
                        "channels": {"telegram": {"enabled": True, "bot_token": "secret", "chat_id": "1"}},
                    }
                ),
                encoding="utf-8",
            )
            config = load_notifications(path)
            self.assertEqual(config.min_severity, "critical")
            self.assertFalse(config.event_enabled("disk"))
            self.assertTrue(config.channel("telegram").enabled)
            redacted = config.to_dict()
            self.assertTrue(redacted["channels"]["telegram"]["bot_token_set"])
            self.assertEqual(redacted["channels"]["telegram"]["bot_token"], "")

    def test_save_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "in.yaml"
            source.write_text(
                "channels:\n  webhook:\n    enabled: true\n    url: https://example.test/hook\n",
                encoding="utf-8",
            )
            config = load_notifications(source)
            target = Path(tmp) / "out.yaml"
            save_notifications(config, target)
            again = load_notifications(target)
            self.assertTrue(again.channel("webhook").enabled)
            self.assertEqual(again.channel("webhook").get("url"), "https://example.test/hook")

    def test_severity_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "n.yaml"
            path.write_text("min_severity: warning\n", encoding="utf-8")
            config = load_notifications(path)
            self.assertFalse(config.severity_allowed("info"))
            self.assertTrue(config.severity_allowed("warning"))
            self.assertTrue(config.severity_allowed("critical"))

    def test_webhook_signature_matches_body(self):
        import hashlib
        import hmac
        import json

        from panel.notify import Notifier

        captured = {}

        class FakeNotifier(Notifier):
            @staticmethod
            def _post_raw(url, body, headers=None, timeout=10):
                captured["body"] = body
                captured["headers"] = headers or {}
                return 200, b"ok"

        notifier = FakeNotifier(None, Path("/nonexistent"))
        result = notifier._webhook(
            {"url": "https://example.test/hook", "secret": "s3cret"},
            "warning",
            "title",
            "message",
            {"event": "disk"},
        )
        self.assertTrue(result["ok"])
        expected = "sha256=" + hmac.new(b"s3cret", captured["body"], hashlib.sha256).hexdigest()
        self.assertEqual(captured["headers"]["X-AgentControl-Signature"], expected)
        self.assertEqual(json.loads(captured["body"])["title"], "title")


if __name__ == "__main__":
    unittest.main()
