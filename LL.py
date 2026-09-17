"""PagerMaid-Pyro plugin: run URL commands through a bot and return images."""

import asyncio
import json
import re
from pathlib import Path
from typing import Optional

from pyrogram.enums import ParseMode

from pagermaid.enums import Client, Message
from pagermaid.listener import listener


DEFAULT_BOT_ID = 5685382633
REPLY_TIMEOUT = 60
CONFIG_FILE = Path(__file__).with_name("ll_miaobot_config.json")

URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_result_lock = asyncio.Lock()
_pending_request_ids = set()
_claimed_result_ids = set()


def _get_bot_id() -> int:
    """Read the configured bot ID, falling back safely to the default."""
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        bot_id = int(data["bot_id"])
        if bot_id > 0:
            return bot_id
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        pass
    return DEFAULT_BOT_ID


def _save_bot_id(bot_id: int) -> None:
    """Persist the bot ID atomically beside the installed plugin."""
    temporary = CONFIG_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"bot_id": bot_id}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(CONFIG_FILE)


def _help_text() -> str:
    return (
        "<b>✦ 聊天窗口测试机场</b>\n\n"
        "<blockquote>"
        f"<b>目标 Bot：</b><code>{_get_bot_id()}</code>"
        "</blockquote>\n\n"
        "<b>链接检测</b>\n\n"
        "<code>,a 链接</code>\n"
        "测试拓扑并返回结果\n\n"
        "<code>,p 链接</code>\n"
        "测试延迟并返回结果\n\n"
        "<code>,sp 链接</code>\n"
        "测试速度并返回结果\n\n"
        "<code>,g 链接</code>\n"
        "查询 GeoIP 并返回结果\n\n"
        "<b>Bot 设置</b>\n\n"
        "<code>,ll botid Bot ID</code>\n"
        "更换目标机器人"
    )


def _image_message(_, __, message: Message) -> bool:
    """Accept Telegram photos and image files sent as documents."""
    if message.photo:
        return True
    return bool(
        message.document
        and message.document.mime_type
        and message.document.mime_type.startswith("image/")
    )


async def _get_replied_message(bot: Client, message: Message):
    """Read the replied message using the same reliable flow as the IP plugin."""
    replied = getattr(message, "reply_to_message", None)

    reply_id = getattr(message, "reply_to_message_id", None)
    if reply_id is None:
        reply_id = getattr(message, "reply_to_msg_id", None)
    if reply_id is None and replied is not None:
        reply_id = getattr(replied, "id", None)

    if reply_id is not None:
        chat = getattr(message, "chat", None)
        chat_id = getattr(chat, "id", None) or getattr(message, "chat_id", None)
        if chat_id is not None:
            try:
                fetched = await bot.get_messages(
                    chat_id=chat_id,
                    message_ids=reply_id,
                    replies=0,
                )
                if fetched:
                    # Pyrogram normally returns one Message for one id, but keep
                    # compatibility with wrappers that may return a one-item list.
                    if isinstance(fetched, (list, tuple)):
                        return fetched[0] if fetched else replied
                    return fetched
            except Exception:
                pass

    return replied


def _parse_command_args(message: Message, command: str) -> str:
    """Parse arguments from pattern-listener messages without message.arguments."""
    text = (getattr(message, "text", None) or "").strip()
    match = re.match(
        rf"^(?:,|，){re.escape(command)}(?:\s+([\s\S]*))?$",
        text,
        re.IGNORECASE,
    )
    if not match:
        return ""
    return (match.group(1) or "").strip()


def _extract_url_from_text(text: str) -> str:
    """Extract the first HTTP(S) URL from arbitrary text."""
    match = URL_PATTERN.search(text)
    if match:
        return match.group(0).rstrip(".,;:!?)]}，。；：！？】》")
    return ""


def _extract_url(message: Message) -> str:
    """Extract the first visible or embedded HTTP(S) URL from a message."""
    if message is None:
        return ""

    text = (
        getattr(message, "text", None)
        or getattr(message, "caption", None)
        or ""
    )
    url = _extract_url_from_text(text)
    if url:
        return url

    entities = (
        getattr(message, "entities", None)
        or getattr(message, "caption_entities", None)
        or []
    )
    for entity in entities:
        url = str(getattr(entity, "url", None) or "").strip()
        if url.lower().startswith(("https://", "http://")):
            return url
    return ""


def _reply_to_id(message: Message) -> Optional[int]:
    reply_id = getattr(message, "reply_to_message_id", None)
    if reply_id is None:
        reply_id = getattr(message, "reply_to_msg_id", None)
    if reply_id is None:
        reply_id = getattr(
            getattr(message, "reply_to_message", None), "id", None
        )
    return reply_id


async def _wait_for_image_result(
    bot: Client,
    target_bot_id: int,
    request_id: int,
) -> Message:
    """Poll and correlate concurrent bot results without pyromod chat listeners."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + REPLY_TIMEOUT

    async with _result_lock:
        _pending_request_ids.add(request_id)

    try:
        while loop.time() < deadline:
            history = [
                item
                async for item in bot.get_chat_history(target_bot_id, limit=40)
            ]
            candidates = sorted(
                (
                    item for item in history
                    if item.id > request_id
                    and not getattr(item, "outgoing", False)
                    and _image_message(None, None, item)
                ),
                key=lambda item: item.id,
            )

            async with _result_lock:
                result = next(
                    (
                        item for item in candidates
                        if item.id not in _claimed_result_ids
                        and _reply_to_id(item) == request_id
                    ),
                    None,
                )

                # Some bots do not reply to the triggering command. In that
                # case only the oldest pending request may claim the oldest
                # unclaimed image, preserving FIFO as a safe fallback.
                if result is None and (
                    not _pending_request_ids
                    or request_id == min(_pending_request_ids)
                ):
                    result = next(
                        (
                            item for item in candidates
                            if item.id not in _claimed_result_ids
                            and (
                                _reply_to_id(item) is None
                                or _reply_to_id(item) == request_id
                            )
                        ),
                        None,
                    )

                if result is not None:
                    _claimed_result_ids.add(result.id)
                    if len(_claimed_result_ids) > 500:
                        newest = sorted(_claimed_result_ids)[-200:]
                        _claimed_result_ids.clear()
                        _claimed_result_ids.update(newest)
                    return result

            await asyncio.sleep(1)

        raise asyncio.TimeoutError
    finally:
        async with _result_lock:
            _pending_request_ids.discard(request_id)


async def _run_url_command(
    bot: Client,
    message: Message,
    target_command: str,
    user_command: str,
):
    # 1) Read a URL supplied directly after the command. Pattern listeners do
    # not rely on PagerMaid's message.arguments, matching the working IP plugin.
    args = _parse_command_args(message, user_command)
    url = _extract_url_from_text(args) if args else ""

    # 2) If no direct URL was supplied, explicitly fetch the replied message
    # before editing the command message, then extract visible/embedded links.
    if not url:
        try:
            replied = await asyncio.wait_for(
                _get_replied_message(bot, message),
                timeout=10,
            )
            url = _extract_url(replied)
        except asyncio.TimeoutError:
            return await message.edit("读取回复消息超时，请重新回复后再试。")
        except Exception as exc:
            return await message.edit(
                f"读取回复消息失败：{type(exc).__name__}"
            )

    if not url:
        return await message.edit(
            f"用法：`,{user_command} 链接`，或回复包含链接的消息发送 `,{user_command}`"
        )

    if "\n" in url or "\r" in url:
        return await message.edit("链接格式错误：请只输入一行链接。")

    origin_chat_id = message.chat.id
    await message.edit("LL_MiaoBot 测试中...")

    try:
        target_bot_id = _get_bot_id()
        request = await bot.send_message(
            target_bot_id,
            f"{target_command} {url}",
        )
        result = await _wait_for_image_result(
            bot,
            target_bot_id,
            request.id,
        )

        await bot.forward_messages(
            chat_id=origin_chat_id,
            from_chat_id=target_bot_id,
            message_ids=result.id,
        )
    except asyncio.TimeoutError:
        # Leave no timeout/error notice in the originating chat.
        await message.delete()
        return

    # Remove the original command/status so only LL_MiaoBot's result remains.
    await message.delete()


@listener(
    pattern=r"^(?:,|，)a(?:\s|$)[\s\S]*",
    priority=1,
    block_process=True,
    ignore_edited=True,
)
async def analyze_url(bot: Client, message: Message):
    await _run_url_command(bot, message, "/analyzeurl", "a")


@listener(
    pattern=r"^(?:,|，)p(?:\s|$)[\s\S]*",
    priority=1,
    block_process=True,
    ignore_edited=True,
)
async def ping_url(bot: Client, message: Message):
    await _run_url_command(bot, message, "/pingurl", "p")


@listener(
    pattern=r"^(?:,|，)sp(?:\s|$)[\s\S]*",
    priority=1,
    block_process=True,
    ignore_edited=True,
)
async def speed_url(bot: Client, message: Message):
    await _run_url_command(bot, message, "/speedurl", "sp")


@listener(
    pattern=r"^(?:,|，)g(?:\s|$)[\s\S]*",
    priority=1,
    block_process=True,
    ignore_edited=True,
)
async def geoip_url(bot: Client, message: Message):
    await _run_url_command(bot, message, "/geoipwurl", "g")


@listener(
    command="ll",
    description="查看 LL_MiaoBot 菜单或设置目标 Bot ID。",
    parameters="<botid Bot ID>",
)
async def ll_menu(_: Client, message: Message):
    arguments = (message.arguments or "").strip().split()

    if not arguments:
        return await message.edit(_help_text(), parse_mode=ParseMode.HTML)

    if arguments[0].lower() != "botid":
        return await message.edit("未知菜单项。请输入 `,ll` 查看可用指令。")

    if len(arguments) == 1:
        return await message.edit("用法：`,ll botid Bot ID`")

    if len(arguments) != 2:
        return await message.edit("用法：`,ll botid Bot ID`")

    value = arguments[1].lower()
    try:
        bot_id = int(value)
    except ValueError:
        return await message.edit("Bot ID 必须是正整数。")

    if bot_id <= 0 or bot_id > 9_223_372_036_854_775_807:
        return await message.edit("Bot ID 必须是有效的正整数。")

    _save_bot_id(bot_id)
    await message.edit(f"✅ Bot ID 已更换为：`{bot_id}`")
