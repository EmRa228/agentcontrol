import unittest
from pathlib import Path
from unittest import mock

import app


class HostRuntimeTests(unittest.TestCase):
    def test_running_inside_docker_dockerenv(self):
        with mock.patch("app.Path") as path_mock:
            path_mock.return_value.is_file.return_value = True
            self.assertTrue(app.running_inside_docker())

    def test_running_inside_docker_cgroup(self):
        with (
            mock.patch.object(Path, "is_file", return_value=False),
            mock.patch.object(Path, "read_text", return_value="0::/docker/abc"),
        ):
            self.assertTrue(app.running_inside_docker())

    def test_worker_runtime_error_inside_docker(self):
        with mock.patch.object(app, "running_inside_docker", return_value=True):
            self.assertIn("inside Docker", app.worker_runtime_error("nictry") or "")

    def test_worker_runtime_error_compose_without_sock(self):
        with (
            mock.patch.object(app, "running_inside_docker", return_value=False),
            mock.patch.object(app, "docker_sock_available", return_value=False),
        ):
            err = app.worker_runtime_error("nictry")
            self.assertIn("docker.sock", err or "")

    def test_worker_runtime_error_ok_on_host(self):
        with (
            mock.patch.object(app, "running_inside_docker", return_value=False),
            mock.patch.object(app, "docker_sock_available", return_value=True),
            mock.patch.object(app, "folder_path", return_value=Path("/root/plain")),
            mock.patch.object(app, "project_needs_docker", return_value=False),
        ):
            self.assertIsNone(app.worker_runtime_error("plain"))


if __name__ == "__main__":
    unittest.main()
