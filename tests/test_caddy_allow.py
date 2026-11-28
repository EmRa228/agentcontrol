import unittest
from unittest import mock

import app as panel


class CaddyAllowTests(unittest.TestCase):
    def setUp(self):
        panel._CADDY_ALLOW_CACHE.clear()

    def test_configured_hostname_is_allowed_without_dns(self):
        with mock.patch.dict(panel.CFG, {"public_hostname": "Panel.Example.com"}):
            self.assertTrue(panel.domain_resolves_here("panel.example.com."))
            self.assertFalse(panel.domain_resolves_here("other.example.com"))

    def test_domain_pointing_here_is_allowed(self):
        with (
            mock.patch.dict(panel.CFG, {"public_hostname": ""}),
            mock.patch.object(panel, "_local_address_set", return_value=panel._normalized_ip_set(["203.0.113.5"])),
            mock.patch.object(panel.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("203.0.113.5", 0))]),
        ):
            self.assertTrue(panel.domain_resolves_here("new.example.com"))

    def test_remote_domain_is_rejected_and_cached(self):
        with (
            mock.patch.dict(panel.CFG, {"public_hostname": ""}),
            mock.patch.object(panel, "_local_address_set", return_value=panel._normalized_ip_set(["203.0.113.5"])),
            mock.patch.object(panel.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("198.51.100.9", 0))]),
        ):
            self.assertFalse(panel.domain_resolves_here("attacker.example.net"))
            self.assertIn("attacker.example.net", panel._CADDY_ALLOW_CACHE)
            self.assertFalse(panel.domain_resolves_here("attacker.example.net"))

    def test_endpoint_only_answers_loopback(self):
        with (
            mock.patch.dict(panel.CFG, {"public_hostname": "panel.example.com"}),
            panel.app.test_client() as client,
        ):
            denied = client.get(
                "/api/caddy/allow?domain=panel.example.com",
                environ_base={"REMOTE_ADDR": "198.51.100.9"},
            )
            self.assertEqual(denied.status_code, 403)
            allowed = client.get(
                "/api/caddy/allow?domain=panel.example.com",
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
            self.assertEqual(allowed.status_code, 200)
            missing = client.get(
                "/api/caddy/allow",
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
            self.assertEqual(missing.status_code, 400)


if __name__ == "__main__":
    unittest.main()
