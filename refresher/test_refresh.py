"""Tests for the parts of the refresher that decide what reaches mihomo.

The fixtures beside this file mirror the three bodies the subscription host is known
to return (CONTEXT.md, "The dialect table"). Two of them are the ones that must never
be published: publishing either would leave the core with no usable provider.

Run: python3 -m unittest discover -s refresher
"""

from __future__ import annotations

import http.client
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
        self.assertTrue(settings.region_filter.search("台湾01-×1-客户端"))

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
