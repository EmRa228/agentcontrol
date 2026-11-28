import tempfile
import time
import unittest
from pathlib import Path

from panel.db import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "panel.db")
        self.store.initialize()

    def tearDown(self):
        self.tmp.cleanup()

    def test_workers_roundtrip(self):
        self.store.replace_workers(
            {"demo": {"pid": 123, "worker_id": "uuid", "started_at": "now", "log_file": "x", "mgmt_port": 32001}}
        )
        state = self.store.workers()
        self.assertEqual(state["demo"]["pid"], 123)
        self.assertEqual(state["demo"]["mgmt_port"], 32001)
        self.store.replace_workers({})
        self.assertEqual(self.store.workers(), {})

    def test_audit(self):
        self.store.audit_add("login", actor="panel", role="admin", ip="127.0.0.1", target="x")
        self.store.audit_add("worker.start", actor="token", target="demo")
        rows = self.store.audit_list(limit=10)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["action"], "worker.start")
        self.assertEqual(self.store.audit_count(), 2)
        self.assertEqual(len(self.store.audit_list(action="login")), 1)
        self.assertEqual(len(self.store.audit_list(actor="token")), 1)

    def test_metrics(self):
        now = time.time()
        self.store.metrics_add({"t": now, "cpu": 12, "ram": 40, "disk": 50, "swap": 0, "net": 1})
        self.store.metrics_add({"t": now + 1, "cpu": 13, "ram": 41, "disk": 50, "swap": 0, "net": 2})
        points = self.store.metrics_since(now - 10)
        self.assertEqual(len(points), 2)
        self.assertEqual(points[-1]["cpu"], 13)

    def test_health(self):
        self.store.health_add({"status": "ok", "version": "1.0"})
        points = self.store.health_since(time.time() - 10)
        self.assertEqual(points[0]["status"], "ok")

    def test_incidents_throttle_marker(self):
        self.store.incident_upsert("disk", "disk", "warning", "85%")
        self.assertIsNotNone(self.store.incident_get("disk"))
        self.store.incident_mark_notified("disk")
        self.assertIsNotNone(self.store.incident_get("disk")["last_notified_ts"])
        self.store.incident_upsert("disk", "disk", "info", "ok", state="resolved")
        self.assertEqual(self.store.incident_get("disk")["state"], "resolved")


if __name__ == "__main__":
    unittest.main()
