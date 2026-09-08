"""PagerMaid-Pyro: convert proxy nodes to Mihomo YAML and upload a short link."""

import base64
import html
import json
import re
from urllib.parse import parse_qs, unquote, urlsplit

import yaml
from pyrogram import enums

from pagermaid.enums import Client, Message
from pagermaid.listener import listener
from pagermaid.services import client as http_client


UPLOAD_URL = "https://fakeyou.eu.org/"
UPLOAD_EXPIRATION = "365d"
REQUEST_TIMEOUT = 30.0

TYPE_ALIASES = {
    "shadowsocks": "ss",
    "ss": "ss",
    "shadowsocksr": "ssr",
    "ssr": "ssr",
    "socks": "socks5",
    "socks5": "socks5",
    "http": "http",
    "https": "http",
    "snell": "snell",
    "vmess": "vmess",
    "vless": "vless",
    "trojan": "trojan",
    "anytls": "anytls",
    "mieru": "mieru",
    "sudoku": "sudoku",
    "hysteria": "hysteria",
    "hy": "hysteria",
    "hysteria2": "hysteria2",
    "hy2": "hysteria2",
    "tuic": "tuic",
    "shadowquic": "shadowquic",
    "wireguard": "wireguard",
    "wg": "wireguard",
    "tailscale": "tailscale",
    "ssh": "ssh",
    "masque": "masque",
    "trusttunnel": "trusttunnel",
    "zerotier": "zerotier",
    "openvpn": "openvpn",
    "dns": "dns",
    "direct": "direct",
    "reject": "reject",
    "reject-drop": "reject-drop",
    "pass": "pass",
    "compatible": "compatible",
    "rematch": "rematch",
}

URI_SCHEMES = "|".join(
    sorted((re.escape(item) for item in TYPE_ALIASES), key=len, reverse=True)
)
URI_RE = re.compile(r"(?i)(?:" + URI_SCHEMES + r")://[^\s<>]+")


class ConvertError(Exception):
    pass


def decode_base64(value):
    value = re.sub(r"\s+", "", str(value or ""))
    if not value:
        raise ValueError("empty base64")
    value += "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value.encode()).decode("utf-8-sig")
    except Exception:
        return base64.b64decode(value.encode()).decode("utf-8-sig")


def first(query, *names, default=None):
    for name in names:
        value = query.get(name)
        if value:
            return value[0]
    return default


def as_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def clean_name(value, fallback="Proxy"):
    value = unquote(str(value or "")).strip()
    return value or fallback


def query_dict(parts):
    return parse_qs(parts.query, keep_blank_values=True)


def apply_common_query(node, query, tls_servername="sni"):
    sni = first(query, "sni", "peer", "servername", "serverName")
    if sni:
        node[tls_servername] = sni

    insecure = first(query, "allowInsecure", "insecure", "skip-cert-verify")
    if insecure is not None:
        node["skip-cert-verify"] = as_bool(insecure)

    udp = first(query, "udp")
    if udp is not None:
        node["udp"] = as_bool(udp)

    fingerprint = first(query, "fp", "fingerprint", "client-fingerprint")
    if fingerprint:
        node["client-fingerprint"] = fingerprint

    alpn = first(query, "alpn")
    if alpn:
        node["alpn"] = [item for item in re.split(r"[,|]", alpn) if item]


def apply_transport(node, query):
    network = first(query, "type", "network")
    if not network or network in {"none", "tcp"}:
        return
    node["network"] = network
    host = first(query, "host")
    path = first(query, "path")
    if network == "ws":
        opts = {}
        if path:
            opts["path"] = path
        if host:
            opts["headers"] = {"Host": host}
        if opts:
            node["ws-opts"] = opts
    elif network == "grpc":
        service = first(query, "serviceName", "service-name")
        if service:
            node["grpc-opts"] = {"grpc-service-name": service}
    elif network == "http":
        opts = {}
        if path:
            opts["path"] = [path]
        if host:
            opts["headers"] = {"Host": [host]}
        if opts:
            node["http-opts"] = opts
    elif network == "h2":
        opts = {}
        if path:
            opts["path"] = path
        if host:
            opts["host"] = [host]
        if opts:
            node["h2-opts"] = opts


def parse_vmess(uri):
    raw = uri.split("://", 1)[1].split("#", 1)[0]
    data = json.loads(decode_base64(raw))
    node = {
        "name": clean_name(data.get("ps"), "VMess"),
        "type": "vmess",
        "server": data.get("add"),
        "port": as_int(data.get("port")),
        "uuid": data.get("id"),
        "alterId": as_int(data.get("aid", 0)),
        "cipher": data.get("scy") or "auto",
        "udp": True,
    }
    network = data.get("net")
    if network:
        node["network"] = network
    tls = str(data.get("tls") or "").lower()
    if tls and tls != "none":
        node["tls"] = True
    if data.get("sni"):
        node["servername"] = data["sni"]
    if data.get("fp"):
        node["client-fingerprint"] = data["fp"]
    if network == "ws":
        opts = {}
        if data.get("path"):
            opts["path"] = data["path"]
        if data.get("host"):
            opts["headers"] = {"Host": data["host"]}
        if opts:
            node["ws-opts"] = opts
    elif network == "grpc" and data.get("path"):
        node["grpc-opts"] = {"grpc-service-name": data["path"]}
    return node


def parse_ss(uri):
    raw = uri.split("://", 1)[1]
    raw, _, fragment = raw.partition("#")
    name = clean_name(fragment, "Shadowsocks")
    if "@" not in raw:
        raw = decode_base64(raw)
    credentials, address = raw.rsplit("@", 1)
    try:
        credentials = decode_base64(credentials)
    except Exception:
        credentials = unquote(credentials)
    method, password = credentials.split(":", 1)
    parts = urlsplit("ss://x@" + address)
    return {
        "name": name,
        "type": "ss",
        "server": parts.hostname,
        "port": parts.port,
        "cipher": method,
        "password": password,
        "udp": True,
    }


def parse_ssr(uri):
    decoded = decode_base64(uri.split("://", 1)[1])
    main, _, query_text = decoded.partition("/?")
    server, port, protocol, cipher, obfs, password = main.rsplit(":", 5)
    query = parse_qs(query_text)
    node = {
        "name": clean_name(
            decode_base64(first(query, "remarks")) if first(query, "remarks") else None,
            "ShadowsocksR",
        ),
        "type": "ssr",
        "server": server,
        "port": as_int(port),
        "cipher": cipher,
        "password": decode_base64(password),
        "protocol": protocol,
        "obfs": obfs,
        "udp": True,
    }
    if first(query, "protoparam"):
        node["protocol-param"] = decode_base64(first(query, "protoparam"))
    if first(query, "obfsparam"):
        node["obfs-param"] = decode_base64(first(query, "obfsparam"))
    return node


def parse_generic_uri(uri):
    scheme = uri.split(":", 1)[0].lower()
    node_type = TYPE_ALIASES.get(scheme)
    if not node_type:
        raise ConvertError("不支持的链接协议: " + scheme)
    if node_type == "vmess":
        return parse_vmess(uri)
    if node_type == "ss":
        return parse_ss(uri)
    if node_type == "ssr":
        return parse_ssr(uri)

    parts = urlsplit(uri)
    query = query_dict(parts)
    node = {
        "name": clean_name(parts.fragment, node_type.upper()),
        "type": node_type,
    }
    if parts.hostname:
        node["server"] = parts.hostname
    if parts.port:
        node["port"] = parts.port

    username = unquote(parts.username or "")
    password = unquote(parts.password or "")

    if node_type == "vless":
        node["uuid"] = username
        node["udp"] = True
        security = first(query, "security")
        if security in {"tls", "reality"}:
            node["tls"] = True
        if first(query, "encryption"):
            node["encryption"] = first(query, "encryption")
        if first(query, "flow"):
            node["flow"] = first(query, "flow")
        if security == "reality":
            node["reality-opts"] = {
                "public-key": first(query, "pbk", "public-key", default=""),
                "short-id": first(query, "sid", "short-id", default=""),
            }
        apply_common_query(node, query, "servername")
        apply_transport(node, query)
    elif node_type == "trojan":
        node["password"] = username or password
        node["udp"] = True
        node["tls"] = True
        apply_common_query(node, query, "sni")
        apply_transport(node, query)
    elif node_type == "anytls":
        node["password"] = username or password
        node["udp"] = True
        apply_common_query(node, query, "sni")
        for key in (
            "idle-session-check-interval",
            "idle-session-timeout",
            "min-idle-session",
        ):
            if first(query, key) is not None:
                node[key] = as_int(first(query, key))
    elif node_type == "hysteria2":
        node["password"] = username or password
        node["udp"] = True
        apply_common_query(node, query, "sni")
        if first(query, "obfs"):
            node["obfs"] = first(query, "obfs")
        if first(query, "obfs-password", "obfsPassword"):
            node["obfs-password"] = first(query, "obfs-password", "obfsPassword")
    elif node_type == "hysteria":
        node["auth-str"] = password or username
        for target, sources in {
            "up": ("up", "upmbps"),
            "down": ("down", "downmbps"),
            "obfs": ("obfs",),
            "protocol": ("protocol",),
        }.items():
            value = first(query, *sources)
            if value:
                node[target] = value
        apply_common_query(node, query, "sni")
    elif node_type == "tuic":
        node["uuid"] = username
        node["password"] = password
        node["udp"] = True
        apply_common_query(node, query, "sni")
        for key in ("congestion-controller", "udp-relay-mode"):
            if first(query, key):
                node[key] = first(query, key)
    elif node_type in {"http", "socks5"}:
        if username:
            node["username"] = username
        if password:
            node["password"] = password
        if node_type == "http" and scheme == "https":
            node["tls"] = True
        node["udp"] = node_type == "socks5"
    elif node_type == "snell":
        node["psk"] = password or username
        node["version"] = as_int(first(query, "version", default=3))
        node["udp"] = True
    elif node_type in {"shadowquic", "trusttunnel", "mieru", "ssh"}:
        if username:
            node["username"] = username
        if password:
            node["password"] = password
        node["udp"] = node_type != "ssh"
        apply_common_query(node, query, "sni")
    else:
        if username:
            node["username"] = username
        if password:
            node["password"] = password
        for key, values in query.items():
            if key not in {"name", "type", "server", "port"} and values:
                node[key.replace("_", "-")] = values[0]
    return node


def convert_singbox_node(item):
    node = dict(item)
    node_type = TYPE_ALIASES.get(str(node.get("type") or "").lower())
    if not node_type:
        return None
    node["type"] = node_type
    node["name"] = node.pop("tag", None) or node.get("name") or node_type.upper()
    if "server_port" in node:
        node["port"] = node.pop("server_port")
    tls = node.pop("tls", None)
    if isinstance(tls, dict) and tls.get("enabled"):
        node["tls"] = True
        if tls.get("server_name"):
            node["servername"] = tls["server_name"]
        if tls.get("insecure") is not None:
            node["skip-cert-verify"] = bool(tls["insecure"])
    transport = node.pop("transport", None)
    if isinstance(transport, dict) and transport.get("type"):
        network = transport.pop("type")
        node["network"] = network
        if network == "ws":
            opts = {}
            if transport.get("path"):
                opts["path"] = transport["path"]
            headers = transport.get("headers")
            if headers:
                opts["headers"] = headers
            if opts:
                node["ws-opts"] = opts
    return node


def normalize_node(item, index):
    if not isinstance(item, dict):
        return None
    item = dict(item)
    if "tag" in item or "server_port" in item:
        item = convert_singbox_node(item)
        if item is None:
            return None
    raw_type = str(item.get("type") or "").strip().lower()
    node_type = TYPE_ALIASES.get(raw_type)
    if not node_type:
        raise ConvertError("Mihomo 不支持节点类型: " + (raw_type or "未填写"))
    item["type"] = node_type
    item["name"] = clean_name(item.get("name"), "%s-%d" % (node_type.upper(), index))
    if "port" in item:
        item["port"] = as_int(item["port"])
    return item


def nodes_from_object(data):
    if isinstance(data, dict):
        if isinstance(data.get("proxies"), list):
            return data["proxies"]
        if isinstance(data.get("outbounds"), list):
            return [item for item in data["outbounds"] if isinstance(item, dict)]
        if data.get("type"):
            return [data]
        return []
    if isinstance(data, list):
        return data
    return []


def try_structured(text):
    try:
        data = yaml.safe_load(text)
    except Exception:
        return []
    return nodes_from_object(data)


def convert_text(text):
    text = str(text or "").strip()
    if not text:
        raise ConvertError("没有找到节点内容")

    raw_nodes = try_structured(text)
    if not raw_nodes:
        decoded = None
        compact = re.sub(r"\s+", "", text)
        if compact and re.fullmatch(r"[A-Za-z0-9_+/=-]+", compact):
            try:
                decoded = decode_base64(compact)
            except Exception:
                decoded = None
        if decoded:
            raw_nodes = try_structured(decoded)
            text = decoded

    if not raw_nodes:
        uris = URI_RE.findall(text)
        raw_nodes = [parse_generic_uri(uri.rstrip(",，。;；")) for uri in uris]

    if not raw_nodes:
        raise ConvertError("没有识别到 Mihomo 节点")

    nodes = []
    names = set()
    for index, item in enumerate(raw_nodes, 1):
        node = normalize_node(item, index)
        if node is None:
            continue
        base_name = node["name"]
        name = base_name
        suffix = 2
        while name in names:
            name = "%s %d" % (base_name, suffix)
            suffix += 1
        node["name"] = name
        names.add(name)
        nodes.append(node)
    if not nodes:
        raise ConvertError("没有可用的 Mihomo 节点")
    return nodes


def build_mihomo_yaml(nodes):
    proxy_names = [node["name"] for node in nodes]
    config = {
        "mixed-port": 7890,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "info",
        "ipv6": False,
        "proxies": nodes,
        "proxy-groups": [
            {
                "name": "PROXY",
                "type": "select",
                "proxies": proxy_names + ["DIRECT"],
            }
        ],
        "rules": ["MATCH,PROXY"],
    }
    return yaml.safe_dump(
        config,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=4096,
    )


def parse_command(message):
    text = (getattr(message, "text", None) or "").strip()
    match = re.match(r"^(?:,|，)lj(?:\s+([\s\S]*))?$", text, re.I)
    return (match.group(1) or "").strip() if match else ""


async def fetch_reply(bot, message):
    reply = getattr(message, "reply_to_message", None)
    reply_id = getattr(message, "reply_to_message_id", None)
    if reply_id:
        try:
            fetched = await bot.get_messages(
                chat_id=message.chat.id,
                message_ids=reply_id,
                replies=0,
            )
            if fetched:
                reply = fetched
        except Exception:
            pass
    return reply


async def read_reply_content(bot, message):
    reply = await fetch_reply(bot, message)
    if not reply:
        return ""
    text = getattr(reply, "text", None) or getattr(reply, "caption", None) or ""
    document = getattr(reply, "document", None)
    if document:
        try:
            downloaded = await reply.download(in_memory=True)
            raw = downloaded.getvalue() if hasattr(downloaded, "getvalue") else bytes(downloaded)
            file_text = raw.decode("utf-8-sig")
            if file_text.strip():
                return file_text
        except Exception:
            pass
    return str(text).strip()


async def upload_yaml(yaml_text):
    response = await http_client.post(
        UPLOAD_URL,
        files={
            "c": (None, yaml_text),
            "e": (None, UPLOAD_EXPIRATION),
        },
        headers={"User-Agent": "PagerMaid-Mihomo-YAML/1.0"},
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
    )
    if response.status_code not in {200, 201}:
        raise ConvertError("上传失败，HTTP %s" % response.status_code)
    try:
        data = response.json()
    except Exception as exc:
        raise ConvertError("上传接口返回了无效数据") from exc
    short_url = str(data.get("url") or "").strip() if isinstance(data, dict) else ""
    if not re.fullmatch(r"https://fakeyou\.eu\.org/[A-Za-z0-9_-]+", short_url):
        raise ConvertError("上传接口没有返回有效短链接")
    return short_url


async def edit_error(message, text):
    await message.edit(
        str(text),
        parse_mode=enums.ParseMode.DISABLED,
        disable_web_page_preview=True,
    )


@listener(
    pattern=r"^(?:,|，)lj(?:\s|$)[\s\S]*",
    priority=1,
    block_process=True,
    ignore_edited=True,
)
async def mihomo_yaml_shortlink(bot: Client, message: Message):
    try:
        source = parse_command(message)
        if not source:
            source = await read_reply_content(bot, message)
        nodes = convert_text(source)
        yaml_text = build_mihomo_yaml(nodes)
        short_url = await upload_yaml(yaml_text)
        await message.edit(
            "订阅链接: <code>%s</code>" % html.escape(short_url),
            parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except ConvertError as exc:
        await edit_error(message, exc)
    except Exception:
        await edit_error(message, "转换或上传失败")
