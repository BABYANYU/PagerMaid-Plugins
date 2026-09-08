"""PagerMaid-Pyro plugin: IP / domain information lookup.

Usage:
    ，ip 8.8.8.8
    ，ip google.com
    ，ip 2001:4860:4860::8888

Reply to a message containing an IP/domain, then send:
    ，ip
"""

import html
import ipaddress
import re
from typing import Optional

import httpx
from pyrogram import enums

from pagermaid.enums import Client, Message
from pagermaid.listener import listener


API_BASE = "http://ip-api.com/json/"
RIPESTAT_BASE = "https://stat.ripe.net/data"
REQUEST_TIMEOUT = 15.0
USER_AGENT = "PagerMaid-IP-Plugin/1.0"
SOURCE_APP = "pagermaid-ip-plugin"


IPV4_RE = re.compile(
    r"(?<![\d.])"
    r"(?:\d{1,3}\.){3}\d{1,3}"
    r"(?![\d.])"
)


DOMAIN_RE = re.compile(
    r"(?<![A-Za-z0-9_-])"
    r"(?:"
    r"[A-Za-z0-9]"
    r"(?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"\."
    r")+"
    r"[A-Za-z]{2,63}"
    r"(?![A-Za-z0-9_-])"
)


IPV6_CANDIDATE_RE = re.compile(
    r"(?<![0-9A-Fa-f:])"
    r"\[?"
    r"("
    r"[0-9A-Fa-f]{0,4}"
    r"(?::[0-9A-Fa-f]{0,4}){2,}"
    r")"
    r"\]?"
    r"(?![0-9A-Fa-f:])"
)


def valid_ip(candidate: str) -> Optional[str]:
    candidate = candidate.strip()
    candidate = candidate.strip("[](){}<>,;\"'`")

    if not candidate:
        return None

    if "%" in candidate:
        candidate = candidate.split("%", 1)[0]

    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def extract_query(text: str) -> Optional[str]:
    if not text:
        return None

    text = text.strip()

    if not text:
        return None

    # IPv4
    for match in IPV4_RE.finditer(text):
        ip = valid_ip(match.group(0))

        if ip:
            return ip

    # IPv6
    for match in IPV6_CANDIDATE_RE.finditer(text):
        candidate = match.group(1)
        ip = valid_ip(candidate)

        if ip and ":" in ip:
            return ip

    # Domain
    domain_match = DOMAIN_RE.search(text)

    if domain_match:
        domain = domain_match.group(0)
        return domain.rstrip(".").lower()

    # Fallback tokens
    tokens = re.split(r"\s+", text)

    for raw_token in tokens:
        token = raw_token.strip()
        token = token.strip("[](){}<>,;\"'`")

        if not token:
            continue

        # [IPv6]:port
        bracket_match = re.match(r"^\[([0-9A-Fa-f:]+)\](?::\d+)?$", token)

        if bracket_match:
            ip = valid_ip(bracket_match.group(1))

            if ip:
                return ip

        # IPv4:port
        host_port_match = re.match(r"^((?:\d{1,3}\.){3}\d{1,3}):\d+$", token)

        if host_port_match:
            ip = valid_ip(host_port_match.group(1))

            if ip:
                return ip

        ip = valid_ip(token)

        if ip:
            return ip

    return None


def parse_args(message: Message) -> str:
    text = (getattr(message, "text", None) or "").strip()

    match = re.match(r"^(?:,|，)ip(?:\s+([\s\S]*))?$", text, re.I)

    if not match:
        return ""

    return (match.group(1) or "").strip()


async def fetch_reply_message(bot: Client, message: Message):
    replied = getattr(message, "reply_to_message", None)

    reply_id = getattr(message, "reply_to_message_id", None)

    if reply_id:
        try:
            fetched = await bot.get_messages(
                chat_id=message.chat.id,
                message_ids=reply_id,
                replies=0,
            )

            if fetched:
                replied = fetched

        except Exception:
            pass

    return replied


async def get_reply_text(bot: Client, message: Message) -> str:
    replied = await fetch_reply_message(bot, message)

    if not replied:
        return ""

    text = getattr(replied, "text", None) or getattr(replied, "caption", None) or ""

    return text.strip()


async def fetch_ripestat(
    client: httpx.AsyncClient,
    endpoint: str,
    params: dict,
) -> dict:
    request_params = dict(params)
    request_params["sourceapp"] = SOURCE_APP

    try:
        response = await client.get(
            f"{RIPESTAT_BASE}/{endpoint}/data.json",
            params=request_params,
        )
        response.raise_for_status()
        payload = response.json()

        if payload.get("status") == "ok":
            data = payload.get("data")
            return data if isinstance(data, dict) else {}

    except (httpx.HTTPError, ValueError, TypeError):
        pass

    return {}


def classify_line(holder: str) -> str:
    upper = holder.upper()

    rules = (
        (("CHINANET", "CHINA TELECOM", "CT-"), "中国电信"),
        (("UNICOM", "CHINA169", "CNCGROUP"), "中国联通"),
        (("CMNET", "CHINAMOBILE", "CHINA MOBILE"), "中国移动"),
        (("CERNET",), "中国教育网"),
        (("CSTNET",), "中国科技网"),
        (("HWCSNET", "HUAWEI CLOUD"), "华为云"),
        (("BGP",), "BGP 多线"),
    )

    for keywords, name in rules:
        if any(keyword in upper for keyword in keywords):
            return name

    return "其他/国际线路"


async def get_routing_info(ip_address: str) -> dict:
    try:
        ip_address = str(ipaddress.ip_address(ip_address))
    except ValueError:
        return {}

    timeout = httpx.Timeout(REQUEST_TIMEOUT)
    headers = {"User-Agent": USER_AGENT}

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers=headers,
    ) as client:
        network = await fetch_ripestat(
            client,
            "network-info",
            {"resource": ip_address},
        )
        prefix = str(network.get("prefix") or "")

        if not prefix:
            return {}

        overview = await fetch_ripestat(
            client,
            "prefix-overview",
            {"resource": prefix, "min_peers_seeing": 1},
        )

        holders = {
            str(item.get("asn")): str(item.get("holder") or "未知 AS")
            for item in overview.get("asns", [])
            if item.get("asn") is not None
        }
        asns = [str(asn).upper().removeprefix("AS") for asn in network.get("asns", [])]

        for asn in holders:
            if asn not in asns:
                asns.append(asn)

    routes = []

    for asn in asns[:10]:
        holder = holders.get(asn, "未知 AS")
        routes.append(
            {
                "asn": asn,
                "holder": holder,
                "line": classify_line(holder),
            }
        )

    line_order = {
        "中国电信": 0,
        "中国联通": 1,
        "中国移动": 2,
        "中国教育网": 3,
        "中国科技网": 4,
        "华为云": 5,
        "BGP 多线": 6,
        "其他/国际线路": 7,
    }
    routes.sort(key=lambda item: (line_order.get(item["line"], 9), int(item["asn"])))

    return {
        "prefix": prefix,
        "routes": routes,
        "is_moas": len(routes) > 1,
    }


async def get_ip_info(query: str) -> dict:
    if not query:
        return {
            "status": "fail",
            "message": "请提供有效的 IP 地址或域名",
        }

    query = query.strip()

    params = {
        "lang": "zh-CN",
        "fields": ("status,message,country,regionName,city,isp,org,as,query"),
    }

    headers = {
        "User-Agent": USER_AGENT,
    }

    try:
        timeout = httpx.Timeout(REQUEST_TIMEOUT)

        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers=headers,
        ) as client:
            response = await client.get(
                API_BASE + query,
                params=params,
            )

        if response.status_code != 200:
            return {
                "status": "fail",
                "message": (f"API 请求失败，HTTP 状态码：{response.status_code}"),
            }

        try:
            data = response.json()

        except Exception:
            return {
                "status": "fail",
                "message": "API 返回了无法解析的数据",
            }

        if not isinstance(data, dict):
            return {
                "status": "fail",
                "message": "API 返回了非预期的数据格式",
            }

        if data.get("status") == "fail":
            return {
                "status": "fail",
                "message": (
                    data.get("message") or "查询失败，请检查 IP 地址或域名是否正确"
                ),
            }

        resolved_ip = str(data.get("query") or "")

        if valid_ip(resolved_ip):
            data["routing"] = await get_routing_info(resolved_ip)

        return data

    except httpx.TimeoutException:
        return {
            "status": "fail",
            "message": "请求超时，请稍后重试",
        }

    except httpx.ConnectError:
        return {
            "status": "fail",
            "message": "网络连接失败，请稍后重试",
        }

    except Exception:
        return {
            "status": "fail",
            "message": "网络请求失败，请稍后重试",
        }


def build_result(data: dict) -> str:
    country_value = str(data.get("country") or "N/A")

    if country_value in {
        "中華人民共和國",
        "中华人民共和国",
        "People's Republic of China",
    }:
        country_value = "中国"

    country = html.escape(country_value)
    region = html.escape(str(data.get("regionName") or "N/A"))
    city = html.escape(str(data.get("city") or "N/A"))
    isp = html.escape(str(data.get("isp") or "N/A"))
    org = html.escape(str(data.get("org") or "N/A"))
    ip_address = html.escape(str(data.get("query") or "N/A"))

    lines = [
        f"<b>IP：</b><code>{ip_address}</code>",
        f"位置：{country} - {region} - {city}",
        f"ISP：{isp}",
        f"组织：{org}",
    ]

    routing = data.get("routing") or {}

    if routing.get("prefix"):
        routes = routing.get("routes") or []

        lines.extend(
            [
                "",
                "<b>路由信息：</b>",
            ]
        )

        for index, route in enumerate(routes, start=1):
            asn = html.escape(str(route.get("asn") or "N/A"))
            holder = html.escape(str(route.get("holder") or "未知 AS"))
            line = html.escape(str(route.get("line") or "未知线路"))
            lines.extend(
                [
                    f"{index}. AS{asn} · {line}",
                    f"&#x20;  {holder}",
                ]
            )

    return "\n".join(lines)


async def show_help(message: Message) -> None:
    await message.edit(
        "没有找到可查询的 IP 或域名",
        parse_mode=enums.ParseMode.DISABLED,
        disable_web_page_preview=True,
    )


@listener(
    pattern=r"^(?:,|，)ip(?:\s|$)[\s\S]*",
    priority=1,
    block_process=True,
    ignore_edited=True,
)
async def ip_handler(bot: Client, message: Message):
    try:
        args = parse_args(message)
        query = None

        # Direct argument
        if args:
            query = extract_query(args)

            if not query and len(args.split()) == 1:
                query = args.strip()

        # Reply message
        if not query:
            reply_text = await get_reply_text(bot, message)

            if reply_text:
                query = extract_query(reply_text)

                if not query:
                    parts = reply_text.split()

                    if parts:
                        first_token = parts[0].strip().strip("[](){}<>,;\"'`")

                        if first_token:
                            query = first_token

        if not query:
            await show_help(message)
            return

        await message.edit(
            f"正在查询: {query}",
            parse_mode=enums.ParseMode.DISABLED,
            disable_web_page_preview=True,
        )

        data = await get_ip_info(query)

        if data.get("status") == "fail":
            error_message = data.get("message") or "未知错误"

            await message.edit(
                f"查询失败\n\n查询目标: {query}\n失败原因: {error_message}",
                parse_mode=enums.ParseMode.DISABLED,
                disable_web_page_preview=True,
            )

            return

        result = build_result(data)

        await message.edit(
            result,
            parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True,
        )

    except Exception as exc:
        error_message = str(exc) or exc.__class__.__name__

        if len(error_message) > 150:
            error_message = error_message[:150] + "..."

        try:
            await message.edit(
                f"IP查询失败\n\n错误信息: {error_message}",
                parse_mode=enums.ParseMode.DISABLED,
                disable_web_page_preview=True,
            )

        except Exception:
            pass
