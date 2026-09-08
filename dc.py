"""PagerMaid-Pyro dc.py: test VPS latency to Telegram data centers.

Usage:
    ,dc
    ，dc

The result measures the direct TCP connection latency from the machine or
container running PagerMaid to Telegram DC1-DC5. It is not the latency of the
phone or computer on which the Telegram account is also logged in.
"""

import asyncio
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Tuple

from pyrogram import enums, raw

from pagermaid.enums import Client, Message
from pagermaid.listener import listener


TITLE = "Yanyu - Telegram 数据中心延迟"
DC_NAMES = {
    1: "Miami",
    2: "Amsterdam",
    3: "Miami",
    4: "Amsterdam",
    5: "Singapore",
}
CONNECT_TIMEOUT = 3.0
PROBE_COUNT = 3


def collect_targets(dc_options: Iterable[Any]) -> Dict[int, List[Tuple[str, int]]]:
    """Collect general-purpose IPv4 endpoints returned by Telegram."""
    targets: Dict[int, List[Tuple[str, int]]] = {
        dc_id: [] for dc_id in DC_NAMES
    }

    for option in dc_options:
        dc_id = int(getattr(option, "id", 0) or 0)
        if dc_id not in targets:
            continue
        if getattr(option, "ipv6", False):
            continue
        if getattr(option, "media_only", False):
            continue
        if getattr(option, "cdn", False):
            continue

        host = str(getattr(option, "ip_address", "") or "").strip()
        port = int(getattr(option, "port", 443) or 443)
        target = (host, port)

        if host and target not in targets[dc_id]:
            targets[dc_id].append(target)

    return targets


async def probe_once(host: str, port: int) -> Optional[float]:
    """Return one TCP connection latency in milliseconds."""
    loop = asyncio.get_running_loop()
    writer = None
    started = loop.time()

    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=CONNECT_TIMEOUT,
        )
        return (loop.time() - started) * 1000
    except (asyncio.TimeoutError, OSError):
        return None
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, RuntimeError):
                pass


async def probe_endpoint(host: str, port: int) -> Optional[int]:
    """Probe an endpoint repeatedly and return the median latency."""
    samples: List[float] = []

    for _ in range(PROBE_COUNT):
        value = await probe_once(host, port)
        if value is not None:
            samples.append(value)

    if not samples:
        return None

    return max(1, round(median(samples)))


async def probe_dc(endpoints: List[Tuple[str, int]]) -> Optional[int]:
    """Return the fastest reachable endpoint for one DC."""
    if not endpoints:
        return None

    values = await asyncio.gather(
        *(probe_endpoint(host, port) for host, port in endpoints),
        return_exceptions=True,
    )
    valid = [value for value in values if isinstance(value, int)]
    return min(valid) if valid else None


def build_result(
    results: Dict[int, Optional[int]], account_dc: Optional[int] = None
) -> str:
    account_value = f"DC{account_dc}" if account_dc in DC_NAMES else "未知"
    lines = [
        f"<b>{TITLE}</b>",
        "",
        f"账号所在 DC：<code>{account_value}</code>",
        "",
    ]

    for dc_id, location in DC_NAMES.items():
        latency = results.get(dc_id)
        value = f"{latency} ms" if latency is not None else "连接失败"
        lines.append(f"DC{dc_id} · {location}　<code>{value}</code>")

    return "\n".join(lines)


async def get_dc_options(bot: Client) -> Iterable[Any]:
    config = await bot.invoke(raw.functions.help.GetConfig())
    return getattr(config, "dc_options", [])


async def get_account_dc(bot: Client) -> Optional[int]:
    """Read the DC used by the current authenticated Pyrogram session."""
    try:
        value = bot.storage.dc_id()
        if hasattr(value, "__await__"):
            value = await value
        dc_id = int(value)
        return dc_id if dc_id in DC_NAMES else None
    except (AttributeError, TypeError, ValueError, RuntimeError):
        return None


@listener(
    pattern=r"^(?:,|，)dc(?:\s|$)",
    priority=1,
    block_process=True,
    ignore_edited=True,
)
async def dcp_handler(bot: Client, message: Message):
    try:
        await message.edit(
            "正在测试 Telegram 数据中心延迟...",
            parse_mode=enums.ParseMode.DISABLED,
        )

        account_dc, dc_options = await asyncio.gather(
            get_account_dc(bot),
            get_dc_options(bot),
        )
        targets = collect_targets(dc_options)
        values = await asyncio.gather(
            *(probe_dc(targets[dc_id]) for dc_id in DC_NAMES)
        )
        results = dict(zip(DC_NAMES, values))

        await message.edit(
            build_result(results, account_dc),
            parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        detail = str(exc) or exc.__class__.__name__
        if len(detail) > 160:
            detail = detail[:160] + "..."

        await message.edit(
            f"Telegram 数据中心延迟测试失败\n\n{detail}",
            parse_mode=enums.ParseMode.DISABLED,
        )
