#!/usr/bin/env python3
"""Move the live country on a timer, so no exit IP faces the consumer's upstream for long.

The core has no time-window primitive: `url-test` ranks on latency and `load-balance`
balances per connection, so "which country serves the next quarter hour" has to be
decided from outside. This is that outside. It reads the health the core already
measures, picks the least recently used country that still has a live node, and moves
the PROXY selector there.

It speaks to the control API from inside the core's own network namespace
(compose.yaml, `network_mode: service:proxy_service`), which is why that API can stay
bound to loopback and out of reach of everything on order_process.

The honest limit: a health check proves a node reaches Google, not that Google still
accepts it. Rotation is prophylaxis on a clock, not detection -- `make rotate` is the
answer to a block you have actually observed.
"""

from __future__ import annotations

import argparse
import http.client
import json
import logging
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

import env

LOG = logging.getLogger("rotator")

# Both are set in mihomo/config.yaml and cannot vary independently of it: the address
# is its `external-controller`, the group is what `rules:` points at. They are
# constants here rather than settings because a second place to write them would only
# be a second place for them to disagree.
CONTROLLER = "http://127.0.0.1:9090"
SELECTOR_GROUP = "PROXY"

# Outbounds the core supplies itself. A country group whose filter matched no node
# holds only REJECT, and counting that as a member would make an empty country look
# populated.
BUILTIN_OUTBOUNDS = frozenset({"DIRECT", "REJECT", "REJECT-DROP", "COMPATIBLE", "PASS"})

# Loopback inside one container; a request that takes longer than this is not slow.
HTTP_TIMEOUT_SECONDS = 10

# Short on purpose. The two reasons a cycle fails -- the core is not listening yet, no
# country has finished its first health check -- both resolve in seconds.
RETRY_SECONDS = 15


class RotationError(Exception):
    """A cycle failed for a reason the operator can act on, with the cause named."""


@dataclass(frozen=True)
class Settings:
    secret: str
    window_seconds: int
    jitter_seconds: int

    @staticmethod
    def from_env() -> Settings:
        window = env.whole_number("PROXY_ROTATION_SECONDS", 900, minimum=60)
        jitter = env.whole_number("PROXY_ROTATION_JITTER_SECONDS", 90, minimum=0)
        if jitter * 2 > window:
            raise ValueError(
                f"PROXY_ROTATION_JITTER_SECONDS={jitter} exceeds half of "
                f"PROXY_ROTATION_SECONDS={window} -- a window that can halve is not a window"
            )
        return Settings(
            secret=env.optional("MIHOMO_API_SECRET"),
            window_seconds=window,
            jitter_seconds=jitter,
        )

    def next_window(self) -> float:
        """The next window, jittered, so the switch is not on a clean quarter hour."""
        return self.window_seconds + random.uniform(-self.jitter_seconds, self.jitter_seconds)


@dataclass(frozen=True)
class Country:
    """One country group as the core currently sees it."""

    name: str
    live: int
    total: int
    node: str | None

    @property
    def is_eligible(self) -> bool:
        """Serving from a country with no live node is a quarter hour of REJECT."""
        return self.live > 0

    def __str__(self) -> str:
        return f"{self.name} {self.live}/{self.total}"


class Rotation:
    """Least recently used, over the countries that currently have a live node.

    With every country healthy this is a plain round-robin. When one is skipped for
    having nothing alive it keeps its place in the queue instead of forfeiting its
    turn -- which matters here, where several countries are a single node.

    The order is seeded randomly and kept only in memory, so a restart re-draws it and
    `make rotate` does not always jump to the same country.
    """

    def __init__(self) -> None:
        self._used: dict[str, float] = {}
        self._turn = 0.0

    def next(self, countries: list[Country], current: str | None) -> str:
        eligible = [country.name for country in countries if country.is_eligible]
        if not eligible:
            raise RotationError(
                f"no country has a live node ({_tally(countries)}) -- holding {current or 'the current selection'}"
            )
        for name in eligible:
            # Seeds land in [0, 1) and turns start at 1, so a country never yet served
            # always outranks one that has, in an order that differs per process.
            self._used.setdefault(name, random.random())
        movable = [name for name in eligible if name != current] or eligible
        chosen = min(movable, key=lambda name: self._used[name])
        self._turn += 1.0
        self._used[chosen] = self._turn
        return chosen


def survey(groups: dict, providers: dict) -> tuple[str | None, list[Country]]:
    """Read the selector group and the health of every country it can reach.

    Two bodies, because the core splits them: /proxies lists the groups and the core's
    own built-in outbounds but *not* the nodes behind a proxy-provider, whose health
    lives under /providers/proxies. Reading only the first makes every country dead.
    """
    proxies = groups.get("proxies")
    if not isinstance(proxies, dict):
        raise RotationError("the control API answered no `proxies` object")
    group = proxies.get(SELECTOR_GROUP)
    if not isinstance(group, dict) or not isinstance(group.get("all"), list):
        raise RotationError(
            f"the core has no `{SELECTOR_GROUP}` group with members -- "
            "mihomo/config.yaml and this program disagree about the selector"
        )
    nodes = _nodes(providers)
    return group.get("now"), [_country(proxies, nodes, name) for name in group["all"]]


def _nodes(providers: dict) -> dict[str, dict]:
    """Every provider member by name -- this is where the health checks are recorded."""
    return {
        proxy["name"]: proxy
        for provider in (providers.get("providers") or {}).values()
        for proxy in (provider or {}).get("proxies") or []
        if isinstance(proxy, dict) and proxy.get("name")
    }


def _country(proxies: dict, nodes: dict, name: str) -> Country:
    group = proxies.get(name) or {}
    members = [member for member in group.get("all") or [] if member not in BUILTIN_OUTBOUNDS]
    return Country(
        name=name,
        live=sum(1 for member in members if _is_live(nodes.get(member))),
        total=len(members),
        node=group.get("now"),
    )


def _is_live(node: dict | None) -> bool:
    """Live means a health check has completed and come back non-zero.

    A failure is recorded as delay 0, not as a missing entry, and the same check is
    mirrored into `extra` under the URL it was made against -- read both, so a node
    tested only under a group's own URL is not mistaken for one never tested. The
    node's own `alive` flag is deliberately not trusted: it starts optimistic, and an
    untested country is not one to hand a quarter of an hour.
    """
    if not node:
        return False
    histories = [node.get("history") or []]
    histories += [(entry or {}).get("history") or [] for entry in (node.get("extra") or {}).values()]
    return any((history[-1].get("delay") or 0) > 0 for history in histories if history)


def rotate(settings: Settings, rotation: Rotation) -> tuple[str, list[Country]]:
    """Run one cycle: survey, choose, and move the selector if the choice moved."""
    current, countries = survey(*_fetch(settings))
    chosen = rotation.next(countries, current)
    if chosen != current:
        _select(settings, chosen)
    return chosen, countries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rotate the live country group on a timer.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true", help="print the live country and every country's health, then exit")
    mode.add_argument("--pin", metavar="COUNTRY", help="select one country and exit, leaving it in place until a rotator runs again")
    arguments = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        stream=sys.stdout,
    )
    try:
        settings = Settings.from_env()
    except ValueError as error:
        LOG.critical("refusing to start: %s", error)
        return 1

    if arguments.status:
        return _report(settings)
    if arguments.pin:
        return _pin(settings, arguments.pin)

    LOG.info(
        "rotating %s over %s, one country every %ds ±%ds",
        SELECTOR_GROUP,
        CONTROLLER,
        settings.window_seconds,
        settings.jitter_seconds,
    )
    rotation = Rotation()
    while True:
        try:
            chosen, countries = rotate(settings, rotation)
            delay = settings.next_window()
            LOG.info("serving %s via %s; next switch in %.0fs; %s", chosen, _node(countries, chosen), delay, _tally(countries))
        except RotationError as error:
            delay = RETRY_SECONDS
            LOG.error("rotation held: %s; retrying in %ds", error, delay)
        time.sleep(delay)


# ---- the one-shot modes ------------------------------------------------------------


def _report(settings: Settings) -> int:
    """The two lines `make status` prints. Errors go to stderr so the target can fall back."""
    try:
        current, countries = survey(*_fetch(settings))
    except RotationError as error:
        print(error, file=sys.stderr)
        return 1
    print(f"country:   {current or 'none selected'} via {_node(countries, current)}")
    print(f"live:      {_tally(countries)}")
    return 0


def _pin(settings: Settings, name: str) -> int:
    try:
        _, countries = survey(*_fetch(settings))
        country = next((c for c in countries if c.name == name), None)
        if country is None:
            raise RotationError(f"{name} is not a member of {SELECTOR_GROUP} ({_tally(countries)})")
        if not country.is_eligible:
            raise RotationError(f"{name} has no live node -- pinning it would serve nothing")
        _select(settings, name)
    except RotationError as error:
        LOG.critical("refusing to pin: %s", error)
        return 1
    LOG.info("pinned %s via %s; rotation resumes when the rotator does", name, country.node)
    return 0


def _tally(countries: list[Country]) -> str:
    return "  ".join(str(country) for country in countries)


def _node(countries: list[Country], name: str | None) -> str:
    return next((country.node or "nothing" for country in countries if country.name == name), "nothing")


# ---- the core's control API --------------------------------------------------------


def _fetch(settings: Settings) -> tuple[dict, dict]:
    """The two bodies a survey needs: the groups, and the provider members' health."""
    return _json(settings, "/proxies"), _json(settings, "/providers/proxies")


def _json(settings: Settings, path: str) -> dict:
    raw = _call(settings, "GET", path)
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise RotationError(f"GET {path} did not answer JSON: {error}") from error
    if not isinstance(body, dict):
        raise RotationError(f"GET {path} answered a JSON {type(body).__name__}, expected an object")
    return body


def _select(settings: Settings, country: str) -> None:
    _call(
        settings,
        "PUT",
        f"/proxies/{urllib.parse.quote(SELECTOR_GROUP)}",
        body=json.dumps({"name": country}).encode(),
    )


def _call(settings: Settings, method: str, path: str, *, body: bytes | None = None) -> bytes:
    headers = {"Content-Type": "application/json"}
    if settings.secret:
        headers["Authorization"] = f"Bearer {settings.secret}"
    request = urllib.request.Request(CONTROLLER + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        raise RotationError(f"{method} {path} answered HTTP {error.code} ({error.read()[:200]!r})") from error
    except TimeoutError as error:
        raise RotationError(f"{method} {path} timed out after {HTTP_TIMEOUT_SECONDS}s") from error
    # See refresh.py's _get: urllib leaves the errors raised while reading a response
    # unwrapped, so the whole family has to be caught or a cycle crashes the process.
    except (OSError, http.client.HTTPException) as error:
        raise RotationError(
            f"{method} {path} unreachable at {CONTROLLER}: {getattr(error, 'reason', error) or type(error).__name__}"
        ) from error


if __name__ == "__main__":
    sys.exit(main())
