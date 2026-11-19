import base64
import unittest
from pathlib import Path

from subscription import SubscriptionError, parse_uris


FIXTURE = Path(__file__).with_name("fixtures") / "subscription-wms-1.45.1.txt"


class NativeSubscriptionTest(unittest.TestCase):
    def test_standard_and_urlsafe_base64_and_raw_text_preserve_names_and_order(self):
        raw = FIXTURE.read_bytes()
        expected = ["台湾01-×0.3", "日本01-×1-客户端", "美国05-×1-客户端", "协议测试节点"]
        for document in (raw, base64.b64encode(raw), base64.urlsafe_b64encode(raw).rstrip(b"=")):
            with self.subTest(encoding=document[:12]):
                nodes = parse_uris(document)
                self.assertEqual([n["name"] for n in nodes], expected)
                self.assertEqual([n["type"] for n in nodes], ["trojan", "ss", "anytls", "vless"])

    def test_ss2022_keeps_both_identity_keys(self):
        node = parse_uris(FIXTURE.read_bytes())[1]
        self.assertEqual(node["cipher"], "2022-blake3-aes-128-gcm")
        self.assertEqual(node["password"], "AAECAwQFBgcICQoLDA0ODw==:EBESExQVFhcYGRobHB0eHw==")
        self.assertEqual((node["port"], node["server"]), (32067, "wm1jp.000.com"))

    def test_tls_names_cert_pins_and_verification_flags(self):
        trojan, _, anytls, vless = parse_uris(FIXTURE.read_bytes())
        self.assertTrue(trojan["skip-cert-verify"])
        self.assertEqual(trojan["sni"], "certificate.example.invalid")
        self.assertEqual(anytls["fingerprint"], "0123456789abcdef" * 4)
        self.assertEqual(anytls["client-fingerprint"], "chrome")
        self.assertTrue(anytls["skip-cert-verify"])
        self.assertTrue(vless["tls"])
        self.assertFalse(vless["skip-cert-verify"])
        self.assertEqual(vless["servername"], "tls.example.invalid")

    def test_vless_websocket_host_path_and_early_data(self):
        node = parse_uris(FIXTURE.read_bytes())[3]
        self.assertEqual(node["uuid"], "00000000-0000-4000-8000-000000000001")
        self.assertEqual(node["ws-opts"], {
            "path": "/socket", "headers": {"Host": "cdn.example.invalid"},
            "max-early-data": 2048, "early-data-header-name": "Sec-WebSocket-Protocol",
        })

    def test_escaped_credentials_unicode_and_ipv6(self):
        node = parse_uris(b"trojan://a%3Ab%40c@[2001:db8::1]:443?sni=cert.test#%E6%97%A5%E6%9C%AC")[0]
        self.assertEqual(node["name"], "日本")
        self.assertEqual(node["password"], "a:b@c")
        self.assertEqual(node["server"], "2001:db8::1")
        self.assertFalse(node["skip-cert-verify"])

    def test_unknown_or_malformed_entry_refuses_the_entire_feed_without_exposing_secrets(self):
        malformed = [
            b"ss://SECRET@server:443#node",
            b"ss://aes-256-gcm:SECRET@server:443?plugin=unknown#node",
            b"trojan://SECRET@server:bad#node", b"trojan://SECRET@server:70000#node",
            b"trojan://SECRET@server:443?insecure=maybe#node",
            b"trojan://SECRET@server:443?insecure=1&insecure=0#node",
            b"trojan://SECRET@server:443?allowInsecure=1&insecure=0#node",
            b"anytls://SECRET@server:443?type=ws#node",
            b"unknown://SECRET@server:443#node",
            b"trojan://SECRET@server:443?new-security-option=1#node",
            b"trojan://SECRET@server:443?security=reality#node",
            b"trojan://SECRET@server:443?type=tcp&path=%2Fws#node",
            b"trojan://SECRET@server:443?sni=%ZZ#node",
            b"trojan://SECRET@server:443#",
        ]
        for bad in malformed:
            with self.subTest(entry=bad), self.assertRaises(SubscriptionError) as raised:
                parse_uris(FIXTURE.read_bytes() + b"\n" + bad)
            self.assertNotIn("SECRET", str(raised.exception))

    def test_duplicate_names_and_invalid_documents_are_rejected(self):
        for value in (b"", b"not base64!", b"\xff", b"trojan://pw@server:443#same\ntrojan://other@host:443#same"):
            with self.subTest(value=value), self.assertRaises(SubscriptionError):
                parse_uris(value)
