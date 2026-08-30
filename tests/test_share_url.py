import base64
import json
import unittest

from xray_client import build_outbound, build_share_url, merge_share_url, parse_share_url


VLESS_SAMPLE = (
    "vless://11111111-2222-3333-4444-555555555555@proxy.example.com:443"
    "?security=reality&encryption=none&pbk=ExamplePublicKeyBase64ValueForTestsOnly"
    "&headerType=none&fp=chrome&type=tcp&sni=www.example.com&sid=abcd1234ef567890"
    "#Example%20Node"
)

TROJAN_SAMPLE = (
    "trojan://ExampleTrojanPasswordForTestsOnly@proxy.example.com:6060"
    "?security=none&headerType=none&type=tcp#Example%20Trojan"
)

VMESS_SAMPLE = "vmess://" + base64.urlsafe_b64encode(
    json.dumps(
        {
            "v": "2",
            "ps": "example",
            "add": "proxy.example.com",
            "port": "443",
            "id": "11111111-2222-3333-4444-555555555555",
            "aid": "0",
            "net": "tcp",
            "type": "none",
            "host": "",
            "path": "",
            "tls": "",
        }
    ).encode("utf-8")
).decode("ascii").rstrip("=")


class ShareUrlTests(unittest.TestCase):
    def test_parse_vless_url(self):
        parsed = parse_share_url(VLESS_SAMPLE)
        self.assertEqual(parsed["protocol"], "vless")
        self.assertEqual(parsed["address"], "proxy.example.com")
        self.assertEqual(parsed["port"], 443)
        self.assertEqual(parsed["uuid"], "11111111-2222-3333-4444-555555555555")
        self.assertEqual(parsed["server_name"], "www.example.com")
        self.assertEqual(parsed["public_key"], "ExamplePublicKeyBase64ValueForTestsOnly")
        self.assertEqual(parsed["short_id"], "abcd1234ef567890")
        self.assertEqual(parsed["security"], "reality")
        self.assertEqual(parsed["network"], "tcp")
        self.assertEqual(parsed["flow"], "")

    def test_parse_trojan_url(self):
        parsed = parse_share_url(TROJAN_SAMPLE)
        self.assertEqual(parsed["protocol"], "trojan")
        self.assertEqual(parsed["address"], "proxy.example.com")
        self.assertEqual(parsed["port"], 6060)
        self.assertEqual(parsed["password"], "ExampleTrojanPasswordForTestsOnly")
        self.assertEqual(parsed["security"], "none")
        self.assertEqual(parsed["network"], "tcp")

    def test_parse_vmess_url(self):
        parsed = parse_share_url(VMESS_SAMPLE)
        self.assertEqual(parsed["protocol"], "vmess")
        self.assertEqual(parsed["address"], "proxy.example.com")
        self.assertEqual(parsed["port"], 443)
        self.assertEqual(parsed["uuid"], "11111111-2222-3333-4444-555555555555")

    def test_build_share_url_roundtrip(self):
        for sample in (VLESS_SAMPLE, TROJAN_SAMPLE):
            parsed = parse_share_url(sample)
            rebuilt = build_share_url(parsed)
            again = parse_share_url(rebuilt)
            self.assertEqual(again["protocol"], parsed["protocol"])
            self.assertEqual(again["address"], parsed["address"])
            self.assertEqual(again["port"], parsed["port"])

    def test_build_outbound_trojan(self):
        settings = merge_share_url({}, TROJAN_SAMPLE)
        outbound = build_outbound(settings)
        self.assertEqual(outbound["protocol"], "trojan")
        server = outbound["settings"]["servers"][0]
        self.assertEqual(server["password"], "ExampleTrojanPasswordForTestsOnly")
        self.assertEqual(outbound["streamSettings"]["security"], "none")
        self.assertEqual(outbound["streamSettings"]["network"], "tcp")

    def test_build_outbound_vless_without_flow(self):
        settings = merge_share_url({}, VLESS_SAMPLE)
        outbound = build_outbound(settings)
        users = outbound["settings"]["vnext"][0]["users"][0]
        self.assertNotIn("flow", users)
        self.assertEqual(outbound["streamSettings"]["network"], "tcp")
        self.assertEqual(outbound["streamSettings"]["security"], "reality")


if __name__ == "__main__":
    unittest.main()
