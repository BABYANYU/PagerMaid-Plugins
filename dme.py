"""删除当前聊天中自己的消息；收藏夹支持清理转存及转发消息。"""

import contextlib

from pagermaid.enums import Client, Message
from pagermaid.listener import listener


RUNNING_TASKS = set()


async def delete_with_progress(bot: Client, message: Message, message_ids, saved=False):
    """Delete messages while updating one temporary progress message."""
    unique_ids = list(dict.fromkeys(message_ids))
    total = len(unique_ids)
    target = "me" if saved else message.chat.id
    options = {}
    thread_id = getattr(message, "message_thread_id", None)
    if thread_id is not None and not saved:
        options["message_thread_id"] = thread_id
    notification = await bot.send_message(target, f"正在清理 · 0/{total}", **options)
    batch_size = 1 if total <= 20 else 5 if total <= 100 else 20
    completed = 0
    try:
        for start in range(0, total, batch_size):
            batch = unique_ids[start : start + batch_size]
            affected = await bot.delete_messages(target, batch)
            if affected == 0:
                # 未确认删除时停止本轮，静默退出，不误报已完成数量。
                return
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
    try:
        message_ids = []
        me = getattr(bot, "me", None)
        if me is None:
            me = await bot.get_me()
        saved = chat_id == me.id
        # 明确使用 InputPeerSelf 对应的 "me"，从命令之前读取收藏夹。
        # 收藏夹不按发送者过滤；其他聊天仍只搜索自己的消息。
        history = (
            bot.get_chat_history("me", offset_id=command_id)
            if saved
            else bot.search_messages(chat_id, from_user="me")
        )
        async for item in history:
            item_id = getattr(item, "id", None)
            if item_id is None or item_id == command_id or getattr(item, "empty", False):
                continue
            message_ids.append(item_id)
            if requested is not None and len(message_ids) >= requested:
                break

        if not message_ids:
            # 没有自己的历史消息属于正常空结果，静默清理命令即可。
            with contextlib.suppress(Exception):
                await message.delete()
            return
        await delete_with_progress(bot, message, message_ids, saved=saved)
        with contextlib.suppress(Exception):
            await message.delete()
    except Exception as exc:
        # 命令保留至目标删除完成，失败时仍有可编辑的消息承载诊断。
        detail = str(exc).replace("\n", " ")[:240]
        await message.edit(f"清理未完成 · {type(exc).__name__}\n{detail}")
    finally:
        RUNNING_TASKS.discard(task_key)
