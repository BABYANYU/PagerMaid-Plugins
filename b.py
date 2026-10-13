# -*- coding: utf-8 -*-
"""PagerMaid-Pyro 群聊记录备份插件。

指令：
    ,b                         导出当前群记录到 Telegram 收藏夹
    ,b help                    查看设置菜单
    ,b start YYYY-MM-DD HH:MM  设置开始时间（北京时间）
    ,b end YYYY-MM-DD HH:MM    设置结束时间（北京时间）
    ,b end now                 结束时间恢复为实时
    ,b today                   恢复今日 00:00 至实时
    ,b type text               仅保存文字记录
    ,b type zip                保存文字、链接和普通文件

ZIP 模式不下载图片、视频、动图、贴纸、音频、语音和压缩包附件。
时间范围和保存类型为全局设置，所有群共用；每次仅导出执行指令的群。
"""

import asyncio
import contextlib
import html
import json
import logging
import os
import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from pagermaid.enums import Client, Message
from pagermaid.listener import listener
from pagermaid.static import working_dir
from pyrogram import raw, utils
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait


BEIJING = ZoneInfo("Asia/Shanghai")
CONFIG_PATH = Path(working_dir) / "data" / "b.json"
LOGGER = logging.getLogger(__name__)
VERSION = "1.1.1"

ARCHIVE_SUFFIXES = {
    ".7z", ".ace", ".arj", ".bz", ".bz2", ".cab", ".gz", ".iso",
    ".lz", ".lz4", ".lzma", ".rar", ".tar", ".tbz", ".tbz2", ".tgz",
    ".txz", ".xz", ".z", ".zip", ".zipx", ".zst",
}
ARCHIVE_MIME_TYPES = {
    "application/gzip",
    "application/java-archive",
    "application/vnd.rar",
    "application/x-7z-compressed",
    "application/x-bzip",
    "application/x-bzip2",
    "application/x-compress",
    "application/x-gzip",
    "application/x-rar-compressed",
    "application/x-tar",
    "application/x-xz",
    "application/zip",
}
MEDIA_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".tif",
    ".tiff", ".heic", ".heif", ".avif", ".ico", ".tgs", ".webm",
    ".mp4", ".mkv", ".mov", ".avi", ".m4v", ".flv", ".wmv", ".3gp",
    ".mp3", ".wav", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".flac",
    ".wma", ".aiff", ".amr", ".mpeg", ".mpg", ".m2ts", ".mts",
}
URL_PATTERN = re.compile(r"(?i)\b(?:https?://|tg://|www\.)[^\s<>\[\]{}\"']+")
INVALID_FILE_CHARS = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")

_export_locks: Dict[int, asyncio.Lock] = {}


def _arguments(message: Message) -> str:
    parameter = getattr(message, "parameter", None)
    if parameter:
        return " ".join(str(value) for value in parameter).strip()
    text = (getattr(message, "text", None) or "").strip()
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) == 2 else ""


def _chat_id(message: Message) -> Optional[int]:
    chat = getattr(message, "chat", None)
    value = getattr(chat, "id", None) or getattr(message, "chat_id", None)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _load_all_settings() -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads(CONFIG_PATH.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def _save_all_settings(data: Dict[str, Dict[str, Any]]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(CONFIG_PATH)
    if os.name == "posix":
        with contextlib.suppress(OSError):
            CONFIG_PATH.chmod(0o600)


def _settings(chat_id: int) -> Dict[str, Any]:
    data = _load_all_settings()
    saved = data.get("global")
    migrate = not isinstance(saved, dict)
    if migrate:
        # 首次使用新版时继承当前群的旧设置，此后所有群只读取 global。
        # 保留旧键作为迁移备份，不再参与设置读取。
        saved = data.get(str(chat_id), {})
    if not isinstance(saved, dict):
        saved = {}
    mode = saved.get("type")
    settings = {
        "start": saved.get("start") if isinstance(saved.get("start"), str) else None,
        "end": saved.get("end") if isinstance(saved.get("end"), str) else None,
        "type": mode if mode in {"text", "zip"} else "text",
    }
    if migrate:
        data["global"] = settings
        _save_all_settings(data)
    return settings


def _update_settings(chat_id: int, **values: Any) -> Dict[str, Any]:
    current = _settings(chat_id)
    data = _load_all_settings()
    current.update(values)
    data["global"] = current
    _save_all_settings(data)
    return current


def _parse_local_time(value: str) -> datetime:
    value = value.strip()
    formats = ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M")
    for date_format in formats:
        try:
            return datetime.strptime(value, date_format).replace(tzinfo=BEIJING)
        except ValueError:
            continue
    raise ValueError("时间格式应为 YYYY-MM-DD HH:MM")


def _time_range(settings: Dict[str, Any]) -> Tuple[datetime, datetime]:
    now = datetime.now(BEIJING).replace(microsecond=0)
    start_value = settings.get("start")
    end_value = settings.get("end")
    start = (
        _parse_local_time(start_value)
        if start_value
        else now.replace(hour=0, minute=0, second=0)
    )
    end = _parse_local_time(end_value) if end_value else now
    if start > end:
        raise ValueError("开始时间不能晚于结束时间")
    return start, end


def _format_range(settings: Dict[str, Any]) -> str:
    now = datetime.now(BEIJING)
    if settings.get("start"):
        start_text = _parse_local_time(settings["start"]).strftime("%Y-%m-%d %H:%M")
    else:
        start_text = now.strftime("%Y-%m-%d 00:00")
    if settings.get("end"):
        end_text = _parse_local_time(settings["end"]).strftime("%Y-%m-%d %H:%M")
    else:
        end_text = "实时"
    return f"{start_text} 至 {end_text}"


def _help_text(chat_id: int) -> str:
    settings = _settings(chat_id)
    mode_name = "文字" if settings["type"] == "text" else "文字 · 链接 · 文件"
    return (
        "<b>✦ 群聊记录备份</b>\n\n"
        "<blockquote>"
        f"<b>当前范围：</b>{html.escape(_format_range(settings))}\n"
        f"<b>当前内容：</b>{mode_name}"
        "</blockquote>\n\n"
        "<b>执行备份</b>\n\n"
        "<code>,b</code>\n"
        "保存当前群聊记录到收藏夹\n\n"
        "<b>时间范围</b>\n\n"
        "<code>,b start 2026-09-22 08:30</code>\n"
        "设置开始时间\n\n"
        "<code>,b end 2026-09-22 18:00</code>\n"
        "设置结束时间\n\n"
        "<code>,b end now</code>\n"
        "结束时间恢复为实时\n\n"
        "<code>,b today</code>\n"
        "恢复今日 00:00 至实时\n\n"
        "<b>保存类型</b>\n\n"
        "<code>,b type text</code>\n"
        "保存文字记录\n\n"
        "<code>,b type zip</code>\n"
        "保存文字 链接 文件"
    )


def _message_datetime(message: Message) -> Optional[datetime]:
    value = getattr(message, "date", None)
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        raise ValueError(f"消息时间缺少时区，无法准确导出；请使用完整的 {VERSION} 版插件。")
    return value.astimezone(BEIJING)


def _sender_name(message: Message) -> str:
    user = getattr(message, "from_user", None)
    if user is not None:
        name = " ".join(
            str(value)
            for value in (
                getattr(user, "first_name", None),
                getattr(user, "last_name", None),
            )
            if value
        ).strip()
        username = getattr(user, "username", None)
        if name and username:
            return f"{name} (@{username})"
        return name or (f"@{username}" if username else str(getattr(user, "id", "未知用户")))

    sender_chat = getattr(message, "sender_chat", None)
    if sender_chat is not None:
        return str(
            getattr(sender_chat, "title", None)
            or getattr(sender_chat, "username", None)
            or "频道"
        )
    return "未知用户"


def _message_text(message: Message) -> str:
    return str(
        getattr(message, "text", None)
        or getattr(message, "caption", None)
        or ""
    ).strip()


def _extract_urls(text: str) -> List[str]:
    result: List[str] = []
    seen = set()
    for match in URL_PATTERN.findall(text or ""):
        url = match.rstrip(".,;:!?)]}。，；：！？）】》”’")
        if url and url not in seen:
            seen.add(url)
            result.append(url)
    return result


def _message_urls(message: Message) -> List[str]:
    urls = _extract_urls(_message_text(message))
    seen = set(urls)
    for field in ("entities", "caption_entities"):
        for entity in getattr(message, field, None) or []:
            url = str(getattr(entity, "url", None) or "").strip()
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def _is_allowed_document(message: Message) -> bool:
    # Telegram 的图片、视频等可能以独立媒体字段出现。
    excluded_fields = (
        "photo", "video", "video_note", "animation", "sticker", "audio", "voice"
    )
    if any(getattr(message, field, None) is not None for field in excluded_fields):
        return False

    document = getattr(message, "document", None)
    if document is None:
        return False

    file_name = str(getattr(document, "file_name", None) or "")
    suffix = Path(file_name).suffix.lower()
    mime_type = str(getattr(document, "mime_type", None) or "").lower()
    if suffix in ARCHIVE_SUFFIXES or suffix in MEDIA_SUFFIXES or mime_type in ARCHIVE_MIME_TYPES:
        return False
    if mime_type.startswith(("image/", "video/", "audio/")):
        return False
    return True


def _safe_name(value: str, fallback: str = "file") -> str:
    name = INVALID_FILE_CHARS.sub("_", value).strip(" .")
    return name[:180] or fallback


def _chat_title(message: Message) -> str:
    chat = getattr(message, "chat", None)
    return str(
        getattr(chat, "title", None)
        or getattr(chat, "first_name", None)
        or getattr(chat, "username", None)
        or _chat_id(message)
        or "chat"
    )


async def _collect_messages(
    client: Client,
    chat_id: int,
    start: datetime,
    end: datetime,
    command_id: Optional[int] = None,
) -> List[Message]:
    # 直接读取 Telegram 原始 Unix 时间戳，避免不同 Pyrogram 分支将
    # date 解析为 UTC / 宿主机本地 naive datetime 后产生八小时时差。
    peer = await _retry_flood(lambda: client.resolve_peer(chat_id))
    start_ts, end_ts = int(start.timestamp()), int(end.timestamp())
    records: Dict[int, Message] = {}
    cursor = 0
    page_number = 0
    scanned = 0
    while True:
        result = await _retry_flood(lambda: client.invoke(
            raw.functions.messages.GetHistory(
                peer=peer, offset_id=cursor, offset_date=0, add_offset=0,
                limit=100, max_id=0, min_id=0, hash=0,
            )
        ))
        batch = getattr(result, "messages", None)
        if batch is None:
            raise RuntimeError("Telegram 未返回可识别的历史记录，导出已停止。")
        if not batch:
            break
        ids = [item.id for item in batch if getattr(item, "id", 0) > 0]
        if not ids:
            raise RuntimeError("历史分页缺少消息 ID，无法确认记录完整。")
        next_cursor = min(ids)
        if cursor and next_cursor >= cursor:
            raise RuntimeError("历史分页没有向前推进，已停止导出，避免保存不完整记录。")

        selected = {}
        dated = []
        for item in batch:
            timestamp = getattr(item, "date", None)
            if timestamp is None:  # MessageEmpty / 已删除消息
                continue
            if not isinstance(timestamp, (int, float)):
                raise RuntimeError("原始消息时间不是 Unix 时间戳，无法准确判断时间范围。")
            dated.append(timestamp)
            if start_ts <= timestamp <= end_ts and item.id != command_id:
                selected[item.id] = timestamp

        if selected:
            parsed = await _retry_flood(lambda: utils.parse_messages(client, result, replies=0))
            found = set()
            for item in parsed:
                item_id = getattr(item, "id", None)
                if item_id not in selected:
                    continue
                if getattr(item, "empty", False):
                    raise RuntimeError(f"消息 {item_id} 解析为空，无法确认导出完整。")
                # 同一原始时间同时用于筛选、TXT 和 ZIP 链接记录。
                item.date = datetime.fromtimestamp(selected[item_id], timezone.utc)
                records[item_id] = item
                found.add(item_id)
            if found != set(selected):
                raise RuntimeError("部分历史消息未能解析，已停止导出，避免静默漏存。")

        scanned += len(batch)
        page_number += 1
        LOGGER.info("b %s chat=%s page=%s scanned=%s matched=%s cursor=%s",
                    VERSION, chat_id, page_number, scanned, len(records), next_cursor)
        cursor = next_cursor
        # 短页不代表历史结束；整页已早于起点或下一页为空才停止。
        if dated and max(dated) < start_ts:
            break
    return sorted(records.values(), key=lambda item: (item.date, item.id))


async def _retry_flood(operation):
    while True:
        try:
            return await operation()
        except FloodWait as exc:
            await asyncio.sleep(max(1, int(getattr(exc, "value", 1) or 1)))


def _transcript(messages: List[Message], chat_title: str, start: datetime, end: datetime) -> str:
    lines = [
        f"群聊：{chat_title}",
        f"时间：{start.strftime('%Y-%m-%d %H:%M')} 至 {end.strftime('%Y-%m-%d %H:%M')}",
        f"消息：{len(messages)} 条",
        "",
    ]
    visible_count = 0
    for item in messages:
        text = _message_text(item)
        hidden_urls = [url for url in _message_urls(item) if url not in text]
        if hidden_urls:
            text += "\n" + "\n".join(hidden_urls)
        if not text:
            continue
        item_time = _message_datetime(item)
        if item_time is None:
            continue
        visible_count += 1
        # 保留消息内换行，续行缩进，方便在 TXT 中区分发言。
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        normalized = normalized.replace("\n", "\n    ")
        lines.append(
            f"[{item_time.strftime('%Y-%m-%d %H:%M:%S')}] "
            f"{_sender_name(item)}\n    {normalized}\n"
        )
    lines[2] = f"文字消息：{visible_count} 条"
    return "\n".join(lines).rstrip() + "\n"


def _links_text(messages: List[Message]) -> str:
    lines: List[str] = []
    seen = set()
    for item in messages:
        item_time = _message_datetime(item)
        if item_time is None:
            continue
        for url in _message_urls(item):
            if url in seen:
                continue
            seen.add(url)
            lines.append(f"[{item_time.strftime('%Y-%m-%d %H:%M:%S')}] {url}")
    return "\n".join(lines) + ("\n" if lines else "")


async def _download_documents(
    client: Client,
    messages: List[Message],
    directory: Path,
) -> int:
    directory.mkdir(parents=True, exist_ok=True)
    downloaded_count = 0
    for item in messages:
        if not _is_allowed_document(item):
            continue
        document = item.document
        original_name = str(getattr(document, "file_name", None) or "file")
        target_name = f"{getattr(item, 'id', 0)}_{_safe_name(original_name)}"
        target = directory / target_name
        try:
            downloaded = await _retry_flood(
                lambda: client.download_media(item, file_name=str(target))
            )
            if not downloaded or not Path(downloaded).is_file():
                raise RuntimeError("下载未返回完整文件")
            expected = getattr(document, "file_size", None)
            if expected is not None and Path(downloaded).stat().st_size != expected:
                raise RuntimeError("下载后的文件大小不符")
            downloaded_count += 1
        except Exception as exc:
            raise RuntimeError(
                f"文件 {original_name}（消息 {item.id}）下载失败：{exc}。"
                "未发送不完整压缩包，请重试。"
            ) from exc
    return downloaded_count


def _make_zip(source_directory: Path, output_path: Path) -> None:
    with zipfile.ZipFile(
        output_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as archive:
        for file_path in sorted(source_directory.rglob("*")):
            if file_path.is_file() and file_path != output_path:
                archive.write(file_path, file_path.relative_to(source_directory))


async def _send_saved_document(
    client: Client,
    file_path: Path,
    chat_title: str,
    start: datetime,
    end: datetime,
    message_count: int,
    file_count: int,
) -> None:
    caption_lines = [
        "<b>✦ 群聊记录备份</b>",
        "",
        "<blockquote>"
        f"<b>群聊：</b>{html.escape(chat_title)}\n"
        f"<b>时间：</b>{start.strftime('%Y-%m-%d %H:%M')} 至 {end.strftime('%Y-%m-%d %H:%M')}\n"
        f"<b>消息：</b>{message_count} 条",
    ]
    if file_path.suffix.lower() == ".zip":
        caption_lines[-1] += f"\n<b>文件：</b>{file_count} 个"
    caption_lines[-1] += "</blockquote>"
    await _retry_flood(lambda: client.send_document(
        "me",
        str(file_path),
        caption="\n".join(caption_lines),
        parse_mode=ParseMode.HTML,
    ))


async def _export(client: Client, command_message: Message, chat_id: int) -> None:
    chat_title = _chat_title(command_message)
    # 在执行时固定结束时刻，删除命令所耗时间不计入范围。
    settings = _settings(chat_id)
    captured_now = datetime.now(BEIJING).replace(microsecond=0)

    # 指令立即消失，导出记录里也不会出现 ,b。
    with contextlib.suppress(Exception):
        await command_message.delete()

    temporary_root: Optional[Path] = None
    try:
        start, end = _time_range(settings)
        if not settings.get("end"):
            end = captured_now
        if not settings.get("start"):
            start = captured_now.replace(hour=0, minute=0, second=0)
        safe_chat_title = _safe_name(chat_title, "chat")
        stamp = end.strftime("%Y%m%d_%H%M")
        temporary_root = Path(tempfile.mkdtemp(prefix="pagermaid_b_"))
        messages = await _collect_messages(
            client, chat_id, start, end, getattr(command_message, "id", None)
        )
        transcript_path = temporary_root / f"群聊记录_{safe_chat_title}_{stamp}.txt"
        transcript_path.write_text(
            _transcript(messages, chat_title, start, end),
            encoding="utf-8-sig",
        )

        file_count = 0
        if settings["type"] == "text":
            output_path = transcript_path
        else:
            links_path = temporary_root / "链接汇总.txt"
            links_path.write_text(_links_text(messages), encoding="utf-8-sig")
            file_count = await _download_documents(
                client,
                messages,
                temporary_root / "文件",
            )
            output_path = temporary_root / f"群聊备份_{safe_chat_title}_{stamp}.zip"
            await asyncio.to_thread(_make_zip, temporary_root, output_path)

        await _send_saved_document(
            client,
            output_path,
            chat_title,
            start,
            end,
            len(messages),
            file_count,
        )
    except Exception as exc:
        LOGGER.exception("群聊记录备份失败：chat_id=%s", chat_id)
        # 群里不留任何过程或失败消息，错误只发到收藏夹。
        with contextlib.suppress(Exception):
            await client.send_message(
                "me",
                "<b>✦ 群聊记录备份失败</b>\n\n"
                f"<blockquote>{html.escape(str(exc)[:1000])}</blockquote>",
                parse_mode=ParseMode.HTML,
            )
    finally:
        if temporary_root is not None:
            shutil.rmtree(temporary_root, ignore_errors=True)


async def _brief_result(message: Message, text: str, delay: int = 3) -> None:
    try:
        await message.edit(text, parse_mode=ParseMode.HTML)
        await asyncio.sleep(delay)
        await message.delete()
    except Exception:
        with contextlib.suppress(Exception):
            await message.delete()


@listener(
    command="b",
    description="记录群组每人发言及文件",
    parameters="<help|today|start|end|type>",
)
async def backup_group_chat(client: Client, message: Message):
    chat_id = _chat_id(message)
    if chat_id is None:
        return await _brief_result(message, "无法读取当前聊天。")

    chat = getattr(message, "chat", None)
    chat_type = str(getattr(chat, "type", "")).lower()
    if "private" in chat_type:
        return await _brief_result(message, "请在群聊中使用该指令。")

    arguments = _arguments(message)
    if not arguments:
        lock = _export_locks.setdefault(chat_id, asyncio.Lock())
        if lock.locked():
            with contextlib.suppress(Exception):
                await message.delete()
            return
        async with lock:
            await _export(client, message, chat_id)
        return

    parts = arguments.split(maxsplit=1)
    action = parts[0].lower()
    value = parts[1].strip() if len(parts) == 2 else ""

    if action in {"help", "menu", "菜单"}:
        return await message.edit(_help_text(chat_id), parse_mode=ParseMode.HTML)

    if action == "today" and not value:
        _update_settings(chat_id, start=None, end=None)
        return await _brief_result(message, "已恢复今日 00:00 至实时。")

    if action == "type":
        mode = value.lower()
        if mode not in {"text", "zip"}:
            return await _brief_result(message, "用法：<code>,b type text</code> 或 <code>,b type zip</code>", 6)
        _update_settings(chat_id, type=mode)
        result = "文字记录" if mode == "text" else "文字、链接和普通文件"
        return await _brief_result(message, f"保存类型已设为：{result}。")

    if action == "end" and value.lower() == "now":
        _update_settings(chat_id, end=None)
        return await _brief_result(message, "结束时间已恢复为实时。")

    if action in {"start", "end"}:
        if not value:
            example = "start 2026-09-22 08:30" if action == "start" else "end 2026-09-22 18:00"
            return await _brief_result(message, f"用法：<code>,b {example}</code>", 6)
        try:
            parsed = _parse_local_time(value)
            new_value = parsed.strftime("%Y-%m-%d %H:%M")
            candidate = _settings(chat_id)
            candidate[action] = new_value
            _time_range(candidate)
        except ValueError as exc:
            return await _brief_result(message, html.escape(str(exc)), 6)
        _update_settings(chat_id, **{action: new_value})
        label = "开始" if action == "start" else "结束"
        return await _brief_result(message, f"{label}时间已设为：{new_value}。")

    await _brief_result(message, "未知指令，请输入 <code>,b help</code> 查看菜单。", 6)
