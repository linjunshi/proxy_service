import base64
import json
import unittest
from pathlib import Path

from subscription import SubscriptionError, parse_uris


FIXTURE = Path(__file__).with_name("fixtures") / "subscription-wms-1.45.1.txt"


def vmess_uri(**fields) -> bytes:
    payload = {
        "v": "2",
        "ps": "美国01-×0.5",
        "add": "vmess.example.invalid",
        "port": "18882",
        "id": "00000000-0000-4000-8000-000000000002",
        "aid": "0",
        "scy": "auto",
        "net": "ws",
        "type": "none",
        "host": None,
        "path": None,
        "tls": "",
        "fp": "chrome",
        **fields,
    }
    encoded = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    return b"vmess://" + encoded


class NativeSubscriptionTest(unittest.TestCase):
    def test_standard_and_urlsafe_base64_and_raw_text_preserve_names_and_order(self):
        raw = FIXTURE.read_bytes() + b"\n" + vmess_uri()
        expected = ["台湾01-×0.3", "日本01-×1-客户端", "美国05-×1-客户端", "协议测试节点", "美国01-×0.5"]
        for document in (raw, base64.b64encode(raw), base64.urlsafe_b64encode(raw).rstrip(b"=")):
            with self.subTest(encoding=document[:12]):
                nodes = parse_uris(document)
                self.assertEqual([n["name"] for n in nodes], expected)
                self.assertEqual([n["type"] for n in nodes], ["trojan", "ss", "anytls", "vless", "vmess"])

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

    def test_vmess_plain_websocket_matches_live_provider_entry(self):
        node = parse_uris(vmess_uri())[0]
        self.assertEqual(node, {
            "name": "美国01-×0.5",
            "type": "vmess",
            "server": "vmess.example.invalid",
            "port": 18882,
            "udp": True,
            "uuid": "00000000-0000-4000-8000-000000000002",
            "alterId": 0,
            "cipher": "auto",
            "tls": False,
            "skip-cert-verify": False,
            "client-fingerprint": "chrome",
            "network": "ws",
            "ws-opts": {"path": "/"},
        })

    def test_vmess_tls_websocket_grpc_and_ipv6(self):
        ws_tls = parse_uris(vmess_uri(
            ps="美国02-×1",
            add="[2001:db8::2]",
            port=443,
            aid=2,
            scy="aes-128-gcm",
            tls="tls",
            sni="vmess-tls.example.invalid",
            host="cdn.example.invalid",
            path="/vmess?ed=2048",
            allowInsecure=True,
            alpn="h2,http/1.1",
        ))[0]
        self.assertEqual(ws_tls["server"], "2001:db8::2")
        self.assertEqual(ws_tls["port"], 443)
        self.assertEqual(ws_tls["alterId"], 2)
        self.assertEqual(ws_tls["cipher"], "aes-128-gcm")
        self.assertTrue(ws_tls["tls"])
        self.assertTrue(ws_tls["skip-cert-verify"])
        self.assertEqual(ws_tls["servername"], "vmess-tls.example.invalid")
        self.assertEqual(ws_tls["alpn"], ["h2", "http/1.1"])
        self.assertEqual(ws_tls["ws-opts"], {
            "path": "/vmess",
            "headers": {"Host": "cdn.example.invalid"},
            "max-early-data": 2048,
            "early-data-header-name": "Sec-WebSocket-Protocol",
        })

        grpc_node = parse_uris(vmess_uri(
            ps="美国03-grpc",
            net="grpc",
            type="gun",
            path="gun-service",
            tls="tls",
        ))[0]
        self.assertEqual(grpc_node["network"], "grpc")
        self.assertEqual(grpc_node["grpc-opts"], {"grpc-service-name": "gun-service"})

    def test_hysteria2_and_tuic_links_preserve_quic_and_obfs_options(self):
        pin = ":".join(["AB"] * 32)
        raw = b"\n".join([
            (
                f"hy2://user%3A1:pw%402@hy2.example.invalid:8443"
                f"?sni=hy2-sni.example.invalid&insecure=1&obfs=salamander&obfs-password=obfs-pw"
                f"&mport=20000-30000&upmbps=50&downmbps=200&pinSHA256={pin}#香港HY2"
            ).encode("utf-8"),
            (
                "tuic://00000000-0000-4000-8000-000000000003:tuic%40pw@tuic.example.invalid:443"
                "?sni=tuic-sni.example.invalid&alpn=h3&congestion_control=bbr"
                "&udp_relay_mode=quic&disable_sni=1#日本TUIC"
            ).encode("utf-8"),
        ])
        hy2, tuic = parse_uris(raw)
        self.assertEqual(hy2["type"], "hysteria2")
        self.assertEqual(hy2["password"], "user:1:pw@2")
        self.assertEqual(hy2["obfs"], "salamander")
        self.assertEqual(hy2["obfs-password"], "obfs-pw")
        self.assertEqual(hy2["ports"], "20000-30000")
        self.assertEqual((hy2["up"], hy2["down"]), ("50", "200"))
        self.assertEqual(hy2["fingerprint"], "ab" * 32)
        self.assertTrue(hy2["skip-cert-verify"])

        self.assertEqual(tuic["type"], "tuic")
        self.assertEqual(tuic["uuid"], "00000000-0000-4000-8000-000000000003")
        self.assertEqual(tuic["password"], "tuic@pw")
        self.assertEqual(tuic["congestion-controller"], "bbr")
        self.assertEqual(tuic["udp-relay-mode"], "quic")
        self.assertTrue(tuic["disable-sni"])
        self.assertEqual(tuic["alpn"], ["h3"])

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
            b"vmess://not-valid-base64-SECRET!!!",
            b"vmess://" + base64.b64encode(b"{not-json-SECRET}"),
            vmess_uri(id="SECRET"),
            vmess_uri(ps="", id="00000000-0000-4000-8000-000000000009", host="SECRET"),
            vmess_uri(tls="reality", sni="SECRET"),
            vmess_uri(type="http", host="SECRET"),
            vmess_uri(extraSecret="SECRET"),
            b"hy2://SECRET@server:443?obfs=salamander#node",
            b"hy2://SECRET@server:443?obfs=unknown#node",
            b"hy2://SECRET@server:443?mport=bad-SECRET#node",
            b"tuic://00000000-0000-4000-8000-000000000003@server:443#SECRET",
            b"tuic://00000000-0000-4000-8000-000000000003:SECRET@server:443?congestion_control=unknown#node",
        ]
        for bad in malformed:
            with self.subTest(entry=bad[:32]), self.assertRaises(SubscriptionError) as raised:
                parse_uris(FIXTURE.read_bytes() + b"\n" + bad)
            self.assertNotIn("SECRET", str(raised.exception))

    def test_duplicate_names_and_invalid_documents_are_rejected(self):
        for value in (b"", b"not base64!", b"\xff", b"trojan://pw@server:443#same\ntrojan://other@host:443#same"):
            with self.subTest(value=value), self.assertRaises(SubscriptionError):
                parse_uris(value)
