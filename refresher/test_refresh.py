"""Tests for the parts of the refresher that decide what reaches mihomo.

The fixtures beside this file mirror the three bodies the subscription host is known
to return (CONTEXT.md, "The dialect table"). Two of them are the ones that must never
be published: publishing either would leave the core with no usable provider.

Run: python3 -m unittest discover -s refresher
"""

from __future__ import annotations

import http.client
import json
import os
import re
import tempfile
import unittest
import urllib.error
from unittest import mock
from pathlib import Path

import yaml

import refresh

FIXTURES = Path(__file__).with_name("fixtures")
TAIWAN = re.compile(refresh.DEFAULT_REGION_FILTER)


def body(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class SelectTest(unittest.TestCase):
    def test_keeps_only_the_named_region(self):
        kept = refresh.select(body("subscription-clash-meta.yaml"), TAIWAN, None)
        self.assertEqual(
            [proxy["name"] for proxy in kept],
            ["台湾01-×1-客户端", "台湾02-×2-客户端", "臺灣03-×1-客户端"],
        )

    def test_exclude_drops_expensive_multipliers(self):
        kept = refresh.select(body("subscription-clash-meta.yaml"), TAIWAN, re.compile(r"×[2-9]"))
        self.assertEqual([proxy["name"] for proxy in kept], ["台湾01-×1-客户端", "臺灣03-×1-客户端"])

    def test_keeps_every_field_the_node_needs(self):
        first = refresh.select(body("subscription-clash-meta.yaml"), TAIWAN, None)[0]
        self.assertEqual(first["type"], "trojan")
        self.assertEqual(first["server"], "tw01.example.invalid")
        self.assertEqual(first["port"], 443)

    def test_a_filter_that_matches_nothing_yields_nothing(self):
        self.assertEqual(refresh.select(body("subscription-clash-meta.yaml"), re.compile("火星"), None), [])

    def test_the_base64_dialect_is_refused(self):
        with self.assertRaises(refresh.RefreshError):
            refresh.select(body("subscription-base64.txt"), TAIWAN, None)

    def test_an_html_error_page_is_refused(self):
        with self.assertRaises(refresh.RefreshError):
            refresh.select(body("subscription-error.html"), TAIWAN, None)

    def test_a_proxy_missing_what_mihomo_needs_is_refused(self):
        document = "proxies:\n  - name: 台湾09-x1\n    type: trojan\n".encode()
        with self.assertRaises(refresh.RefreshError):
            refresh.select(document, TAIWAN, None)


class PublishTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.path = self.directory / "nodes.yaml"

    def test_writes_a_provider_document_mihomo_can_read(self):
        nodes = refresh.select(body("subscription-clash-meta.yaml"), TAIWAN, None)
        refresh.publish(nodes, self.path)
        written = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        self.assertEqual([proxy["name"] for proxy in written["proxies"]], [n["name"] for n in nodes])

    def test_leaves_no_staging_file_behind(self):
        refresh.publish([{"name": "台湾01", "type": "trojan", "server": "x"}], self.path)
        self.assertEqual([entry.name for entry in self.directory.iterdir()], ["nodes.yaml"])

    def test_replaces_the_previous_list_wholly(self):
        refresh.publish([{"name": "旧", "type": "trojan", "server": "x"}], self.path)
        refresh.publish([{"name": "新", "type": "trojan", "server": "y"}], self.path)
        written = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        self.assertEqual([proxy["name"] for proxy in written["proxies"]], ["新"])


class ServerMapTest(unittest.TestCase):
    MAP = json.dumps([
        {"domain": "wm1jp.000.com", "targets": ["203.0.113.25"], "strategy": "priority", "ip": "203.0.113.25"},
        {"domain": "client.example.invalid", "targets": ["203.0.113.26"], "strategy": "priority", "ip": "203.0.113.26"},
    ]).encode()

    @staticmethod
    def node(kind="ss", **fields):
        return {"name": "日本01-×1-客户端", "type": kind, "server": "wm1jp.000.com", "port": 32067,
                "password": "test-node-password", **fields}

    def test_the_official_encrypted_map_decodes_the_japan_alias(self):
        result = refresh.apply_server_map([self.node()], body("server-map-client-1.42.5.txt"))
        self.assertEqual(result[0]["server"], "117.184.28.50")

    def test_damaged_encrypted_map_is_rejected(self):
        document = body("server-map-client-1.42.5.txt").strip()
        for damaged in (document[:-4], b"not base64!", document[:32] + b"AAAA" + document[36:]):
            with self.subTest(damaged=damaged[:20]):
                with self.assertRaises(refresh.RefreshError):
                    refresh.apply_server_map([self.node()], damaged)

    def test_refresh_publishes_the_same_client_nodes_with_official_server_addresses(self):
        nodes = [self.node(), self.node("anytls", name="美国V01-×1.5-客户端",
                                       server="client.example.invalid", sni="", **{"skip-cert-verify": True})]
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {
            "PANEL_BASE_URL": "https://panel.example.invalid",
            "PANEL_EMAIL": "operator@example.invalid", "PANEL_PASSWORD": "test-panel-password",
            "PROXY_REGION_FILTER": "客户端", "PROXY_REGION_EXCLUDE": "香港",
            "NODES_PATH": str(Path(directory) / "nodes.yaml"),
        }, clear=True):
            settings = refresh.Settings.from_env()
            responses = [b'{"data":{"auth_data":"test-auth"}}',
                         b'{"data":{"subscribe_url":"https://sub.example.invalid/w/wm?token=test-token"}}',
                         yaml.safe_dump({"proxies": nodes}).encode(), self.MAP]
            with mock.patch.object(refresh, "_get", side_effect=responses):
                count = refresh.refresh(settings)
            written = yaml.safe_load(settings.nodes_path.read_text())["proxies"]
            self.assertEqual(written[0], {**nodes[0], "server": "203.0.113.25"})
            self.assertEqual(written[1], {**nodes[1], "server": "203.0.113.26", "sni": "client.example.invalid"})
            self.assertEqual(count, len(nodes))

    def test_address_changes_preserve_explicit_tls_names_and_input(self):
        for kind, field in [("anytls", "sni"), ("trojan", "sni"), ("hysteria", "sni"),
                            ("hysteria2", "sni"), ("tuic", "sni"), ("vmess", "servername"), ("vless", "servername")]:
            with self.subTest(kind=kind):
                node = self.node(kind, **{field: "certificate.example.invalid"})
                result = refresh.apply_server_map([node], self.MAP)[0]
                self.assertEqual(result["server"], "203.0.113.25")
                self.assertEqual(result[field], "certificate.example.invalid")
                self.assertEqual(node["server"], "wm1jp.000.com")

    def test_address_changes_preserve_implicit_tls_names(self):
        for kind, field in [("anytls", "sni"), ("trojan", "sni"), ("hysteria", "sni"),
                            ("hysteria2", "sni"), ("tuic", "sni"), ("vmess", "servername"), ("vless", "servername")]:
            with self.subTest(kind=kind):
                result = refresh.apply_server_map([self.node(kind)], self.MAP)[0]
                self.assertEqual(result[field], "wm1jp.000.com")

    def test_normal_nodes_and_already_resolved_client_nodes_remain_unchanged(self):
        nodes = [self.node(name="日本02-×1", server="regular.example.invalid"),
                 self.node(server="203.0.113.30")]
        self.assertEqual(refresh.apply_server_map(nodes, self.MAP), nodes)

    def test_missing_client_mapping_refuses_an_unresolvable_publication(self):
        with self.assertRaisesRegex(refresh.RefreshError, "no server mapping"):
            refresh.apply_server_map([self.node()], b"[]")

    def test_invalid_mapping_is_rejected_without_echoing_the_document(self):
        invalid = [b"<html>SECRET</html>", b'{"SECRET":[]}',
                   json.dumps([{"domain": "wm1jp.000.com", "ip": "SECRET"}]).encode(),
                   json.dumps([{"domain": "wm1jp.000.com", "ip": 123}]).encode(),
                   json.dumps([{"domain": "bad\nSECRET", "ip": "203.0.113.1"}]).encode(),
                   b"[null]",
                   json.dumps([{"domain": "wm1jp.000.com", "ip": "203.0.113.1"},
                               {"domain": "WM1JP.000.COM", "ip": "203.0.113.2"}]).encode()]
        for document in invalid:
            with self.subTest(document=document):
                with self.assertRaises(refresh.RefreshError) as raised:
                    refresh.apply_server_map([self.node()], document)
                self.assertNotIn("SECRET", str(raised.exception))

    def test_dns_case_and_trailing_dot_do_not_miss_a_mapping(self):
        result = refresh.apply_server_map([self.node(server="WM1JP.000.COM.")], self.MAP)
        self.assertEqual(result[0]["server"], "203.0.113.25")

    def test_unknown_mapped_protocol_or_ss_plugin_is_not_silently_rewritten(self):
        for node in [self.node("unknown"), self.node(["ss"]), self.node(plugin="v2ray-plugin"),
                     self.node("trojan", network="ws"), self.node("trojan", sni=["invalid"])]:
            with self.subTest(node=node):
                with self.assertRaises(refresh.RefreshError):
                    refresh.apply_server_map([node], self.MAP)

    def test_failed_map_download_preserves_the_published_file(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {
            "PANEL_BASE_URL": "https://panel.example.invalid", "PANEL_EMAIL": "operator@example.invalid",
            "PANEL_PASSWORD": "test-password", "NODES_PATH": str(Path(directory) / "nodes.yaml"),
            "PROXY_REGION_FILTER": "客户端",
        }, clear=True):
            settings = refresh.Settings.from_env()
            previous = b"previous working provider\n"
            settings.nodes_path.write_bytes(previous)
            with mock.patch.object(refresh, "login", return_value="test-auth"), \
                    mock.patch.object(refresh, "subscription", return_value="https://sub.example.invalid"), \
                    mock.patch.object(refresh, "fetch_nodes", return_value=yaml.safe_dump({"proxies": [self.node()]}).encode()), \
                    mock.patch.object(refresh, "_get", side_effect=refresh.RefreshError("map unavailable")):
                with self.assertRaisesRegex(refresh.RefreshError, "map unavailable"):
                    refresh.refresh(settings)
            self.assertEqual(settings.nodes_path.read_bytes(), previous)


class DialectTest(unittest.TestCase):
    def test_flag_is_pinned_even_when_the_panel_supplies_one(self):
        self.assertEqual(
            refresh._with_flag("https://wm1855.example.invalid/w/wm?token=abc&flag=mihomo", "clash.meta"),
            "https://wm1855.example.invalid/w/wm?token=abc&flag=clash.meta",
        )

    def test_flag_is_added_when_the_panel_supplies_none(self):
        self.assertEqual(
            refresh._with_flag("http://139.162.80.238:3690/w/wm?token=abc", "clash.meta"),
            "http://139.162.80.238:3690/w/wm?token=abc&flag=clash.meta",
        )


class PanelHeaderTest(unittest.TestCase):
    """The panel is behind Cloudflare and bans urllib's default signature with a 403."""

    def test_every_panel_request_identifies_itself(self):
        self.assertEqual(refresh._panel_headers({})["User-Agent"], refresh.PANEL_USER_AGENT)
        self.assertNotIn("urllib", refresh.PANEL_USER_AGENT)

    def test_the_caller_s_own_headers_survive(self):
        headers = refresh._panel_headers({"Authorization": "token"})
        self.assertEqual(headers["Authorization"], "token")
        self.assertIn("User-Agent", headers)


class RedactionTest(unittest.TestCase):
    def test_the_subscription_token_never_reaches_a_log_line(self):
        self.assertEqual(
            refresh._public("http://139.162.80.238:3690/w/wm?token=SECRET&flag=clash.meta"),
            "http://139.162.80.238:3690/w/wm",
        )

    def test_credentials_in_a_url_never_reach_a_log_line(self):
        self.assertEqual(refresh._public("https://user:pw@panel.example.invalid/api"), "https://panel.example.invalid/api")


class TransportTest(unittest.TestCase):
    """Every network fault must become a RefreshError, so a cycle retries rather than dies.

    urllib converts only what the *request* raises into a URLError; a peer that closes
    the connection while the response is read escapes as a raw http.client error, which
    once crashed the process and let Docker restart it in place of a logged retry.
    """

    FAULTS = [
        http.client.RemoteDisconnected("Remote end closed connection without response"),
        http.client.BadStatusLine("\x15\x03\x03"),
        http.client.IncompleteRead(b"half"),
        ConnectionResetError(104, "Connection reset by peer"),
        urllib.error.URLError("[SSL] certificate verify failed"),
        TimeoutError(),
    ]

    def test_a_fault_mid_response_is_named_not_raised(self):
        for fault in self.FAULTS:
            with self.subTest(fault=type(fault).__name__), \
                    mock.patch("urllib.request.urlopen", side_effect=fault):
                with self.assertRaises(refresh.RefreshError):
                    refresh._get("https://panel.example.invalid/api")

    def test_the_message_never_carries_the_subscription_token(self):
        with mock.patch("urllib.request.urlopen", side_effect=http.client.RemoteDisconnected("closed")):
            with self.assertRaises(refresh.RefreshError) as raised:
                refresh._get("http://host.invalid/w/wm?token=SECRET")
        self.assertNotIn("SECRET", str(raised.exception))


class SettingsTest(unittest.TestCase):
    CREDENTIALS = {
        "PANEL_BASE_URL": "https://panel.example.invalid/",
        "PANEL_EMAIL": "operator@example.invalid",
        "PANEL_PASSWORD": "pw",
    }

    def setUp(self):
        self.saved = dict(os.environ)
        for name in list(os.environ):
            if name.startswith(("PANEL_", "PROXY_", "REFRESH_", "RETRY_", "NODES_")):
                del os.environ[name]

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)

    def test_a_missing_credential_names_itself(self):
        os.environ.update(self.CREDENTIALS)
        del os.environ["PANEL_PASSWORD"]
        with self.assertRaisesRegex(ValueError, "PANEL_PASSWORD"):
            refresh.Settings.from_env()

    def test_defaults_cover_everything_but_the_credentials(self):
        os.environ.update(self.CREDENTIALS)
        settings = refresh.Settings.from_env()
        self.assertEqual(settings.panel_base_url, "https://panel.example.invalid")
        self.assertIsNone(settings.region_exclude)
        self.assertEqual(settings.min_nodes, 1)
        self.assertEqual(settings.interval_seconds, 1800)
        self.assertEqual(settings.nodes_path, Path("/providers/nodes.yaml"))
        self.assertEqual(settings.server_map_url, refresh.DEFAULT_SERVER_MAP_URL)
        self.assertTrue(settings.region_filter.search("台湾01-×1-客户端"))

    def test_the_server_map_url_can_follow_an_official_endpoint_change(self):
        os.environ.update(self.CREDENTIALS, PROXY_SERVER_MAP_URL="https://vendor.example.invalid/addresses.json")
        self.assertEqual(refresh.Settings.from_env().server_map_url, "https://vendor.example.invalid/addresses.json")

    def test_a_non_https_server_map_url_refuses_to_start(self):
        for value in ("http://vendor.example.invalid/map.json", "/map.json", "https:///map.json"):
            with self.subTest(value=value):
                os.environ.update(self.CREDENTIALS, PROXY_SERVER_MAP_URL=value)
                with self.assertRaisesRegex(ValueError, "PROXY_SERVER_MAP_URL"):
                    refresh.Settings.from_env()

    def test_an_unusable_interval_refuses_to_start(self):
        os.environ.update(self.CREDENTIALS, REFRESH_INTERVAL_SECONDS="0")
        with self.assertRaisesRegex(ValueError, "REFRESH_INTERVAL_SECONDS"):
            refresh.Settings.from_env()

    def test_an_unusable_region_filter_refuses_to_start(self):
        os.environ.update(self.CREDENTIALS, PROXY_REGION_FILTER="台湾(")
        with self.assertRaisesRegex(ValueError, "PROXY_REGION_FILTER"):
            refresh.Settings.from_env()


if __name__ == "__main__":
    unittest.main()
