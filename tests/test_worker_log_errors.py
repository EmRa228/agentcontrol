import unittest

import app


class WorkerLogErrorTests(unittest.TestCase):
    def test_ignores_verbose_hook_json(self):
        tail = (
            "11:56:50.246 INFO  Implementation: ctx=worker-mode "
            'meta={errors: [], configDirs: {}}'
        )
        self.assertEqual(app.detect_worker_log_errors(tail), [])

    def test_detects_cli_error_line(self):
        tail = "error: unknown option '--api-key-file'\nWorker is now running\n"
        errs = app.detect_worker_log_errors(tail)
        self.assertEqual(len(errs), 1)
        self.assertTrue(errs[0].startswith("error:"))

    def test_session_marker_limits_scan(self):
        old = "error: stale failure\n--- agentcontrol worker start 2026-01-01 ---\n"
        new = "11:56:50 INFO Private worker started\n"
        tail = old + new
        self.assertEqual(app.detect_worker_log_errors(tail), [])


if __name__ == "__main__":
    unittest.main()
