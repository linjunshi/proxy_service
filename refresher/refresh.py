#!/usr/bin/env python3
"""Keep mihomo's node list fresh from the panel account.

The subscription URL rotates its host every few hours, so the account credential is
the only durable secret: every cycle logs in, asks the panel where the list lives
today, downloads its native URI feed and publishes mihomo YAML atomically. A cycle that
cannot do all of that leaves the previous list serving -- a dead panel costs
staleness, never an outage.

The contract with the vendor's API, and how to re-derive it, is in CONTEXT.md.
"""

from __future__ import annotations

import base64
import http.client
import ipaddress
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
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

import env
import subscription as native_subscription

LOG = logging.getLogger("refresher")

# WMS 1.45.1's bundled customUserAgent selects its complete native URI feed.
# Adding flag=clash.meta selects a smaller YAML export, even with this user agent.
SUBSCRIPTION_USER_AGENT = "wmsxwd142"

# The panel sits behind Cloudflare, which answers `error code: 1010` -- a banned browser
# signature -- to urllib's default User-Agent. Any other value passes; this one is what
# the vendor's own client sends to its own API, so it is the least likely to be banned
# next. The native subscription uses its own client identifier above.
PANEL_USER_AGENT = "clash.meta/1.0"

# Long enough for a slow rotating host, short enough that a hung fetch cannot outlive
# the retry interval and stack cycles on top of each other.
HTTP_TIMEOUT_SECONDS = 30

DEFAULT_REGION_FILTER = r"台湾|台灣|臺灣|Taiwan|TW"

# The official app's `campusNetwork` feed supplies addresses for client-only server
# aliases that do not exist in public DNS. This is separate from the subscription;
# keep fetching it so a provider-side address change is part of an atomic refresh.
# Source: wmsxwd 1.42.5 app configuration, verified 2026-10-28 (CONTEXT.md §4).
DEFAULT_SERVER_MAP_URL = "https://wmxos.oss-cn-shenzhen.aliyuncs.com/wma/wm1xiaoyuan120.json"

# Format constants distributed in the vendor's app, not account credentials. This
# CBC envelope provides compatibility, not authenticity: the download must use HTTPS
# and the decoded data must still pass the schema checks below.
SERVER_MAP_KEY = bytes.fromhex("63724f7a42733544524f70422372794e7476495144617623716d5057614c7439")
SERVER_MAP_IV = bytes.fromhex("2139492378217831596f4d5045415445")

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
    server_map_url: str

    @staticmethod
    def from_env() -> Settings:
        """Read the whole configuration, or refuse to start naming what is missing."""
        server_map_url = env.optional("PROXY_SERVER_MAP_URL", DEFAULT_SERVER_MAP_URL)
        address = urllib.parse.urlsplit(server_map_url)
        if address.scheme != "https" or not address.hostname:
            raise ValueError("PROXY_SERVER_MAP_URL must be an absolute HTTPS URL")
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
            server_map_url=server_map_url,
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
    """Request the complete native feed with a freshly issued subscription URL."""
    url = _without_flag(subscribe_url)
    LOG.info("fetching %s", _public(url))
    return _get(url, headers={"User-Agent": SUBSCRIPTION_USER_AGENT})


def select(
    document: bytes,
    region_filter: re.Pattern[str],
    region_exclude: re.Pattern[str] | None,
) -> list[dict]:
    """Parse native WMS URIs or Clash YAML and apply the deployment's region filters.

    Invalid or unsupported entries fail the entire refresh.
    """
    try:
        parsed = yaml.safe_load(document)
    except yaml.YAMLError as error:
        raise RefreshError(f"subscription body is not YAML: {error}") from error

    if isinstance(parsed, str):
        try:
            proxies = native_subscription.parse_uris(document)
        except native_subscription.SubscriptionError as error:
            raise RefreshError(str(error)) from error
    elif isinstance(parsed, dict) and isinstance(parsed.get("proxies"), list):
        proxies = parsed["proxies"]
    else:
        raise RefreshError(
            "subscription body carries no `proxies:` list -- wrong dialect, or an error page"
        )

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
    for title, label, nodes in (
        ("FETCHED NODES", "fetched", proxies),
        ("FILTERED NODES (KEPT)", "filtered", kept),
    ):
        LOG.info("========== %s | %d node(s) ==========", title, len(nodes))
        for proxy in nodes:
            name = str(proxy["name"]).replace("\r", r"\r").replace("\n", r"\n")
            LOG.info("%s node: %s", label, name)
        LOG.info("========== END %s ==========", title)
    LOG.info("%d of %d proxies match the region filter", len(kept), len(proxies))
    return kept


def apply_server_map(nodes: list[dict], document: bytes) -> list[dict]:
    """Apply the official feed's selected `ip` for each server alias.

    Preserve the node's identity, port, credentials, and TLS name. Rewriting just the
    connection address lets the existing provider-file hot reload carry map updates,
    without restarting the core or changing its global DNS configuration. Missing
    mappings for client-only aliases fail the cycle; ordinary public hostnames and
    already-resolved server addresses remain valid without an override.
    """
    try:
        entries = json.loads(document)
    except (json.JSONDecodeError, UnicodeDecodeError):
        # The app feed is base64(AES-CBC(JSON + PKCS#7 padding)). Also accept plain
        # JSON for an explicitly configured endpoint that has removed the envelope.
        try:
            ciphertext = base64.b64decode(document.strip(), validate=True)
            plaintext = AES.new(SERVER_MAP_KEY, AES.MODE_CBC, SERVER_MAP_IV).decrypt(ciphertext)
            entries = json.loads(unpad(plaintext, AES.block_size))
        except ValueError as error:
            raise RefreshError("server address map has invalid JSON or app encryption") from error
    if not isinstance(entries, list):
        raise RefreshError("server address map must be a list")
    addresses = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or not isinstance(entry.get("domain"), str) \
                or not isinstance(entry.get("ip"), str):
            raise RefreshError(f"server address map entry {index + 1} needs domain/ip strings")
        domain = entry["domain"].rstrip(".").lower()
        if len(domain) > 253 or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in domain.split(".")
        ):
            raise RefreshError(f"server address map entry {index + 1} has an invalid domain")
        try:
            address = str(ipaddress.ip_address(entry["ip"]))
        except ValueError as error:
            raise RefreshError(f"server address map entry {index + 1} has an invalid IP address") from error
        if domain in addresses:
            raise RefreshError(f"server address map entry {index + 1} repeats a domain")
        addresses[domain] = address

    # These protocols default the TLS name to `server`. Keep that default's original
    # value when substituting an IP, including when the subscription spells it as "".
    tls_names = {"anytls": "sni", "trojan": "sni", "hysteria": "sni", "hysteria2": "sni",
                 "tuic": "sni", "vmess": "servername", "vless": "servername"}
    resolved = []
    for node in nodes:
        server = node["server"]
        name = str(node["name"]).replace("\r", r"\r").replace("\n", r"\n")
        if not isinstance(server, str):
            raise RefreshError(f"node {name!r} has a non-string server")
        address = addresses.get(server.rstrip(".").lower())
        if address is None:
            if "客户端" in str(node["name"]):
                try:
                    ipaddress.ip_address(server)
                except ValueError as error:
                    raise RefreshError(f"client node {name!r} has no server mapping -- refusing to publish") from error
            resolved.append(node)
            continue
        kind = node["type"]
        if not isinstance(kind, str) or (kind not in tls_names and kind != "ss") or (kind == "ss" and node.get("plugin")) \
                or node.get("network", "tcp") not in ("tcp", ""):
            raise RefreshError(f"cannot preserve connection settings when mapping node {name!r}: unsupported protocol/plugin/transport")
        mapped = {**node, "server": address}
        if kind in tls_names:
            field = tls_names[kind]
            if mapped.get(field) is not None and not isinstance(mapped[field], str):
                raise RefreshError(f"node {name!r} has an invalid TLS server name")
            if not mapped.get(field):
                mapped[field] = server
        resolved.append(mapped)
        LOG.info("server address: %s | %s -> %s", name, server, address)
    return resolved


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
    LOG.info("fetching server address map %s", _public(settings.server_map_url))
    nodes = apply_server_map(nodes, _get(settings.server_map_url, headers={"User-Agent": SUBSCRIPTION_USER_AGENT}))
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


def _without_flag(subscribe_url: str) -> str:
    parts = urllib.parse.urlsplit(subscribe_url)
    # Preserve encoded account tokens and signatures byte for byte.
    query = [part for part in parts.query.split("&")
             if urllib.parse.unquote_plus(part.partition("=")[0]) != "flag"]
    return urllib.parse.urlunsplit(parts._replace(query="&".join(query)))


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
