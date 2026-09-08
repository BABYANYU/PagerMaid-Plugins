"""PagerMaid-Pyro plugin: run URL commands through a bot and return images."""

import asyncio
import json
from pathlib import Path

from pyrogram import filters

from pagermaid.enums import Client, Message
from pagermaid.listener import listener
from pyromod.utils.errors import TimeoutConversationError


DEFAULT_BOT_ID = 5685382633
REPLY_TIMEOUT = 60
CONFIG_FILE = Path(__file__).with_name("ll_miaobot_config.json")

# The target is a single private chat. Serializing requests prevents one command
# from accidentally receiving the image generated for another command.
_request_lock = asyncio.Lock()


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
        "**LL_MiaoBot 菜单**\n\n"
        "`,/analyzeurl` — 分析链接并回传图片\n"
        "`,/pingurl` — Ping 链接并回传图片\n\n"
        "`，ll botid <Bot ID>` — 更换目标 Bot ID\n"
        "`，ll botid reset` — 恢复默认 Bot ID\n\n"
        f"默认 Bot ID：`{DEFAULT_BOT_ID}`"
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


IMAGE_FILTER = filters.incoming & filters.create(_image_message)


async def _run_url_command(
    bot: Client,
    message: Message,
    target_command: str,
):
    url = (message.arguments or "").strip()
    if not url:
        return await message.edit(f"用法：`,{target_command} 链接`")

    # A Telegram command cannot contain a line break. Reject it instead of
    # silently changing the value sent to the target bot.
    if "\n" in url or "\r" in url:
        return await message.edit("链接格式错误：请只输入一行链接。")

    origin_chat_id = message.chat.id

    await message.edit("LL_MiaoBot 测试中...")

    try:
        async with _request_lock:
            target_bot_id = _get_bot_id()
            # PagerMaid-Pyro bundles pyromod; ask() registers the listener before
            # sending, so even a very fast bot reply will not be missed.
            result: Message = await bot.ask(
                target_bot_id,
                f"{target_command} {url}",
                filters=IMAGE_FILTER,
                timeout=REPLY_TIMEOUT,
            )

            await bot.forward_messages(
                chat_id=origin_chat_id,
                from_chat_id=target_bot_id,
                message_ids=result.id,
            )
    except (TimeoutConversationError, asyncio.TimeoutError):
        # Leave no timeout/error notice in the originating chat.
        await message.delete()
        return

    # Remove the original command/status so only LL_MiaoBot's result remains.
    await message.delete()


@listener(
    command="/analyzeurl",
    description="将链接交给分析机器人，并把机器人生成的图片回传到当前聊天。",
)
async def analyze_url(bot: Client, message: Message):
    await _run_url_command(bot, message, "/analyzeurl")


@listener(
    command="/pingurl",
    description="将链接交给 Ping 机器人，并把机器人生成的图片回传到当前聊天。",
)
async def ping_url(bot: Client, message: Message):
    await _run_url_command(bot, message, "/pingurl")


@listener(
    command="ll",
    description="查看 LL_MiaoBot 菜单或设置目标 Bot ID。",
    parameters="<help|botid [Bot ID|reset]>",
)
async def ll_menu(_: Client, message: Message):
    arguments = (message.arguments or "").strip().split()

    if not arguments or arguments[0].lower() == "help":
        return await message.edit(_help_text())

    if arguments[0].lower() != "botid":
        return await message.edit(
            "未知菜单项。请输入 `，ll help` 查看可用指令。"
        )

    if len(arguments) == 1:
        return await message.edit("用法：`，ll botid <Bot ID|reset>`")

    if len(arguments) != 2:
        return await message.edit("用法：`，ll botid <Bot ID|reset>`")

    value = arguments[1].lower()
    if value == "reset":
        _save_bot_id(DEFAULT_BOT_ID)
        return await message.edit(
            f"✅ 已恢复默认 Bot ID：`{DEFAULT_BOT_ID}`"
        )

    try:
        bot_id = int(value)
    except ValueError:
        return await message.edit("Bot ID 必须是正整数。")

    if bot_id <= 0 or bot_id > 9_223_372_036_854_775_807:
        return await message.edit("Bot ID 必须是有效的正整数。")

    _save_bot_id(bot_id)
    await message.edit(f"✅ Bot ID 已更换为：`{bot_id}`")
