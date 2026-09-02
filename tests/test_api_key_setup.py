import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as panel


class FakeStdout:
    def __init__(self, lines: list[str]):
        self._lines = list(lines)

    def readline(self) -> str:
        return self._lines.pop(0) if self._lines else ""


class FakeProc:
    def __init__(self, lines: list[str]):
        self.stdout = FakeStdout(lines)
        self.pid = 999
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = 0


class ApiKeySetupTests(unittest.TestCase):
    def test_write_api_key_strips_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            key_path = Path(tmp) / "api-key"
            cfg = dict(panel.CFG)
            cfg["api_key_file"] = str(key_path)
            with mock.patch.object(panel, "CFG", cfg):
                panel.write_api_key("  crsr_test\n")
            self.assertEqual(key_path.read_text(encoding="utf-8"), "crsr_test")

    def test_verify_rejects_non_crsr_prefix(self):
        result = panel.verify_cursor_api_key("not-a-cursor-key")
        self.assertFalse(result["valid"])
        self.assertIn("crsr_", str(result["error"]))

    def test_verify_accepts_authenticated_worker_output(self):
        fake_proc = FakeProc(
            ["Authenticating...\n", "Authenticated with API key\n"],
        )
        with (
            mock.patch.object(panel, "find_agent_bin", return_value="/usr/bin/agent"),
            mock.patch.object(panel.subprocess, "Popen", return_value=fake_proc),
            mock.patch.object(panel.os, "killpg"),
            mock.patch.object(panel.os, "getpgid", return_value=1234),
        ):
            result = panel.verify_cursor_api_key("crsr_" + "a" * 64)
        self.assertTrue(result["valid"])


if __name__ == "__main__":
    unittest.main()
