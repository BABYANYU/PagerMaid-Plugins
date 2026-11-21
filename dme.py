"""删除当前聊天中自己的消息；收藏夹支持清理转存及转发消息。"""

import asyncio
import contextlib
import logging

from pagermaid.enums import Client, Message
from pagermaid.listener import listener


RUNNING_TASKS = set()
LOGGER = logging.getLogger("pagermaid")


def is_service_message(item):
    """System actions are not user-authored chat content (including join notices)."""
    return bool(getattr(item, "service", None)) or getattr(getattr(item, "raw", None), "action", None) is not None


async def confirm_deleted(bot, target, batch):
    """Check only the original IDs, allowing a short server visibility delay."""
    expected = set(batch)
    remaining = expected
    for delay in (0, 0.25, 0.75):
        if delay:
            await asyncio.sleep(delay)
        checked = await bot.get_messages(target, batch, replies=0)
        if (not isinstance(checked, (list, tuple)) or len(checked) != len(batch)
                or {getattr(item, "id", None) for item in checked} != expected):
            LOGGER.warning("DME verification response incomplete: batch_size=%d", len(batch))
            raise RuntimeError("无法核实删除结果，请稍后重试")
        confirmed = {item.id for item in checked
                     if getattr(item, "empty", False) is True or is_service_message(item)}
        remaining = expected - confirmed
        if not remaining:
            return
    LOGGER.warning("DME deletion unconfirmed after 3 checks: remaining=%d batch_size=%d", len(remaining), len(batch))
    raise RuntimeError(f"仍有 {len(remaining)} 条消息未确认删除，请检查权限后重试")


async def delete_with_progress(bot: Client, message: Message, message_ids, notification, saved=False):
    """Delete messages while updating one temporary progress message."""
    unique_ids = list(dict.fromkeys(message_ids))
    total = len(unique_ids)
    target = "me" if saved else message.chat.id
    batch_size = 1 if total <= 20 else 5 if total <= 100 else 20
    completed = 0
    for start in range(0, total, batch_size):
        batch = unique_ids[start : start + batch_size]
        affected = await bot.delete_messages(target, batch)
        if affected == 0:
            # MTProto pts_count is an update counter, not the number deleted.
            await confirm_deleted(bot, target, batch)
        completed += len(batch)
        with contextlib.suppress(Exception):
            await notification.edit(f"正在清理 · {completed}/{total}")


@listener(
    is_plugin=False,
    outgoing=True,
    command="d",
    need_admin=False,
    description="删除当前聊天中自己的消息，收藏夹支持删除保存的消息。",
    parameters="[数量]",
    priority=1,
    block_process=True,
    ignore_edited=True,
)
async def dme(bot: Client, message: Message):
    """Delete all own messages, or only the requested latest count."""
    parameters = list(getattr(message, "parameter", None) or [])
    requested = None
    if parameters:
        if len(parameters) != 1:
            return await message.edit("格式：,d 或 ,d 数量")
        try:
            requested = int(parameters[0])
        except (TypeError, ValueError):
            return await message.edit("格式：,d 或 ,d 数量")
        if requested <= 0:
            return await message.edit("删除数量必须大于 0")

    chat_id = message.chat.id
    command_id = message.id
    task_key = (chat_id, command_id)
    if task_key in RUNNING_TASKS:
        return
    RUNNING_TASKS.add(task_key)
    notification = None
    command_deleted = False
    try:
        # 先移除有效指令，再读取历史、执行清理。
        await message.delete()
        command_deleted = True
        message_ids = []
        me = getattr(bot, "me", None)
        if me is None:
            me = await bot.get_me()
        saved = chat_id == me.id
        options = {}
        thread_id = getattr(message, "message_thread_id", None)
        if thread_id is not None and not saved:
            options["message_thread_id"] = thread_id
        # 明确使用 InputPeerSelf 对应的 "me"，从命令之前读取收藏夹。
        # 收藏夹不按发送者过滤；其他聊天仍只搜索自己的消息。
        history = (
            bot.get_chat_history("me", offset_id=command_id)
            if saved
            else bot.search_messages(chat_id, from_user="me")
        )
        async for item in history:
            item_id = getattr(item, "id", None)
            if item_id is None or item_id >= command_id or getattr(item, "empty", False) or is_service_message(item):
                continue
            message_ids.append(item_id)
            if requested is not None and len(message_ids) >= requested:
                break

        if not message_ids:
            # 空结果静默结束，不发送提示。
            return
        notification = await bot.send_message(
            "me" if saved else chat_id,
            f"正在清理 · 0/{len(set(message_ids))}",
            **options,
        )
        await delete_with_progress(bot, message, message_ids, notification, saved=saved)
        with contextlib.suppress(Exception):
            await notification.delete()
    except Exception as exc:
        LOGGER.warning("DME cleanup failed: %s", type(exc).__name__)
        detail = str(exc).replace("\n", " ")[:240]
        error = f"清理未完成 · {type(exc).__name__}\n{detail}"
        if notification is not None:
            await notification.edit(error)
        elif not command_deleted:
            await message.edit(error)
        else:
            await bot.send_message(chat_id, error)
    finally:
        RUNNING_TASKS.discard(task_key)
