"""PagerMaid: 每天固定时间发送消息。

版本: copy-only-v2
"""

import asyncio
import contextlib
import datetime
import html
from typing import List, Optional

from pagermaid.listener import listener

try:
    from pagermaid.dependence import sqlite
    from pagermaid.enums import Message
    from pagermaid.services import bot, scheduler
except ImportError:
    from pagermaid.scheduler import scheduler
    from pagermaid.single_utils import Message, sqlite
    from pagermaid import bot


DB_KEY = "ds_daily_tasks"
JOB_PREFIX = "ds_daily"


class DailyTask:
    def __init__(
        self,
        task_id: int,
        cid: int,
        msg: str,
        hour: int,
        minute: int,
        second: int = 0,
    ):
        self.task_id = task_id
        self.cid = cid
        self.msg = msg
        self.hour = hour
        self.minute = minute
        self.second = second

    @property
    def job_id(self) -> str:
        return f"{JOB_PREFIX}|{self.cid}|{self.task_id}"

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "cid": self.cid,
            "msg": self.msg,
            "hour": self.hour,
            "minute": self.minute,
            "second": self.second,
        }

    @classmethod
    def from_dict(cls, data: dict):
        return cls(**data)

    @classmethod
    def from_command(cls, task_id: int, cid: int, text: str):
        if "|" not in text:
            raise ValueError("请输入时间和消息内容")

        time_text, msg = text.split("|", 1)
        time_text = time_text.strip()
        msg = msg.strip()

        if not msg:
            raise ValueError("消息内容不能为空")

        parsed = None
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                parsed = datetime.datetime.strptime(time_text, fmt)
                break
            except ValueError:
                continue

        if parsed is None:
            raise ValueError("时间格式应为 HH:MM:SS 或 HH:MM")

        return cls(
            task_id=task_id,
            cid=cid,
            msg=msg,
            hour=parsed.hour,
            minute=parsed.minute,
            second=parsed.second,
        )

    def display(self) -> str:
        send_time = f"{self.hour:02d}:{self.minute:02d}:{self.second:02d}"
        safe_msg = html.escape(self.msg)
        return f"{self.task_id} - 群ID：{self.cid} - {send_time} - {safe_msg}"


class DailyTaskManager:
    def __init__(self):
        self.tasks: List[DailyTask] = []

    def load(self):
        self.tasks.clear()
        for data in sqlite.get(DB_KEY, []):
            try:
                self.tasks.append(DailyTask.from_dict(data))
            except Exception:
                continue

    def save(self):
        sqlite[DB_KEY] = [task.to_dict() for task in self.tasks]

    def next_id(self) -> int:
        return max((task.task_id for task in self.tasks), default=0) + 1

    def get(self, task_id: int) -> Optional[DailyTask]:
        return next((task for task in self.tasks if task.task_id == task_id), None)

    def add(self, task: DailyTask):
        self.tasks.append(task)
        self.register(task)
        self.save()

    def remove(self, task_id: int) -> bool:
        task = self.get(task_id)
        if not task:
            return False

        job = scheduler.get_job(task.job_id)
        if job:
            scheduler.remove_job(task.job_id)

        self.tasks.remove(task)
        self.save()
        return True

    def list_all(self) -> str:
        return "\n".join(task.display() for task in self.tasks)

    async def send_message(self, task: DailyTask):
        with contextlib.suppress(Exception):
            await bot.send_message(task.cid, task.msg)

    def register(self, task: DailyTask):
        scheduler.add_job(
            self.send_message,
            "cron",
            id=task.job_id,
            name=task.job_id,
            hour=task.hour,
            minute=task.minute,
            second=task.second,
            args=[task],
            replace_existing=True,
        )

    def register_all(self):
        for task in self.tasks:
            self.register(task)


async def edit_and_delete(message: Message, text: str, delay: int):
    """编辑当前指令消息，等待指定秒数后删除。"""
    await message.edit(text)
    await asyncio.sleep(delay)
    with contextlib.suppress(Exception):
        await message.delete()


# 插件热重载时，先清掉本插件已注册的任务，避免重复。
with contextlib.suppress(Exception):
    for job in scheduler.get_jobs():
        if str(job.id).startswith(f"{JOB_PREFIX}|"):
            scheduler.remove_job(job.id)


manager = DailyTaskManager()
manager.load()
manager.register_all()


HELP = """每天定时发送消息

新增任务：
<code>,ds 00:00:00 | 消息内容</code>

获取全部任务：
<code>,ds list</code>

删除任务：
<code>,ds rm</code>
"""


@listener(
    command="ds",
    need_admin=True,
    description="每天固定时间发送消息",
)
async def ds(message: Message):
    args = message.arguments.strip()

    if not args:
        return await message.edit(HELP)

    # 在任意群执行 ,ds list，都显示全部已注册任务；3 秒后自动删除。
    if args == "list":
        text = manager.list_all()
        if not text:
            return await edit_and_delete(message, "没有已注册的任务。", 3)
        return await edit_and_delete(
            message,
            f"已注册的每日任务：\n\n{text}",
            10,
        )

    # 删除任务：任务 ID 全局有效，可在任意群删除。
    if args == "rm":
        return await message.edit("<code>,ds rm 任务ID</code>")

    if args.startswith("rm "):
        parts = args.split()
        if len(parts) != 2:
            return await message.edit("<code>,ds rm 任务ID</code>")

        try:
            task_id = int(parts[1])
        except ValueError:
            return await message.edit("任务 ID 必须是数字。")

        if not manager.get(task_id):
            return await message.edit("该任务不存在。")

        manager.remove(task_id)
        return await edit_and_delete(
            message,
            f"已删除任务 <code>{task_id}</code>",
            1,
        )

    # 新增每天固定时间任务。
    try:
        task = DailyTask.from_command(
            task_id=manager.next_id(),
            cid=message.chat.id,
            text=args,
        )
        manager.add(task)
    except Exception as exc:
        return await message.edit(f"参数错误：{html.escape(str(exc))}")

    send_time = f"{task.hour:02d}:{task.minute:02d}:{task.second:02d}"
    return await edit_and_delete(
        message,
        f"已添加任务 <code>{task.task_id}</code>\n"
        f"每天 <code>{send_time}</code> 发送。",
        1,
    )