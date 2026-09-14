"""PagerMaid-Pyro：查询 Codex 最近两个月的额度重置记录。"""

import asyncio
import html
import json
from datetime import datetime
from typing import Any, Dict, List
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from pyrogram.enums import ParseMode

from pagermaid.enums import Client, Message
from pagermaid.listener import listener


API_URL = "https://codex-resets.com/api/v1/resets"
TIME_ZONE = ZoneInfo("Asia/Shanghai")
REQUEST_TIMEOUT = 20


def previous_month_start(value: datetime) -> datetime:
    """返回上一个自然月第一天 00:00。"""
    if value.month == 1:
        return value.replace(
            year=value.year - 1, month=12, day=1,
            hour=0, minute=0, second=0, microsecond=0,
        )
    return value.replace(
        month=value.month - 1, day=1,
        hour=0, minute=0, second=0, microsecond=0,
    )


def iso_utc(value: datetime) -> str:
    return value.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z")


def request_json(url: str) -> Dict[str, Any]:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "PagerMaid-Codex-Reset/1.0",
        },
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 429:
            raise RuntimeError("请求过于频繁，请稍后再试。") from exc
        raise RuntimeError(f"接口请求失败：HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"网络连接失败：{exc.reason}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("接口返回的数据格式不正确。") from exc

    if not isinstance(payload, dict):
        raise RuntimeError("接口返回的数据格式不正确。")
    return payload


async def fetch_resets(start: datetime, end: datetime) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    cursor = ""
    seen_ids = set()

    while True:
        params = {
            "limit": 100,
            "order": "desc",
            "from": iso_utc(start),
            "to": iso_utc(end),
        }
        if cursor:
            params["cursor"] = cursor

        payload = await asyncio.to_thread(
            request_json,
            f"{API_URL}?{urlencode(params)}",
        )
        page = payload.get("data")
        if not isinstance(page, list):
            raise RuntimeError("接口没有返回重置记录。")

        for item in page:
            if not isinstance(item, dict):
                continue
            record_id = str(item.get("id") or "")
            if record_id and record_id in seen_ids:
                continue
            if record_id:
                seen_ids.add(record_id)
            records.append(item)

        pagination = payload.get("pagination")
        if not isinstance(pagination, dict) or not pagination.get("has_more"):
            break
        cursor = str(pagination.get("next_cursor") or "")
        if not cursor:
            break

    return records


def parse_announcement_time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        ).astimezone(TIME_ZONE)
    except (TypeError, ValueError):
        return None


def format_records(records: List[Dict[str, Any]]) -> str:
    parsed = []
    for record in records:
        announced_at = parse_announcement_time(record.get("announced_at"))
        if announced_at is not None:
            parsed.append((announced_at, record))
    parsed.sort(key=lambda item: item[0], reverse=True)

    month_counts: Dict[str, int] = {}
    for announced_at, _ in parsed:
        month_key = announced_at.strftime("%Y-%m")
        month_counts[month_key] = month_counts.get(month_key, 0) + 1

    lines = [
        "<b>✦ Codex Reset History</b>",
        "",
        "<blockquote>",
    ]

    current_month = ""
    for announced_at, record in parsed:
        month_key = announced_at.strftime("%Y-%m")
        if month_key != current_month:
            if current_month:
                lines.append("")
            current_month = month_key
            lines.append(
                f"<b>{announced_at.year} 年 {announced_at.month} 月"
                f"（{month_counts[month_key]}次）</b>"
            )
        reset_type = (
            "储备" if record.get("reset_type") == "banked" else "普通"
        )
        lines.append(
            f"<code>{announced_at:%m-%d %H:%M}</code>  "
            f"{html.escape(reset_type)}"
        )

    if not parsed:
        lines.extend(["", "近两个月没有重置记录。"])
    lines.extend(["</blockquote>", "", "<b>✦ YanyuBot</b>"])
    return "\n".join(lines)


@listener(
    is_plugin=True,
    outgoing=True,
    command="cz",
    description="查询 Codex 最近两个月的额度重置记录。",
)
async def codex_reset_history(client: Client, message: Message) -> None:
    del client
    now = datetime.now(TIME_ZONE)
    start = previous_month_start(now)
    try:
        records = await fetch_resets(start, now)
        await message.edit(format_records(records), parse_mode=ParseMode.HTML)
    except Exception as exc:
        await message.edit(
            f"查询失败：{html.escape(str(exc))}",
            parse_mode=ParseMode.HTML,
        )
