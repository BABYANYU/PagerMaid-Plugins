"""Delete self-authored messages from the current chat."""

import contextlib

from pagermaid.enums import Client, Message
from pagermaid.listener import listener


RUNNING_TASKS = set()


async def delete_with_progress(bot: Client, message: Message, message_ids):
    """Delete messages while updating one temporary progress message."""
    unique_ids = list(dict.fromkeys(message_ids))
    total = len(unique_ids)
    notification = await bot.send_message(
        message.chat.id,
        f"正在清理 · 0/{total}",
        message_thread_id=getattr(message, "message_thread_id", None),
    )
    batch_size = 1 if total <= 20 else 5 if total <= 100 else 20
    completed = 0
    try:
        for start in range(0, total, batch_size):
            batch = unique_ids[start : start + batch_size]
            await bot.delete_messages(message.chat.id, batch)
            completed += len(batch)
            with contextlib.suppress(Exception):
                await notification.edit(f"正在清理 · {completed}/{total}")
    finally:
        with contextlib.suppress(Exception):
            await notification.delete()


@listener(
    is_plugin=False,
    outgoing=True,
    command="d",
    need_admin=False,
    description="删除自己在当前群内的全部消息，或指定数量的最新消息。",
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
    try:
        message_ids = []
        async for item in bot.search_messages(chat_id, from_user="me"):
            item_id = getattr(item, "id", None)
            if item_id is None or item_id == command_id:
                continue
            message_ids.append(item_id)
            if requested is not None and len(message_ids) >= requested:
                break

        with contextlib.suppress(Exception):
            await message.delete()
        await delete_with_progress(bot, message, message_ids)
    finally:
        RUNNING_TASKS.discard(task_key)
