# -*- coding: utf-8 -*-
"""PagerMaid-Pyro 随机视频插件。

数据线路取自 https://shuaya.pages.dev/ 。视频优先由 Telegram 直接抓取；
必要时仅在内存中转，不在 VPS 文件系统保存视频。
"""

import asyncio
import contextlib
import html
import io
import random
import time
from typing import Any, Dict, List
from urllib.parse import urlsplit

import httpx
from pagermaid.enums import Client, Message
from pagermaid.listener import listener
from pyrogram import enums


TITLE = "小姐姐 · 随机视频"
REQUEST_TIMEOUT = 45
MAX_VIDEO_BYTES = 32 * 1024 * 1024
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "Chrome/128.0 Safari/537.36"
)

# 网站当前展示的 23 条线路，重复接口也按不同线路完整保留。
VIDEO_SOURCES: List[Dict[str, str]] = [
    {"code": "003", "name": "高质量纯妹", "type": "link", "url": "https://api.yujn.cn/api/zzxjj.php?type=video"},
    {"code": "001", "name": "黑丝", "type": "link", "url": "https://api.yujn.cn/api/heisis.php?type=video"},
    {"code": "002", "name": "萌妹短视频", "type": "link", "url": "https://api.yujn.cn/api/nvda.php?type=video"},
    {"code": "005", "name": "清纯萝莉", "type": "random", "url": "https://api.yujn.cn/api/zzxjj.php?temps="},
    {"code": "007", "name": "快手扭一扭", "type": "random", "url": "https://v.nrzj.vip/video.php?_t="},
    {"code": "009", "name": "妹纸", "type": "link", "url": "https://v.nrzj.vip/video.php"},
    {"code": "010", "name": "清纯", "type": "link", "url": "https://api.yujn.cn/api/zzxjj.php?type=video"},
    {"code": "011", "name": "甜妹", "type": "link", "url": "https://api.yujn.cn/api/xjj.php?type=video"},
    {"code": "018", "name": "快手女大学生", "type": "link", "url": "https://api.yujn.cn/api/nvda.php?type=video"},
    {"code": "019", "name": "黑丝系列", "type": "link", "url": "https://api.yujn.cn/api/heisis.php?type=video"},
    {"code": "020", "name": "慢摇系列", "type": "link", "url": "https://api.yujn.cn/api/manyao.php?type=video"},
    {"code": "021", "name": "吊带系列", "type": "link", "url": "https://api.yujn.cn/api/diaodai.php?type=video"},
    {"code": "022", "name": "清纯系列", "type": "link", "url": "https://api.yujn.cn/api/qingchun.php?type=video"},
    {"code": "023", "name": "女高系列", "type": "link", "url": "https://api.yujn.cn/api/nvgao.php?type=video"},
    {"code": "024", "name": "快手变装系列", "type": "link", "url": "https://api.yujn.cn/api/ksbianzhuang.php?type=video"},
    {"code": "025", "name": "甜妹系列", "type": "link", "url": "https://api.yujn.cn/api/tianmei.php?type=video"},
    {"code": "026", "name": "欲梦视频", "type": "link", "url": "https://api.yujn.cn/api/ndym.php?type=video"},
    {"code": "027", "name": "热舞视频", "type": "link", "url": "https://api.yujn.cn/api/rewu.php?type=video"},
    {"code": "028", "name": "小姐姐视频", "type": "link", "url": "https://api.yujn.cn/api/ksxjjsp.php"},
    {"code": "031", "name": "小姐姐", "type": "link", "url": "https://api.cenguigui.cn/api/mp4/MP4_xiaojiejie.php"},
    {"code": "032", "name": "高质量", "type": "link", "url": "https://api.lolimi.cn/API/xjj/xjj.php"},
    {"code": "033", "name": "随机小姐姐", "type": "data", "url": "https://v2.xxapi.cn/api/meinv"},
    {"code": "034", "name": "小姐姐", "type": "link", "url": "https://api.dwo.cc/api/ksvideo"},
]


def cache_busted(url: str) -> str:
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}_t={time.time_ns()}-{random.randrange(1000000)}"


def valid_remote_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


async def safe_edit(message: Message, text: str) -> None:
    try:
        await message.edit(
            text,
            parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        if "MESSAGE_NOT_MODIFIED" not in str(exc).upper():
            raise


def chat_id_of(message: Message):
    chat = getattr(message, "chat", None)
    return getattr(chat, "id", None) or getattr(message, "chat_id", None)


async def send_remote_video(client: Client, chat_id, url: str) -> None:
    await client.send_video(
        chat_id=chat_id,
        video=url,
        caption=f"<b>{TITLE}</b>",
        parse_mode=enums.ParseMode.HTML,
        supports_streaming=True,
    )


async def download_to_memory(url: str) -> io.BytesIO:
    buffer = io.BytesIO()
    buffer.name = "video.mp4"
    timeout = httpx.Timeout(REQUEST_TIMEOUT, connect=10)
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Referer": "https://shuaya.pages.dev/"},
    ) as session:
        async with session.stream("GET", url) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > MAX_VIDEO_BYTES:
                raise RuntimeError("视频超过内存上传限制")
            if content_type.startswith(("text/html", "application/json")):
                raise RuntimeError("线路没有返回视频文件")
            size = 0
            async for chunk in response.aiter_bytes(256 * 1024):
                size += len(chunk)
                if size > MAX_VIDEO_BYTES:
                    raise RuntimeError("视频超过内存上传限制")
                buffer.write(chunk)
    if buffer.tell() < 1024:
        raise RuntimeError("线路返回的视频内容为空")
    buffer.seek(0)
    return buffer


async def send_memory_video(client: Client, chat_id, url: str) -> None:
    buffer = await download_to_memory(url)
    try:
        await client.send_video(
            chat_id=chat_id,
            video=buffer,
            caption=f"<b>{TITLE}</b>",
            parse_mode=enums.ParseMode.HTML,
            supports_streaming=True,
        )
    finally:
        buffer.close()


def nested_value(data: Any, path: str):
    value = data
    for key in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


async def resolve_source(source: Dict[str, str]) -> str:
    url = source["url"]
    if source["type"] == "random":
        return url + str(random.random())
    if source["type"] != "data":
        return cache_busted(url)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=10),
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Referer": "https://shuaya.pages.dev/"},
    ) as session:
        response = await session.get(cache_busted(url))
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("JSON 线路返回格式异常") from exc
    resolved = nested_value(payload, "data")
    if not isinstance(resolved, str) or not valid_remote_url(resolved.strip()):
        raise RuntimeError("JSON 线路没有返回视频地址")
    return resolved.strip()


async def try_source(client: Client, chat_id, source: Dict[str, str]) -> None:
    url = await resolve_source(source)
    if not valid_remote_url(url):
        raise RuntimeError("视频线路地址无效")
    try:
        await send_remote_video(client, chat_id, url)
    except asyncio.CancelledError:
        raise
    except Exception:
        # Telegram 无法从第三方线路抓取时，只使用内存中转，不创建临时文件。
        await send_memory_video(client, chat_id, url)


@listener(
    is_plugin=True,
    command="xjj",
    outgoing=True,
    description="随机获取小姐姐视频",
    parameters="",
    priority=1,
)
async def random_video(client: Client, message: Message):
    chat_id = chat_id_of(message)
    if chat_id is None:
        return await safe_edit(message, "无法取得当前聊天 ID")

    await safe_edit(message, "正在抓取小姐姐...")
    errors: List[str] = []
    sources = random.sample(VIDEO_SOURCES, len(VIDEO_SOURCES))
    for source in sources:
        try:
            await try_source(client, chat_id, source)
            with contextlib.suppress(Exception):
                await message.delete()
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            errors.append(type(exc).__name__)

    detail = "、".join(dict.fromkeys(errors)) or "线路不可用"
    await safe_edit(
        message,
        "<b>随机视频获取失败</b>\n\n"
        "<blockquote>第三方视频线路暂时不可用，请稍后重试。\n\n"
        f"错误类型：{html.escape(detail)}</blockquote>",
    )
