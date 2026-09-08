# -*- coding: utf-8 -*-
"""PagerMaid-Pyro 消息保存插件。

支持回复消息、Telegram 消息链接、批量/范围保存、转存到其他对话，
以及把媒体文件保存到 VPS 本地。受保护媒体采用下载后重新上传的方式。
"""

import asyncio
import contextlib
import html
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

from pagermaid.enums import Client, Message
from pagermaid.listener import listener
from pagermaid.static import working_dir
from pyrogram import enums
from pyrogram.errors import FloodWait, RPCError
from pyrogram.types import (
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
)


PLUGIN_DIR = Path(working_dir)
DATA_DIR = PLUGIN_DIR / "data"
CONFIG_PATH = DATA_DIR / "save_config.json"
LOCAL_SAVE_ROOT = PLUGIN_DIR / "save"
RANGE_LIMIT = 500
PROCESS_DELAY_SECONDS = 0.35

HELP_TEXT = """<b>消息保存插件</b>

<b>设置默认目标</b>
<code>,bc to me</code>　保存到收藏夹
<code>,bc to @username</code>　保存到指定对话
<code>,bc to -1001234567890</code>　使用 Chat ID
<code>,bc to local</code>　保存媒体到 VPS 本地
<code>,bc target</code>　查看当前目标

<b>来源显示</b>
<code>,bc source on</code>　开启来源说明
<code>,bc source off</code>　关闭来源说明
<code>,bc source</code>　查看当前状态

<b>保存消息</b>
回复消息后发送 <code>,bc</code>
<code>,bc 消息链接</code>
<code>,bc 链接1 链接2</code>　批量保存
<code>,bc 链接 @target</code>　临时指定目标
<code>,bc 链接 local</code>　临时保存到本地
<code>,bc 起始链接|结束链接</code>　保存同一对话内的消息范围

本地模式只保存媒体，目录为 <code>workdir/save/</code>。"""

_operation_lock = asyncio.Lock()
_config_lock = asyncio.Lock()


def command_arguments(message: Message) -> str:
    arguments = getattr(message, "arguments", None)
    if arguments is not None:
        return arguments.strip()
    content = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    parts = content.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def parse_target(value: str) -> Union[int, str]:
    value = value.strip()
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def parse_message_link(link: str) -> Optional[Tuple[Union[int, str], int]]:
    """解析公开链接和 t.me/c 私有频道链接。"""
    clean = link.strip().rstrip(".,;，。；").split("?", 1)[0]
    private_match = re.fullmatch(
        r"(?:https?://)?t\.me/c/(\d+)/(?:\d+/)?(\d+)/?", clean, re.IGNORECASE
    )
    if private_match:
        return int(f"-100{private_match.group(1)}"), int(private_match.group(2))

    public_match = re.fullmatch(
        r"(?:https?://)?t\.me/([A-Za-z][A-Za-z0-9_]{3,})/(?:\d+/)?(\d+)/?",
        clean,
        re.IGNORECASE,
    )
    if public_match:
        return public_match.group(1), int(public_match.group(2))
    return None


def parse_range(value: str) -> Optional[Tuple[Union[int, str], int, int]]:
    if value.count("|") != 1:
        return None
    left, right = value.split("|", 1)
    first = parse_message_link(left)
    last = parse_message_link(right)
    if not first or not last or str(first[0]).lower() != str(last[0]).lower():
        return None
    start_id, end_id = sorted((first[1], last[1]))
    return first[0], start_id, end_id


def source_link(message: Message) -> str:
    username = getattr(getattr(message, "chat", None), "username", None)
    if username:
        return f"https://t.me/{username}/{message.id}"
    chat_id = str(getattr(getattr(message, "chat", None), "id", ""))
    if chat_id.startswith("-100"):
        return f"https://t.me/c/{chat_id[4:]}/{message.id}"
    return ""


def message_text(message: Message) -> str:
    return (getattr(message, "caption", None) or getattr(message, "text", None) or "").strip()


def media_kind(message: Message) -> Optional[str]:
    for kind in (
        "photo",
        "video",
        "animation",
        "audio",
        "voice",
        "video_note",
        "document",
        "sticker",
    ):
        if getattr(message, kind, None):
            return kind
    return None


def is_missing_message(message: Optional[Message]) -> bool:
    return not message or bool(getattr(message, "empty", False))


def safe_segment(value: object, fallback: str = "chat") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.\-\u4e00-\u9fff]+", "_", str(value)).strip("_.")
    return (cleaned[:80] or fallback)


def unique_path(directory: Path, file_name: str) -> Path:
    source = Path(file_name)
    stem = source.stem or "file"
    suffix = source.suffix
    candidate = directory / f"{stem}{suffix}"
    counter = 1
    while candidate.exists():
        candidate = directory / f"{stem}_{counter}{suffix}"
        counter += 1
    return candidate


def _default_database() -> Dict[str, Dict[str, Dict[str, object]]]:
    return {"users": {}}


def load_database() -> Dict[str, Dict[str, Dict[str, object]]]:
    try:
        data = json.loads(CONFIG_PATH.read_text("utf-8"))
        if isinstance(data, dict) and isinstance(data.get("users"), dict):
            return data
    except (OSError, ValueError, TypeError):
        pass
    return _default_database()


async def get_user_config(user_id: str) -> Dict[str, object]:
    async with _config_lock:
        database = load_database()
        config = database["users"].get(user_id)
        if not isinstance(config, dict):
            config = {"target": "me", "show_source": False}
        return {
            "target": str(config.get("target") or "me"),
            "show_source": bool(config.get("show_source", False)),
        }


async def update_user_config(user_id: str, **changes: object) -> Dict[str, object]:
    async with _config_lock:
        database = load_database()
        current = database["users"].get(user_id)
        if not isinstance(current, dict):
            current = {"target": "me", "show_source": False}
        current.update(changes)
        database["users"][user_id] = current
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        temporary = CONFIG_PATH.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(database, ensure_ascii=False, indent=2), "utf-8")
        os.replace(temporary, CONFIG_PATH)
        return current


def user_key(message: Message) -> str:
    from_user = getattr(message, "from_user", None)
    return str(getattr(from_user, "id", None) or "self")


async def get_replied_message(client: Client, message: Message) -> Optional[Message]:
    reply = getattr(message, "reply_to_message", None)
    if reply:
        return reply
    reply_id = getattr(message, "reply_to_message_id", None)
    if not reply_id:
        reply_parameters = getattr(message, "reply_parameters", None)
        reply_id = getattr(reply_parameters, "message_id", None)
    if not reply_id:
        return None
    try:
        reply = await client.get_messages(message.chat.id, reply_id)
        return None if is_missing_message(reply) else reply
    except Exception:
        return None


async def edit_status(message: Message, text: str) -> None:
    try:
        await message.edit(text, parse_mode=enums.ParseMode.HTML)
    except Exception as exc:
        if "MESSAGE_NOT_MODIFIED" not in str(exc).upper():
            raise


async def get_message(client: Client, chat_id: Union[int, str], message_id: int) -> Optional[Message]:
    try:
        result = await client.get_messages(chat_id, message_id)
        return None if is_missing_message(result) else result
    except Exception:
        return None


async def expand_album(client: Client, message: Message) -> List[Message]:
    group_id = getattr(message, "media_group_id", None)
    if not group_id or not hasattr(client, "get_media_group"):
        return [message]
    try:
        group = await client.get_media_group(message.chat.id, message.id)
        valid = [item for item in group if not is_missing_message(item)]
        return sorted(valid, key=lambda item: item.id) or [message]
    except Exception:
        return [message]


async def collect_units(
    client: Client,
    references: Sequence[Tuple[Union[int, str], int]],
) -> Tuple[List[List[Message]], int]:
    units: List[List[Message]] = []
    seen_messages = set()
    missing = 0
    for chat_id, message_id in references:
        message = await get_message(client, chat_id, message_id)
        if not message:
            missing += 1
            continue
        key = (message.chat.id, message.id)
        if key in seen_messages:
            continue
        unit = await expand_album(client, message)
        fresh = [item for item in unit if (item.chat.id, item.id) not in seen_messages]
        if not fresh:
            continue
        for item in fresh:
            seen_messages.add((item.chat.id, item.id))
        units.append(fresh)
    return units, missing


async def download_message(client: Client, message: Message, directory: Path) -> Optional[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    downloaded = await client.download_media(message, file_name=str(directory) + os.sep)
    if not downloaded:
        return None
    file_path = Path(downloaded)
    return file_path if file_path.exists() and file_path.is_file() else None


def input_media(kind: str, file_path: Path, caption: str):
    kwargs = {"caption": caption or None, "parse_mode": enums.ParseMode.DISABLED}
    if kind == "photo":
        return InputMediaPhoto(str(file_path), **kwargs)
    if kind == "video":
        return InputMediaVideo(str(file_path), **kwargs)
    if kind == "audio":
        return InputMediaAudio(str(file_path), **kwargs)
    return InputMediaDocument(str(file_path), **kwargs)


async def upload_downloaded(
    client: Client,
    target: Union[int, str],
    message: Message,
    file_path: Path,
):
    caption = message_text(message)
    common = {"caption": caption or None, "parse_mode": enums.ParseMode.DISABLED}
    kind = media_kind(message)
    if kind == "photo":
        return await client.send_photo(target, str(file_path), **common)
    if kind == "video":
        return await client.send_video(target, str(file_path), **common)
    if kind == "animation":
        return await client.send_animation(target, str(file_path), **common)
    if kind == "audio":
        return await client.send_audio(target, str(file_path), **common)
    if kind == "voice":
        return await client.send_voice(target, str(file_path), **common)
    if kind == "video_note":
        return await client.send_video_note(target, str(file_path))
    if kind == "sticker":
        return await client.send_sticker(target, str(file_path))
    return await client.send_document(target, str(file_path), **common)


async def recreate_non_file_message(client: Client, target: Union[int, str], message: Message):
    text = getattr(message, "text", None)
    if text:
        return await client.send_message(
            target,
            text,
            parse_mode=enums.ParseMode.DISABLED,
            disable_web_page_preview=False,
        )
    poll = getattr(message, "poll", None)
    if poll:
        options = [option.text for option in poll.options]
        return await client.send_poll(
            target,
            poll.question,
            options,
            is_anonymous=poll.is_anonymous,
            type=poll.type,
            allows_multiple_answers=poll.allows_multiple_answers,
        )
    location = getattr(message, "location", None)
    venue = getattr(message, "venue", None)
    if venue:
        return await client.send_venue(
            target,
            venue.location.latitude,
            venue.location.longitude,
            venue.title,
            venue.address,
        )
    if location:
        return await client.send_location(target, location.latitude, location.longitude)
    contact = getattr(message, "contact", None)
    if contact:
        return await client.send_contact(
            target,
            contact.phone_number,
            contact.first_name,
            last_name=contact.last_name or "",
            vcard=contact.vcard or "",
        )
    dice = getattr(message, "dice", None)
    if dice:
        return await client.send_dice(target, emoji=dice.emoji)
    return await message.copy(target)


async def save_file_locally(client: Client, message: Message, group_name: str = "") -> Optional[Path]:
    kind = media_kind(message)
    if not kind:
        return None
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", "unknown")
    chat_title = getattr(chat, "title", None) or getattr(chat, "first_name", None) or str(chat_id)
    directory = LOCAL_SAVE_ROOT / safe_segment(chat_id)
    if group_name:
        directory /= safe_segment(group_name, "album")
    directory.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="pagermaid_save_") as temp_name:
        downloaded = await download_message(client, message, Path(temp_name))
        if not downloaded:
            return None
        destination = unique_path(directory, downloaded.name)
        shutil.move(str(downloaded), str(destination))

    metadata = {
        "source_chat_id": chat_id,
        "source_chat_title": chat_title,
        "source_message_id": message.id,
        "source_link": source_link(message),
        "media_type": kind,
        "caption": message_text(message),
        "file": str(destination.relative_to(PLUGIN_DIR)),
    }
    metadata_path = unique_path(directory, f"{destination.stem}.json")
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), "utf-8")
    return destination


async def send_source_note(client: Client, target: Union[int, str], messages: Sequence[Message]) -> None:
    lines = []
    for item in messages:
        chat = getattr(item, "chat", None)
        title = getattr(chat, "title", None) or getattr(chat, "first_name", None) or str(getattr(chat, "id", ""))
        link = source_link(item)
        item_id = f'<a href="{html.escape(link)}">{item.id}</a>' if link else str(item.id)
        lines.append(f"• <b>{html.escape(str(title))}</b> / {item_id}")
    await client.send_message(
        target,
        "<b>消息来源</b>\n" + "\n".join(lines),
        parse_mode=enums.ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def process_remote_unit(
    client: Client,
    target: Union[int, str],
    messages: Sequence[Message],
) -> int:
    # 相册使用下载后重新组成媒体组，无法组成时自动逐条发送。
    if len(messages) > 1 and all(media_kind(item) in {"photo", "video", "audio", "document"} for item in messages):
        with tempfile.TemporaryDirectory(prefix="pagermaid_save_album_") as temp_name:
            media_items = []
            for item in messages:
                downloaded = await download_message(client, item, Path(temp_name))
                if not downloaded:
                    media_items = []
                    break
                media_items.append(input_media(media_kind(item) or "document", downloaded, message_text(item)))
            if media_items:
                await client.send_media_group(target, media_items)
                return len(messages)

    sent = 0
    for item in messages:
        kind = media_kind(item)
        if kind:
            with tempfile.TemporaryDirectory(prefix="pagermaid_save_") as temp_name:
                downloaded = await download_message(client, item, Path(temp_name))
                if not downloaded:
                    continue
                await upload_downloaded(client, target, item, downloaded)
        else:
            await recreate_non_file_message(client, target, item)
        sent += 1
    return sent


async def process_local_unit(client: Client, messages: Sequence[Message]) -> Tuple[int, int]:
    saved = 0
    skipped = 0
    group_id = getattr(messages[0], "media_group_id", None) if messages else None
    group_name = f"album_{group_id}" if group_id and len(messages) > 1 else ""
    for item in messages:
        path = await save_file_locally(client, item, group_name)
        if path:
            saved += 1
        else:
            skipped += 1
    return saved, skipped


async def sleep_for_flood_wait(exc: FloodWait) -> None:
    seconds = int(getattr(exc, "value", None) or getattr(exc, "x", None) or 1)
    await asyncio.sleep(max(1, seconds))


@listener(
    is_plugin=True,
    outgoing=True,
    command="bc",
    description="保存或转存 Telegram 消息与媒体",
    parameters="[help|to|target|source|消息链接]",
    priority=1,
    ignore_forwarded=False,
    ignore_reacted=False,
)
async def bc_command(client: Client, message: Message):
    arguments = command_arguments(message)
    parts = arguments.split() if arguments else []
    action = parts[0].lower() if parts else ""
    owner_key = user_key(message)

    if action in {"help", "h"}:
        await edit_status(message, HELP_TEXT)
        return

    if action == "to":
        if len(parts) < 2:
            await edit_status(message, "请指定目标，例如 <code>,bc to me</code>")
            return
        target = " ".join(parts[1:]).strip()
        if target.lower() != "local":
            try:
                await client.get_chat(parse_target(target))
            except Exception as exc:
                await edit_status(message, f"无法访问目标：<code>{html.escape(target)}</code>\n{html.escape(str(exc))}")
                return
        await update_user_config(owner_key, target=target)
        await edit_status(message, f"默认保存目标已设置为：<code>{html.escape(target)}</code>")
        return

    if action == "target":
        config = await get_user_config(owner_key)
        await edit_status(message, f"当前默认保存目标：<code>{html.escape(str(config['target']))}</code>")
        return

    if action == "source":
        config = await get_user_config(owner_key)
        if len(parts) == 1:
            status = "开启" if config["show_source"] else "关闭"
            await edit_status(message, f"来源显示：<b>{status}</b>")
            return
        switch = parts[1].lower()
        if switch not in {"on", "off"}:
            await edit_status(message, "使用 <code>,bc source on</code> 或 <code>,bc source off</code>")
            return
        enabled = switch == "on"
        await update_user_config(owner_key, show_source=enabled)
        await edit_status(message, f"来源显示已{'开启' if enabled else '关闭'}")
        return

    config = await get_user_config(owner_key)
    target_text = str(config["target"])
    references: List[Tuple[Union[int, str], int]] = []
    range_request = None
    temporary_target = None
    unknown_parts: List[Tuple[int, str]] = []

    for index, part in enumerate(parts):
        parsed_range = parse_range(part)
        if parsed_range:
            range_request = parsed_range
            continue
        parsed_link = parse_message_link(part)
        if parsed_link:
            references.append(parsed_link)
            continue
        unknown_parts.append((index, part))

    if unknown_parts and unknown_parts[-1][0] == len(parts) - 1 and (references or range_request):
        temporary_target = unknown_parts[-1][1]

    if range_request:
        if references:
            await edit_status(message, "范围链接不能与其他消息链接混用。")
            return
        chat_id, start_id, end_id = range_request
        range_count = end_id - start_id + 1
        if range_count > RANGE_LIMIT:
            await edit_status(message, f"一次最多处理 {RANGE_LIMIT} 条消息，当前范围为 {range_count} 条。")
            return
        references = [(chat_id, item_id) for item_id in range(start_id, end_id + 1)]

    reply = None
    if not references:
        reply = await get_replied_message(client, message)
        if reply:
            references = [(reply.chat.id, reply.id)]
        elif not parts:
            await edit_status(message, HELP_TEXT)
            return
        else:
            await edit_status(message, "没有识别到有效的 Telegram 消息链接。")
            return

    if temporary_target:
        target_text = temporary_target
    local_mode = target_text.lower() == "local"
    target = parse_target(target_text)

    # 实际保存任务完全静默：删除命令，不显示过程、完成或失败提示。
    with contextlib.suppress(Exception):
        await message.delete()

    if _operation_lock.locked():
        print("bc 插件：已有保存任务正在执行，本次请求已忽略。")
        return

    async with _operation_lock:
        try:
            if reply and len(references) == 1:
                units = [await expand_album(client, reply)]
                missing = 0
            else:
                units, missing = await collect_units(client, references)
            if not units:
                print("bc 插件：没有获取到可保存的消息，请确认账号可以访问来源对话。")
                return

            saved = 0
            skipped = missing
            failed = 0
            source_messages: List[Message] = []
            total = len(units)
            for index, unit in enumerate(units, 1):
                try:
                    if local_mode:
                        unit_saved, unit_skipped = await process_local_unit(client, unit)
                        saved += unit_saved
                        skipped += unit_skipped
                    else:
                        saved += await process_remote_unit(client, target, unit)
                    source_messages.extend(unit)
                except FloodWait as exc:
                    await sleep_for_flood_wait(exc)
                    try:
                        if local_mode:
                            unit_saved, unit_skipped = await process_local_unit(client, unit)
                            saved += unit_saved
                            skipped += unit_skipped
                        else:
                            saved += await process_remote_unit(client, target, unit)
                        source_messages.extend(unit)
                    except Exception:
                        failed += len(unit)
                except Exception as exc:
                    print(f"bc 插件处理消息失败: {exc}")
                    failed += len(unit)
                if index < total:
                    await asyncio.sleep(PROCESS_DELAY_SECONDS)

            if not local_mode and config["show_source"] and source_messages:
                with contextlib.suppress(Exception):
                    await send_source_note(client, target, source_messages)
            print(
                f"bc 插件任务完成：模式={'local' if local_mode else 'remote'}，"
                f"成功={saved}，跳过={skipped}，失败={failed}"
            )
        except RPCError as exc:
            print(f"bc 插件 Telegram 请求失败：{exc}")
        except Exception as exc:
            print(f"bc 插件执行失败: {exc}")
