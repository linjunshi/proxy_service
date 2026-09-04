"""Tests for the parts of the rotator that decide which country serves next.

The HTTP edges are not tested here: everything worth getting wrong -- reading the
core's health, and choosing from it -- is a pure function over the `/proxies` body.

Run: python3 -m unittest discover -s refresher
"""

from __future__ import annotations

import http.client
import json
import os
import random
import unittest
import urllib.error
from unittest import mock

import rotate


def snapshot(selected: str | None = None, **countries: list[int | None]) -> tuple[dict, dict]:
    """The `/proxies` and `/providers/proxies` bodies the core serves, as a pair.

    Each country is given its members' last measured delay: a number for a node that
    answered, 0 for one that failed, None for one not yet tested. An empty list is a
    country whose filter matched no node, which the core renders as a lone REJECT.

    Every node is marked `alive` whatever its delay, because mihomo starts that flag
    optimistic and the rotator must not believe it.
    """
    groups: dict = {"DIRECT": {"type": "Direct"}, "REJECT": {"type": "Reject"}}
    nodes: list[dict] = []
    for country, delays in countries.items():
        members = []
        for index, delay in enumerate(delays, start=1):
            name = f"{country}{index:02d}"
            nodes.append(
                {"name": name, "type": "Trojan", "alive": True,
                 "history": [] if delay is None else [{"delay": delay}]}
            )
            members.append(name)
        groups[country] = {
            "type": "URLTest",
            "all": members or ["REJECT"],
            "now": members[0] if members else "REJECT",
        }
    groups["PROXY"] = {"type": "Selector", "all": list(countries), "now": selected}
    return {"proxies": groups}, {"providers": {"subscription": {"proxies": nodes}}}


class SurveyTest(unittest.TestCase):
    def test_counts_the_nodes_that_answered_their_health_check(self):
        _, countries = rotate.survey(*snapshot(JP=[120, 0, 240], US=[90]))
        self.assertEqual([(c.name, c.live, c.total) for c in countries], [("JP", 2, 1 + 2), ("US", 1, 1)])

    def test_a_node_never_tested_is_not_live(self):
        _, countries = rotate.survey(*snapshot(JP=[None, None]))
        self.assertEqual(countries[0].live, 0)

    def test_a_country_whose_filter_matched_nothing_holds_no_members(self):
        _, countries = rotate.survey(*snapshot(DE=[]))
        self.assertEqual((countries[0].live, countries[0].total), (0, 0))
        self.assertFalse(countries[0].is_eligible)

    def test_reports_the_country_the_core_is_serving(self):
        current, _ = rotate.survey(*snapshot(selected="US", JP=[120], US=[90]))
        self.assertEqual(current, "US")

    def test_health_recorded_only_against_a_group_url_still_counts(self):
        groups, providers = snapshot(JP=[0])
        providers["providers"]["subscription"]["proxies"][0] = {
            "name": "JP01",
            "alive": True,
            "history": [],
            "extra": {"https://www.gstatic.com/generate_204": {"history": [{"delay": 88}]}},
        }
        self.assertEqual(rotate.survey(groups, providers)[1][0].live, 1)

    def test_a_node_the_providers_do_not_report_is_not_assumed_live(self):
        groups, _ = snapshot(JP=[120, 120])
        self.assertEqual(rotate.survey(groups, {"providers": {}})[1][0].live, 0)

    def test_a_core_without_the_selector_group_is_refused(self):
        with self.assertRaisesRegex(rotate.RotationError, rotate.SELECTOR_GROUP):
            rotate.survey({"proxies": {"JP": {"type": "URLTest", "all": []}}}, {"providers": {}})

    def test_a_body_that_is_not_a_proxy_map_is_refused(self):
        with self.assertRaises(rotate.RotationError):
            rotate.survey({"message": "unauthorized"}, {"providers": {}})


class RotationTest(unittest.TestCase):
    def setUp(self):
        random.seed(20260904)
        self.rotation = rotate.Rotation()

    def countries(self, **health: list[int | None]) -> list[rotate.Country]:
        return rotate.survey(*snapshot(**health))[1]

    def serve(self, current: str | None, **health: list[int | None]) -> str:
        return self.rotation.next(self.countries(**health), current)

    def test_every_healthy_country_serves_before_any_serves_twice(self):
        healthy = {"JP": [120], "US": [90], "TW": [60], "KR": [200]}
        current, visited = None, []
        for _ in range(len(healthy)):
            current = self.rotation.next(self.countries(**healthy), current)
            visited.append(current)
        self.assertCountEqual(visited, list(healthy))

    def test_the_country_in_play_is_never_chosen_again_while_another_is_live(self):
        for _ in range(10):
            self.assertNotEqual(self.serve("JP", JP=[120], US=[90]), "JP")

    def test_a_country_with_nothing_live_is_skipped(self):
        self.assertEqual(self.serve("JP", JP=[120], DE=[0], US=[90]), "US")

    def test_a_skipped_country_keeps_its_turn_rather_than_losing_it(self):
        # DE is dead while JP and US take their turns, then recovers: it has still
        # never served, so it outranks both.
        self.serve(None, JP=[120], US=[90], DE=[0])
        self.serve("JP", JP=[120], US=[90], DE=[0])
        self.assertEqual(self.serve("US", JP=[120], US=[90], DE=[150]), "DE")

    def test_the_only_live_country_keeps_serving(self):
        self.assertEqual(self.serve("JP", JP=[120], US=[0], DE=[]), "JP")

    def test_a_core_with_nothing_live_anywhere_holds_instead_of_guessing(self):
        with self.assertRaisesRegex(rotate.RotationError, "no country has a live node"):
            self.serve("JP", JP=[0], US=[0])


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.saved = dict(os.environ)
        for name in list(os.environ):
            if name.startswith(("PROXY_ROTATION_", "MIHOMO_")):
                del os.environ[name]

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)

    def test_the_window_defaults_to_a_quarter_hour(self):
        settings = rotate.Settings.from_env()
        self.assertEqual(settings.window_seconds, 900)
        self.assertEqual(settings.secret, "")

    def test_the_window_stays_within_its_jitter(self):
        os.environ.update(PROXY_ROTATION_SECONDS="600", PROXY_ROTATION_JITTER_SECONDS="60")
        settings = rotate.Settings.from_env()
        self.assertTrue(all(540 <= settings.next_window() <= 660 for _ in range(100)))

    def test_jitter_wider_than_half_the_window_refuses_to_start(self):
        os.environ.update(PROXY_ROTATION_SECONDS="600", PROXY_ROTATION_JITTER_SECONDS="400")
        with self.assertRaisesRegex(ValueError, "PROXY_ROTATION_JITTER_SECONDS"):
            rotate.Settings.from_env()

    def test_a_window_too_short_to_be_one_refuses_to_start(self):
        os.environ["PROXY_ROTATION_SECONDS"] = "30"
        with self.assertRaisesRegex(ValueError, "PROXY_ROTATION_SECONDS"):
            rotate.Settings.from_env()


class CycleTest(unittest.TestCase):
    """One cycle writes to the core only when the country it chose is not the one serving."""

    SETTINGS = rotate.Settings(secret="", window_seconds=900, jitter_seconds=0)

    def cycle(self, state):
        with mock.patch.object(rotate, "_fetch", return_value=state), \
                mock.patch.object(rotate, "_select") as select:
            chosen, _ = rotate.rotate(self.SETTINGS, rotate.Rotation())
        return chosen, select.call_args

    def test_moving_country_selects_it_once(self):
        chosen, call = self.cycle(snapshot(selected="JP", JP=[120], US=[90]))
        self.assertEqual(chosen, "US")
        self.assertEqual(call, mock.call(self.SETTINGS, "US"))

    def test_staying_put_leaves_the_core_alone(self):
        chosen, call = self.cycle(snapshot(selected="JP", JP=[120], US=[0]))
        self.assertEqual(chosen, "JP")
        self.assertIsNone(call)


class SelectTest(unittest.TestCase):
    def test_the_selector_group_is_moved_by_name(self):
        settings = rotate.Settings(secret="", window_seconds=900, jitter_seconds=0)
        with mock.patch.object(rotate, "_call") as call:
            rotate._select(settings, "US")
        (_, method, path), keywords = call.call_args
        self.assertEqual((method, path), ("PUT", f"/proxies/{rotate.SELECTOR_GROUP}"))
        self.assertEqual(json.loads(keywords["body"]), {"name": "US"})


class TransportTest(unittest.TestCase):
    """A control API that faults must hold the selection, never crash the rotator."""

    FAULTS = [
        http.client.RemoteDisconnected("closed"),
        ConnectionRefusedError(111, "Connection refused"),
        urllib.error.URLError("unreachable"),
        TimeoutError(),
    ]

    def test_every_fault_becomes_a_held_rotation(self):
        settings = rotate.Settings(secret="", window_seconds=900, jitter_seconds=0)
        for fault in self.FAULTS:
            with self.subTest(fault=type(fault).__name__), \
                    mock.patch("urllib.request.urlopen", side_effect=fault):
                with self.assertRaises(rotate.RotationError):
                    rotate._json(settings, "/proxies")


if __name__ == "__main__":
    unittest.main()
