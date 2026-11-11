import tempfile
import unittest
from pathlib import Path
from unittest import mock

import worker_git


class WorkerGitTests(unittest.TestCase):
    def test_ensure_init_and_empty_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            cfg = {}
            result = worker_git.ensure_worker_git("demo", path, cfg)
            self.assertTrue(result["initialized"])
            self.assertTrue(result["empty_commit"])
            self.assertTrue((path / ".git").is_dir())

    def test_remote_from_marker_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / worker_git.GIT_REMOTE_MARKER).write_text(
                "git@git.example.com:org/demo.git\n", encoding="utf-8"
            )
            result = worker_git.ensure_worker_git("demo", path, cfg={})
            self.assertEqual(result["remote_url"], "git@git.example.com:org/demo.git")
            self.assertTrue(result["remote_added"])


if __name__ == "__main__":
    unittest.main()
