"""Decode WMS share links into mihomo proxy records.

WMS 1.45.1 bundles URI support for SS, Trojan, AnyTLS, VLESS, VMess, Hysteria2
and TUIC in its native subscription feed. The conversion preserves TLS pins,
SS2022 identity keys and transport options. An unsupported or malformed entry
fails the entire refresh without echoing connection secrets, so a format change
cannot silently drop nodes. Clash YAML continues to be handled by refresh.select.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import re
import urllib.parse
import uuid


class SubscriptionError(ValueError):
    """A format error with a message that contains no connection secrets."""


VMESS_CIPHERS = ("auto", "aes-128-gcm", "chacha20-poly1305", "none", "zero")
VMESS_ALLOWED_KEYS = {
    "v", "ps", "add", "port", "id", "aid", "scy", "cipher",
    "net", "type", "host", "path", "serviceName",
    "tls", "sni", "peer", "alpn", "fp", "pcs", "hpkp",
    "insecure", "allowInsecure", "allow_insecure",
}
PORT_HOP_PATTERN = re.compile(r"\d{1,5}(-\d{1,5})?(,\d{1,5}(-\d{1,5})?)*")


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
    if re.search(r"[\x00-\x20\x7f]", line.partition("#")[0]):
        raise SubscriptionError("invalid URI characters")
    if line.startswith("vmess://"):
        return _parse_vmess(line[8:])
    if re.search(r"%(?![0-9A-Fa-f]{2})", line):
        raise SubscriptionError("invalid URI characters")

    url = urllib.parse.urlsplit(line)
    scheme = url.scheme
    kind = "hysteria2" if scheme == "hy2" else scheme
    if kind not in ("ss", "trojan", "anytls", "vless", "hysteria2", "tuic"):
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

    node: dict = {"name": name, "type": kind, "server": url.hostname, "port": url.port, "udp": True}
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

    allowed = {"sni", "peer", "pcs", "fp", "alpn", "allowInsecure", "allow_insecure", "insecure"}
    if kind == "anytls":
        allowed.update(("type", "hpkp"))
    elif kind == "hysteria2":
        allowed.update(("hpkp", "pinSHA256", "obfs", "obfs-password", "mport", "ports", "up", "down", "upmbps", "downmbps"))
    elif kind == "tuic":
        allowed.update((
            "hpkp", "congestion_control", "congestion-control", "congestion-controller",
            "udp_relay_mode", "udp-relay-mode", "disable_sni", "disable-sni",
        ))
    else:
        allowed.update(("type", "host", "path", "serviceName"))
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
    elif kind == "tuic":
        uuid.UUID(user)
        if url.password is None:
            raise SubscriptionError("TUIC password is required")
        password = urllib.parse.unquote(url.password, errors="strict")
        if not password:
            raise SubscriptionError("connection password is empty")
        node.update(uuid=user, password=password)
    elif kind == "hysteria2":
        password = (
            f"{user}:{urllib.parse.unquote(url.password, errors='strict')}"
            if url.password is not None else user
        )
        if not password:
            raise SubscriptionError("connection password is empty")
        node["password"] = password
    else:
        password = url.password if url.password is not None and kind == "anytls" else url.username
        if kind == "trojan" and url.password is not None:
            raise SubscriptionError("Trojan credential must be in the URI username")
        node["password"] = urllib.parse.unquote(password, errors="strict")
        if not node["password"]:
            raise SubscriptionError("connection password is empty")

    _apply_tls(
        node,
        sni_key="servername" if kind == "vless" else "sni",
        sni=_single_option(query, ("sni", "peer"), "TLS server name", fallback=True),
        insecure_values=[query[k] for k in ("allowInsecure", "allow_insecure", "insecure") if query.get(k)],
        pin_values=[query[k] for k in ("pcs", "hpkp", "pinSHA256") if query.get(k)],
        fp=query.get("fp"),
        alpn=query.get("alpn"),
    )

    if kind == "hysteria2":
        _apply_hysteria2_options(node, query)
        return node
    if kind == "tuic":
        _apply_tuic_options(node, query)
        return node

    network = query.get("type") or "tcp"
    if kind == "anytls" and network != "tcp":
        raise SubscriptionError("unsupported transport")
    _apply_transport(
        node,
        network,
        host=query.get("host"),
        path=query.get("path"),
        service_name=query.get("serviceName"),
    )
    return node


def _parse_vmess(encoded: str) -> dict:
    if not encoded:
        raise SubscriptionError("VMess payload is empty")
    try:
        raw_json = decode64(encoded)
    except (ValueError, UnicodeError) as error:
        raise SubscriptionError("invalid VMess base64 payload") from error
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as error:
        raise SubscriptionError("invalid VMess JSON payload") from error
    if not isinstance(payload, dict):
        raise SubscriptionError("VMess payload must be a JSON object")

    if any(value not in (None, "") and key not in VMESS_ALLOWED_KEYS for key, value in payload.items()):
        raise SubscriptionError("unsupported VMess option")
    if payload.get("v") not in (None, "", "2", 2):
        raise SubscriptionError("unsupported VMess version")

    name = payload.get("ps")
    server = payload.get("add")
    if not isinstance(name, str) or not name or not name.isprintable():
        raise SubscriptionError("name, host and port are required")
    if not isinstance(server, str) or not server or not server.isprintable() or re.search(r"[\x00-\x20\x7f]", server):
        raise SubscriptionError("name, host and port are required")
    if server.startswith("[") and server.endswith("]"):
        try:
            server = str(ipaddress.ip_address(server[1:-1]))
        except ValueError as error:
            raise SubscriptionError("invalid VMess host address") from error

    port = _parse_int(payload.get("port"), "port", minimum=1, maximum=65535)
    raw_id = payload.get("id")
    if not isinstance(raw_id, str) or not raw_id:
        raise SubscriptionError("connection credential is missing")
    try:
        uuid.UUID(raw_id)
    except ValueError as error:
        raise SubscriptionError("invalid VMess UUID") from error

    raw_aid = payload.get("aid")
    alter_id = 0 if raw_aid in (None, "") else _parse_int(raw_aid, "alterId", minimum=0, maximum=65535)

    cipher = _single_option(payload, ("scy", "cipher"), "VMess cipher") or "auto"
    if cipher not in VMESS_CIPHERS:
        raise SubscriptionError("unsupported VMess cipher")

    network = _optional_str(payload.get("net"), "transport") or "tcp"
    header_type = _optional_str(payload.get("type"), "header type") or "none"
    allowed_headers = ("none", "gun") if network == "grpc" else ("none",)
    if header_type not in allowed_headers:
        raise SubscriptionError("unsupported transport security or header mode")

    raw_tls = payload.get("tls")
    if raw_tls in (None, "", "none", False, 0, "0", "false"):
        tls = False
    elif raw_tls in ("tls", True, 1, "1", "true"):
        tls = True
    else:
        raise SubscriptionError("unsupported VMess TLS mode")

    node: dict = {
        "name": name,
        "type": "vmess",
        "server": server,
        "port": port,
        "udp": True,
        "uuid": raw_id,
        "alterId": alter_id,
        "cipher": cipher,
        "tls": tls,
    }
    _apply_tls(
        node,
        sni_key="servername",
        sni=_single_option(payload, ("sni", "peer"), "TLS server name", fallback=True),
        insecure_values=[payload[k] for k in ("allowInsecure", "allow_insecure", "insecure") if payload.get(k) not in (None, "")],
        pin_values=[payload[k] for k in ("pcs", "hpkp") if payload.get(k) not in (None, "")],
        fp=_optional_str(payload.get("fp"), "client fingerprint"),
        alpn=payload.get("alpn"),
    )
    host = _optional_str(payload.get("host"), "WebSocket host")
    path = _optional_str(payload.get("path"), "transport path")
    service_name = _optional_str(payload.get("serviceName"), "gRPC service name")
    if network == "grpc" and not service_name and path:
        service_name, path = path, None
    _apply_transport(node, network, host=host, path=path, service_name=service_name)
    return node


def _apply_hysteria2_options(node: dict, query: dict[str, str]) -> None:
    obfs = query.get("obfs") or ""
    obfs_password = query.get("obfs-password") or ""
    if obfs not in ("", "none", "salamander"):
        raise SubscriptionError("unsupported Hysteria2 obfuscation")
    if obfs == "salamander":
        if not obfs_password:
            raise SubscriptionError("Hysteria2 salamander requires obfs-password")
        node.update(obfs="salamander", **{"obfs-password": obfs_password})
    elif obfs_password:
        raise SubscriptionError("Hysteria2 obfs-password requires salamander obfs")

    ports = _single_option(query, ("ports", "mport"), "Hysteria2 port hop")
    if ports:
        if not PORT_HOP_PATTERN.fullmatch(ports):
            raise SubscriptionError("invalid Hysteria2 port range")
        node["ports"] = ports

    for target, keys in (("up", ("up", "upmbps")), ("down", ("down", "downmbps"))):
        rate = _single_option(query, keys, f"Hysteria2 {target} rate")
        if rate:
            if not rate.isprintable():
                raise SubscriptionError("invalid Hysteria2 bandwidth option")
            node[target] = rate


def _apply_tuic_options(node: dict, query: dict[str, str]) -> None:
    cc = _single_option(
        query,
        ("congestion_control", "congestion-control", "congestion-controller"),
        "TUIC congestion controller",
    )
    if cc:
        if cc not in ("cubic", "new_reno", "bbr"):
            raise SubscriptionError("unsupported TUIC congestion controller")
        node["congestion-controller"] = cc

    relay = _single_option(query, ("udp_relay_mode", "udp-relay-mode"), "TUIC UDP relay mode")
    if relay:
        if relay not in ("native", "quic"):
            raise SubscriptionError("unsupported TUIC UDP relay mode")
        node["udp-relay-mode"] = relay

    disable_sni = [query[k] for k in ("disable_sni", "disable-sni") if query.get(k)]
    if disable_sni and _parse_bool_flags(disable_sni, "TUIC disable-sni flag"):
        node["disable-sni"] = True


def _apply_tls(
    node: dict,
    *,
    sni_key: str,
    sni: str | None,
    insecure_values: list[object],
    pin_values: list[object],
    fp: str | None,
    alpn: object,
) -> None:
    node["skip-cert-verify"] = _parse_bool_flags(insecure_values, "TLS verification flag")
    if sni:
        if not sni.isprintable() or re.search(r"[\x00-\x20\x7f]", sni):
            raise SubscriptionError("invalid TLS server name")
        node[sni_key] = sni

    pins = set()
    for raw_pin in pin_values:
        if not isinstance(raw_pin, str):
            raise SubscriptionError("invalid or conflicting TLS certificate pins")
        normalized = raw_pin.replace(":", "").lower() if ":" in raw_pin else raw_pin.lower()
        if not re.fullmatch(r"[a-f0-9]{64}", normalized):
            raise SubscriptionError("invalid or conflicting TLS certificate pins")
        pins.add(normalized)
    if len(pins) > 1:
        raise SubscriptionError("invalid or conflicting TLS certificate pins")
    if pins:
        node["fingerprint"] = pins.pop()

    if fp is not None and (not fp.isprintable() or re.search(r"[\x00-\x20\x7f]", fp)):
        raise SubscriptionError("invalid TLS client fingerprint")
    node["client-fingerprint"] = fp or "chrome"

    if alpn not in (None, "", []):
        if isinstance(alpn, str):
            items = [part.strip() for part in alpn.split(",") if part.strip()]
        elif isinstance(alpn, list) and all(isinstance(part, str) and part.strip() for part in alpn):
            items = [part.strip() for part in alpn]
        else:
            raise SubscriptionError("invalid ALPN option")
        if not items or any(not item.isprintable() for item in items):
            raise SubscriptionError("invalid ALPN option")
        node["alpn"] = items


def _apply_transport(
    node: dict,
    network: str,
    *,
    host: str | None,
    path: str | None,
    service_name: str | None,
) -> None:
    if network not in ("tcp", "ws", "grpc"):
        raise SubscriptionError("unsupported transport")
    if network != "ws" and (host or path):
        raise SubscriptionError("WebSocket options require the WebSocket transport")
    if network != "grpc" and service_name:
        raise SubscriptionError("gRPC service name requires the gRPC transport")
    if network == "ws":
        raw_path = path or "/"
        split_path = urllib.parse.urlsplit(raw_path)
        opts: dict = {"path": raw_path}
        # Xray share links encode WS early data as an ed parameter on the path.
        params = urllib.parse.parse_qsl(split_path.query, keep_blank_values=True)
        early_data = [v for k, v in params if k == "ed"]
        if early_data:
            if len(early_data) != 1 or not early_data[0].isdigit():
                raise SubscriptionError("invalid WebSocket early-data size")
            opts["max-early-data"] = int(early_data[0])
            opts["early-data-header-name"] = "Sec-WebSocket-Protocol"
            remaining = urllib.parse.urlencode([(k, v) for k, v in params if k != "ed"])
            opts["path"] = urllib.parse.urlunsplit(split_path._replace(query=remaining))
        if host:
            if not host.isprintable() or re.search(r"[\r\n]", host):
                raise SubscriptionError("invalid WebSocket host")
            opts["headers"] = {"Host": host}
        node.update(network="ws", **{"ws-opts": opts})
    elif network == "grpc":
        if service_name and not service_name.isprintable():
            raise SubscriptionError("invalid gRPC service name")
        node.update(network="grpc", **{"grpc-opts": {"grpc-service-name": service_name or ""}})


def _parse_bool_flags(values: list[object], label: str) -> bool:
    flags: set[bool] = set()
    for value in values:
        if isinstance(value, bool):
            flags.add(value)
        elif isinstance(value, int) and value in (0, 1):
            flags.add(bool(value))
        elif isinstance(value, str) and value.lower() in ("0", "1", "false", "true"):
            flags.add(value.lower() in ("1", "true"))
        else:
            raise SubscriptionError(f"invalid {label}")
    if len(flags) > 1:
        raise SubscriptionError(f"conflicting {label}s")
    return True in flags


def _parse_int(value: object, label: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise SubscriptionError(f"invalid {label}")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        parsed = int(value)
    else:
        raise SubscriptionError(f"invalid {label}")
    if not (minimum <= parsed <= maximum):
        raise SubscriptionError(f"invalid {label}")
    return parsed


def _optional_str(value: object, label: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise SubscriptionError(f"invalid {label}")
    return value


def _single_option(
    mapping: dict[str, object],
    keys: tuple[str, ...],
    label: str,
    *,
    fallback: bool = False,
) -> str | None:
    present: list[str] = []
    for key in keys:
        raw = mapping.get(key)
        if raw in (None, ""):
            continue
        if not isinstance(raw, str):
            raise SubscriptionError(f"invalid {label}")
        present.append(raw)
    if not present:
        return None
    if fallback:
        return present[0]
    if len(set(present)) > 1:
        raise SubscriptionError(f"conflicting {label}")
    return present[0]
