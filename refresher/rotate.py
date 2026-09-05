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

It also answers `GET /current` on PROXY_CURRENT_PORT in that same namespace: the
country serving right now. A consumer that names that country in its proxy username
stays on it while the rotation moves on for everyone else. The other half of that
contract is the IN-USER rules in mihomo/config.yaml, which this program checks at start
and names any that are missing.

The honest limit: a health check proves a node reaches Google, not that Google still
accepts it. Rotation is prophylaxis on a clock, not detection -- `make rotate` is the
answer to a block you have actually observed.
"""

from __future__ import annotations

import argparse
import http.client
import http.server
import json
import logging
import random
import re
import sys
import threading
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

# /current hands out the core's own group name, never a copy of it: the name a client
# puts in its proxy username has to match the `IN-USER,<name>,<name>` rules in
# mihomo/config.yaml, which are written against the groups declared there.
CURRENT_PATH = "/current"
DEFAULT_CURRENT_PORT = 1083

# A request id comes from the consumer and goes straight into this log, so only this
# shape is echoed back -- anything else could forge a log line of its own.
REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")

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
    current_port: int

    @staticmethod
    def from_env() -> Settings:
        window = env.whole_number("PROXY_ROTATION_SECONDS", 900, minimum=60)
        jitter = env.whole_number("PROXY_ROTATION_JITTER_SECONDS", 90, minimum=0)
        if jitter * 2 > window:
            raise ValueError(
                f"PROXY_ROTATION_JITTER_SECONDS={jitter} exceeds half of "
                f"PROXY_ROTATION_SECONDS={window} -- a window that can halve is not a window"
            )
        port = env.whole_number("PROXY_CURRENT_PORT", DEFAULT_CURRENT_PORT, minimum=1)
        if port > 65535:
            raise ValueError(f"PROXY_CURRENT_PORT={port} is not a TCP port")
        return Settings(
            secret=env.optional("MIHOMO_API_SECRET"),
            window_seconds=window,
            jitter_seconds=jitter,
            current_port=port,
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


def missing_tag_rules(rules: dict, countries: list[Country]) -> list[str]:
    """The countries no `IN-USER,<name>,<name>` rule names, so a client cannot pin them.

    `IN-USER,US,JP` does not count: it would send a client that asked for US to Japan,
    and nothing on the client's side would show it.
    """
    pinned = {
        (rule.get("payload"), rule.get("proxy"))
        for rule in (rules.get("rules") or [])
        if isinstance(rule, dict) and rule.get("type") == "InUser"
    }
    return [country.name for country in countries if (country.name, country.name) not in pinned]


def current(settings: Settings) -> dict[str, str | None]:
    """The country serving right now and the node behind it, in the core's own names."""
    selected, countries = survey(*_fetch(settings))
    if selected is None:
        raise RotationError(f"{SELECTOR_GROUP} has no country selected")
    via = next((country.node for country in countries if country.name == selected), None)
    return {"country": selected, "via": via}


def request_label(query: str) -> str | None:
    """The request id the consumer sent, None if it sent none, a note if it is malformed."""
    values = [value for key, value in urllib.parse.parse_qsl(query, keep_blank_values=True) if key == "request"]
    if not values:
        return None
    return values[0] if REQUEST_ID.fullmatch(values[0]) else "a malformed request id"


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
    try:
        serve_current(settings)
    except OSError as error:
        LOG.critical("refusing to start: cannot listen on port %d for %s: %s", settings.current_port, CURRENT_PATH, error)
        return 1
    LOG.info("answering GET %s on port %d", CURRENT_PATH, settings.current_port)

    rotation = Rotation()
    verified = False
    while True:
        try:
            chosen, countries = rotate(settings, rotation)
            if not verified:
                _verify_tag_rules(settings, countries)
                verified = True
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
    print(f"pins:      {_pins(settings, countries)}")
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


def _pins(settings: Settings, countries: list[Country]) -> str:
    """The countries a proxy username reaches, and the ones the core's rules leave out."""
    try:
        missing = missing_tag_rules(_json(settings, "/rules"), countries)
    except RotationError as error:
        return f"unreadable ({error})"
    pinned = " ".join(country.name for country in countries if country.name not in missing)
    return pinned + (f"  (no IN-USER rule for {' '.join(missing)})" if missing else "")


def _verify_tag_rules(settings: Settings, countries: list[Country]) -> None:
    """Log, once at start, every country a client cannot pin: naming one of those
    rotates with everyone else, and nothing on the client's side would show it."""
    try:
        missing = missing_tag_rules(_json(settings, "/rules"), countries)
    except RotationError as error:
        LOG.error("could not read the core's rules to check the pin tags: %s", error)
        return
    if not missing:
        LOG.info("every member of %s has its IN-USER rule: %s", SELECTOR_GROUP, " ".join(c.name for c in countries))
    for name in missing:
        LOG.error(
            "no IN-USER rule pins %s: a client naming it rotates with everyone else -- add "
            "`- IN-USER,%s,%s` above MATCH in mihomo/config.yaml, then `make down` and `make up`",
            name, name, name,
        )


# ---- the current endpoint ----------------------------------------------------------


class CurrentServer(http.server.ThreadingHTTPServer):
    """Read-only and unauthenticated, which is safe because it listens in the core's
    network namespace: reachable wherever the mixed port is, and nowhere else."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, settings: Settings) -> None:
        super().__init__(("0.0.0.0", settings.current_port), CurrentHandler)
        self.settings = settings


class CurrentHandler(http.server.BaseHTTPRequestHandler):
    server: CurrentServer

    def do_GET(self) -> None:
        parts = urllib.parse.urlsplit(self.path)
        if parts.path != CURRENT_PATH:
            self._answer(404, {"error": f"only GET {CURRENT_PATH} is served here"})
            return
        try:
            answer = current(self.server.settings)
        except RotationError as error:
            self._answer(503, {"error": str(error)})
            return
        label = request_label(parts.query)
        if label is not None:
            LOG.info("current: %s -> %s via %s", label, answer["country"], answer["via"])
        self._answer(200, answer)

    def _answer(self, status: int, body: dict) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        LOG.debug("current endpoint: " + format, *args)


def serve_current(settings: Settings) -> CurrentServer:
    """Start the /current listener in a thread of its own; OSError if the port is taken."""
    server = CurrentServer(settings)
    threading.Thread(target=server.serve_forever, name="current", daemon=True).start()
    return server


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
