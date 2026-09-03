import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app


class WorkerDataDirTests(unittest.TestCase):
    def test_worker_data_dir_creates_per_project_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(app, "WORKER_DATA_ROOT", Path(tmp) / "worker-data"):
                path = app.worker_data_dir("myproject")
                self.assertEqual(path, Path(tmp) / "worker-data" / "myproject")
                self.assertTrue(path.is_dir())

    def test_worker_data_dirs_are_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(app, "WORKER_DATA_ROOT", Path(tmp) / "worker-data"):
                a = app.worker_data_dir("alpha")
                b = app.worker_data_dir("beta")
                self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
