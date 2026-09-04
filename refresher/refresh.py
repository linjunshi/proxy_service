#!/usr/bin/env python3
"""Keep mihomo's node list fresh from the panel account.

The subscription URL rotates its host every few hours, so the account credential is
the only durable secret: every cycle logs in, asks the panel where the list lives
today, downloads it in the pinned dialect and republishes it atomically. A cycle that
cannot do all of that leaves the previous list serving -- a dead panel costs
staleness, never an outage.

The contract with the vendor's API, and how to re-derive it, is in CONTEXT.md.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import yaml

import env

LOG = logging.getLogger("refresher")

# The subscription host answers every dialect on one URL and picks by flag and
# User-Agent. This pair is the one that yields the full Clash proxy list; the default
# must never be relied on, because an unset flag plus mihomo's own User-Agent selects
# a base64 URI list instead. See CONTEXT.md "The dialect table".
SUBSCRIPTION_FLAG = "clash.meta"
SUBSCRIPTION_USER_AGENT = "clash.meta/1.0"

# The panel sits behind Cloudflare, which answers `error code: 1010` -- a banned browser
# signature -- to urllib's default User-Agent. Any other value passes; this one is what
# the vendor's own client sends to its own API, so it is the least likely to be banned
# next. It happens to equal SUBSCRIPTION_USER_AGENT and is not the same decision: that
# one pins a dialect and may have to move, this one only has to not look like a bot.
PANEL_USER_AGENT = "clash.meta/1.0"

# Long enough for a slow rotating host, short enough that a hung fetch cannot outlive
# the retry interval and stack cycles on top of each other.
HTTP_TIMEOUT_SECONDS = 30

DEFAULT_REGION_FILTER = r"台湾|台灣|臺灣|Taiwan|TW"

# Everything mihomo needs to instantiate a proxy. A body missing them is not the
# dialect we pinned, and publishing it would break the provider rather than the node.
REQUIRED_PROXY_FIELDS = ("name", "type", "server")


class RefreshError(Exception):
    """A cycle failed for a reason the operator can act on, with the cause named."""


@dataclass(frozen=True)
class Settings:
    panel_base_url: str
    email: str
    password: str
    region_filter: re.Pattern[str]
    region_exclude: re.Pattern[str] | None
    min_nodes: int
    interval_seconds: int
    retry_interval_seconds: int
    nodes_path: Path

    @staticmethod
    def from_env() -> Settings:
        """Read the whole configuration, or refuse to start naming what is missing."""
        return Settings(
            panel_base_url=env.required("PANEL_BASE_URL").rstrip("/"),
            email=env.required("PANEL_EMAIL"),
            password=env.required("PANEL_PASSWORD"),
            region_filter=env.pattern("PROXY_REGION_FILTER", DEFAULT_REGION_FILTER),
            region_exclude=env.optional_pattern("PROXY_REGION_EXCLUDE"),
            min_nodes=env.whole_number("PROXY_MIN_NODES", 1),
            interval_seconds=env.whole_number("REFRESH_INTERVAL_SECONDS", 1800),
            retry_interval_seconds=env.whole_number("RETRY_INTERVAL_SECONDS", 300),
            nodes_path=Path(env.optional("NODES_PATH", "/providers/nodes.yaml")),
        )


def login(settings: Settings) -> str:
    """Exchange the credential for the panel's authorization token."""
    form = urllib.parse.urlencode({"email": settings.email, "password": settings.password})
    body = _request_json(
        f"{settings.panel_base_url}/api/v1/passport/auth/login",
        data=form.encode(),
        headers=_panel_headers({"Content-Type": "application/x-www-form-urlencoded"}),
    )
    return _field(body, "data", "auth_data")


def subscription(settings: Settings, auth: str) -> str:
    """Ask the panel where the node list lives right now."""
    body = _request_json(
        f"{settings.panel_base_url}/api/v1/user/getSubscribe",
        headers=_panel_headers({"Authorization": auth}),
    )
    return _field(body, "data", "subscribe_url")


def fetch_nodes(subscribe_url: str) -> bytes:
    """Download the node list, pinning the dialect by flag *and* User-Agent."""
    url = _with_flag(subscribe_url, SUBSCRIPTION_FLAG)
    LOG.info("fetching %s", _public(url))
    return _get(url, headers={"User-Agent": SUBSCRIPTION_USER_AGENT})


def select(
    document: bytes,
    region_filter: re.Pattern[str],
    region_exclude: re.Pattern[str] | None,
) -> list[dict]:
    """Parse a Clash subscription body and keep the proxies this deployment may use.

    Raises RefreshError when the body is not a Clash proxy list -- which is equally
    what a base64 node list, an HTML error page and a lapsed plan look like.
    """
    try:
        parsed = yaml.safe_load(document)
    except yaml.YAMLError as error:
        raise RefreshError(f"subscription body is not YAML: {error}") from error

    if not isinstance(parsed, dict) or not isinstance(parsed.get("proxies"), list):
        raise RefreshError(
            "subscription body carries no `proxies:` list -- wrong dialect, or an error page"
        )

    proxies = parsed["proxies"]
    for entry in proxies:
        if not isinstance(entry, dict) or any(not entry.get(field) for field in REQUIRED_PROXY_FIELDS):
            raise RefreshError(
                f"subscription body holds a proxy without {'/'.join(REQUIRED_PROXY_FIELDS)}: {entry!r}"
            )

    kept = [
        proxy
        for proxy in proxies
        if region_filter.search(str(proxy["name"]))
        and not (region_exclude and region_exclude.search(str(proxy["name"])))
    ]
    LOG.info("%d of %d proxies match the region filter", len(kept), len(proxies))
    return kept


def publish(nodes: list[dict], path: Path) -> None:
    """Replace the node list in one step: mihomo must never read a half-written file."""
    document = yaml.safe_dump(
        {"proxies": nodes}, allow_unicode=True, sort_keys=False, default_flow_style=False
    )
    staging = path.with_name(f"{path.name}.{os.getpid()}")
    try:
        staging.write_text(document, encoding="utf-8")
        os.replace(staging, path)  # atomic within one filesystem
    finally:
        staging.unlink(missing_ok=True)


def refresh(settings: Settings) -> int:
    """Run one cycle and return how many nodes it published."""
    auth = login(settings)
    nodes = select(
        fetch_nodes(subscription(settings, auth)),
        settings.region_filter,
        settings.region_exclude,
    )
    if len(nodes) < settings.min_nodes:
        raise RefreshError(
            f"{len(nodes)} node(s) match the region filter, below PROXY_MIN_NODES="
            f"{settings.min_nodes} -- refusing to publish a shorter list than the one serving"
        )
    publish(nodes, settings.nodes_path)
    return len(nodes)


def main() -> int:
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

    LOG.info(
        "panel %s, region filter /%s/, minimum %d node(s), refreshing every %ds",
        _public(settings.panel_base_url),
        settings.region_filter.pattern,
        settings.min_nodes,
        settings.interval_seconds,
    )
    while True:
        try:
            published = refresh(settings)
            delay = settings.interval_seconds
            LOG.info("published %d node(s); next cycle in %ds", published, delay)
        except RefreshError as error:
            delay = settings.retry_interval_seconds
            LOG.error("refresh failed: %s; %s; retrying in %ds", error, _standing(settings.nodes_path), delay)
        time.sleep(delay)


# ---- the panel's HTTP surface ------------------------------------------------------


def _panel_headers(extra: dict[str, str]) -> dict[str, str]:
    """Headers every panel request carries. See PANEL_USER_AGENT for why."""
    return {"User-Agent": PANEL_USER_AGENT, **extra}


def _get(url: str, *, data: bytes | None = None, headers: dict[str, str] | None = None) -> bytes:
    request = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        raise RefreshError(f"{_public(url)} answered HTTP {error.code}") from error
    except TimeoutError as error:
        raise RefreshError(f"{_public(url)} timed out after {HTTP_TIMEOUT_SECONDS}s") from error
    # URLError is an OSError, but urllib only converts what the *request* raises: a peer
    # that closes the connection while the response is being read arrives here as a raw
    # http.client error. Catching the whole family is what keeps a flaky panel a logged
    # retry instead of a crash that restarts the container and re-logs in.
    except (OSError, http.client.HTTPException) as error:
        raise RefreshError(f"{_public(url)} unreachable: {getattr(error, 'reason', error) or type(error).__name__}") from error


def _request_json(url: str, **kwargs) -> dict:
    raw = _get(url, **kwargs)
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise RefreshError(f"{_public(url)} did not answer JSON: {error}") from error
    if not isinstance(body, dict):
        raise RefreshError(f"{_public(url)} answered a JSON {type(body).__name__}, expected an object")
    return body


def _field(body: dict, *path: str) -> str:
    """Read a nested non-empty string, naming the whole path when the shape has moved."""
    name = ".".join(path)
    node: object = body
    for key in path:
        if not isinstance(node, dict) or key not in node:
            raise RefreshError(f"panel response has no `{name}` (message: {body.get('message')!r})")
        node = node[key]
    if not isinstance(node, str) or not node:
        raise RefreshError(f"panel field `{name}` is {node!r}, expected a non-empty string")
    return node


def _with_flag(subscribe_url: str, flag: str) -> str:
    parts = urllib.parse.urlsplit(subscribe_url)
    query = [pair for pair in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if pair[0] != "flag"]
    query.append(("flag", flag))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def _public(url: str) -> str:
    """A URL safe to log: the subscription token lives in the query string."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc.rpartition("@")[2], parts.path, "", ""))


# ---- staleness ---------------------------------------------------------------------


def _standing(path: Path) -> str:
    """What the tunnel is still serving, and how stale it now is."""
    try:
        age_minutes = (time.time() - path.stat().st_mtime) / 60
    except FileNotFoundError:
        return f"no node list has ever been published to {path}"
    return f"{path} still stands, last published {age_minutes:.0f} min ago"


if __name__ == "__main__":
    sys.exit(main())
