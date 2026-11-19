"""Decode WMS share links into mihomo proxy records.

WMS 1.45.1 serves SS, Trojan, AnyTLS and VLESS in its native URI feed.
The conversion preserves TLS pins, SS2022 identity keys and transport options.
An unsupported entry fails the entire refresh so a format change cannot silently
remove nodes. Clash YAML continues to be handled by refresh.parse_nodes.
"""

import base64
import re
import urllib.parse
import uuid


class SubscriptionError(ValueError):
    """A format error with a message that contains no connection secrets."""


def decode64(value: str) -> str:
    raw = value.encode("ascii")
    return base64.b64decode(raw + b"=" * (-len(raw) % 4), altchars=b"-_", validate=True).decode("utf-8")


def parse_uris(document: bytes) -> list[dict]:
    try:
        text = document.decode("utf-8-sig").strip()
        if "://" not in text:
            text = decode64("".join(text.split()))
    except (ValueError, UnicodeError) as error:
        raise SubscriptionError("subscription is neither Clash YAML nor a UTF-8 URI list") from error
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise SubscriptionError("subscription URI list is empty")
    proxies = []
    for index, line in enumerate(lines, 1):
        try:
            proxies.append(parse_uri(line))
        except SubscriptionError as error:
            raise SubscriptionError(f"subscription URI {index}: {error}") from error
        except (ValueError, UnicodeError) as error:
            raise SubscriptionError(f"subscription URI {index} has invalid encoding, address or credentials") from error
    if len({node["name"] for node in proxies}) != len(proxies):
        raise SubscriptionError("subscription URI list repeats a node name")
    return proxies


def parse_uri(line: str) -> dict:
    if re.search(r"[\x00-\x20\x7f]", line.partition("#")[0]) or re.search(r"%(?![0-9A-Fa-f]{2})", line):
        raise SubscriptionError("invalid URI characters")
    url = urllib.parse.urlsplit(line)
    kind = url.scheme
    if kind not in ("ss", "trojan", "anytls", "vless"):
        raise SubscriptionError("unsupported proxy protocol")
    name = urllib.parse.unquote(url.fragment, errors="strict")
    if not name or not name.isprintable() or not url.hostname or not url.port or url.path not in ("", "/"):
        raise SubscriptionError("name, host and port are required")
    if not url.username:
        raise SubscriptionError("connection credential is missing")
    pairs = urllib.parse.parse_qsl(url.query, keep_blank_values=True, errors="strict")
    query = dict(pairs)
    if len(query) != len(pairs):
        raise SubscriptionError("repeated URI parameter")
    node = {"name": name, "type": kind, "server": url.hostname, "port": url.port, "udp": True}
    user = urllib.parse.unquote(url.username, errors="strict")
    if kind == "ss":
        if any(query.values()):
            raise SubscriptionError("unsupported Shadowsocks URI options")
        if url.password is None:
            cipher, sep, password = decode64(user).partition(":")
            if not sep:
                raise SubscriptionError("Shadowsocks userinfo needs cipher and password")
        else:
            cipher = user
            password = urllib.parse.unquote(url.password, errors="strict")
        if not cipher or not password:
            raise SubscriptionError("Shadowsocks cipher and password are required")
        node.update(cipher=cipher, password=password)
        return node

    allowed = {"sni", "peer", "pcs", "fp", "alpn", "type", "allowInsecure", "insecure"}
    if kind == "anytls":
        allowed.add("hpkp")
    else:
        allowed.update(("host", "path", "serviceName"))
    if kind == "vless":
        allowed.update(("security", "encryption", "flow", "headerType", "quicSecurity"))
    if any(value and key not in allowed for key, value in query.items()):
        raise SubscriptionError("unsupported URI option")
    for key in ("headerType", "quicSecurity"):
        if query.get(key, "none") not in ("", "none"):
            raise SubscriptionError("unsupported transport security or header mode")

    if kind == "vless":
        uuid.UUID(user)
        if url.password is not None or query.get("encryption", "none") not in ("", "none"):
            raise SubscriptionError("unsupported VLESS credential or encryption")
        node["uuid"] = user
        security = query.get("security", "none")
        if security not in ("", "none", "tls"):
            raise SubscriptionError("unsupported VLESS security mode")
        node["tls"] = security == "tls"
        if query.get("flow"):
            node["flow"] = query["flow"]
    else:
        password = url.password if url.password is not None and kind == "anytls" else url.username
        if kind == "trojan" and url.password is not None:
            raise SubscriptionError("Trojan credential must be in the URI username")
        node["password"] = urllib.parse.unquote(password, errors="strict")
        if not node["password"]:
            raise SubscriptionError("connection password is empty")

    insecure = [query[k].lower() for k in ("allowInsecure", "insecure") if query.get(k)]
    if any(v not in ("0", "1", "false", "true") for v in insecure):
        raise SubscriptionError("invalid TLS verification flag")
    flags = {v in ("1", "true") for v in insecure}
    if len(flags) > 1:
        raise SubscriptionError("conflicting TLS verification flags")
    node["skip-cert-verify"] = True in flags
    sni = query.get("sni") or query.get("peer")
    if sni:
        node["servername" if kind == "vless" else "sni"] = sni
    pins = {query[k] for k in ("pcs", "hpkp") if query.get(k)}
    if len(pins) > 1 or any(not re.fullmatch(r"[a-fA-F0-9]{64}", pin) for pin in pins):
        raise SubscriptionError("invalid or conflicting TLS certificate pins")
    if pins:
        node["fingerprint"] = pins.pop()
    node["client-fingerprint"] = query.get("fp") or "chrome"
    if query.get("alpn"):
        node["alpn"] = query["alpn"].split(",")

    network = query.get("type") or "tcp"
    if network not in ("tcp", "ws", "grpc") or (kind == "anytls" and network != "tcp"):
        raise SubscriptionError("unsupported transport")
    if network != "ws" and (query.get("host") or query.get("path")):
        raise SubscriptionError("WebSocket options require the WebSocket transport")
    if network != "grpc" and query.get("serviceName"):
        raise SubscriptionError("gRPC service name requires the gRPC transport")
    if network == "ws":
        path = urllib.parse.urlsplit(query.get("path") or "/")
        opts = {"path": query.get("path") or "/"}
        # Xray share links encode WS early data as an ed parameter on the path.
        params = urllib.parse.parse_qsl(path.query, keep_blank_values=True)
        early_data = [v for k, v in params if k == "ed"]
        if early_data:
            if len(early_data) != 1 or not early_data[0].isdigit():
                raise SubscriptionError("invalid WebSocket early-data size")
            opts["max-early-data"] = int(early_data[0])
            opts["early-data-header-name"] = "Sec-WebSocket-Protocol"
            remaining = urllib.parse.urlencode([(k, v) for k, v in params if k != "ed"])
            opts["path"] = urllib.parse.urlunsplit(path._replace(query=remaining))
        if query.get("host"):
            opts["headers"] = {"Host": query["host"]}
        node.update(network="ws", **{"ws-opts": opts})
    elif network == "grpc":
        node.update(network="grpc", **{"grpc-opts": {"grpc-service-name": query.get("serviceName", "")}})
    return node
