import unittest

from xray_client import build_outbound, build_vless_url, merge_vless_url, parse_vless_url


SAMPLE_URL = (
    "vless://11111111-2222-3333-4444-555555555555@proxy.example.com:443"
    "?security=reality&encryption=none&pbk=ExamplePublicKeyBase64ValueForTestsOnly"
    "&headerType=none&fp=chrome&type=tcp&sni=www.example.com&sid=abcd1234ef567890"
    "#Example%20Node"
)


class VlessUrlTests(unittest.TestCase):
    def test_parse_vless_url(self):
        parsed = parse_vless_url(SAMPLE_URL)
        self.assertEqual(parsed["address"], "proxy.example.com")
        self.assertEqual(parsed["port"], 443)
        self.assertEqual(parsed["uuid"], "11111111-2222-3333-4444-555555555555")
        self.assertEqual(parsed["server_name"], "www.example.com")
        self.assertEqual(parsed["public_key"], "ExamplePublicKeyBase64ValueForTestsOnly")
        self.assertEqual(parsed["short_id"], "abcd1234ef567890")
        self.assertEqual(parsed["fingerprint"], "chrome")
        self.assertEqual(parsed["network"], "tcp")
        self.assertEqual(parsed["flow"], "")

    def test_build_vless_url_roundtrip(self):
        parsed = parse_vless_url(SAMPLE_URL)
        rebuilt = build_vless_url(parsed)
        again = parse_vless_url(rebuilt)
        self.assertEqual(again["address"], parsed["address"])
        self.assertEqual(again["port"], parsed["port"])
        self.assertEqual(again["uuid"], parsed["uuid"])
        self.assertEqual(again["server_name"], parsed["server_name"])
        self.assertEqual(again["public_key"], parsed["public_key"])
        self.assertEqual(again["short_id"], parsed["short_id"])

    def test_build_outbound_omits_empty_flow(self):
        settings = merge_vless_url({}, SAMPLE_URL)
        outbound = build_outbound(settings)
        users = outbound["settings"]["vnext"][0]["users"][0]
        self.assertNotIn("flow", users)
        self.assertEqual(outbound["streamSettings"]["network"], "tcp")
        self.assertEqual(
            outbound["streamSettings"]["realitySettings"]["publicKey"],
            "ExamplePublicKeyBase64ValueForTestsOnly",
        )


if __name__ == "__main__":
    unittest.main()
