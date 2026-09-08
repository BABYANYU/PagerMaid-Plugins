"""PagerMaid-Pyro 自动签到插件。

安装后使用：,ck help
配置文件：<PagerMaid 工作目录>/data/checkin_config.json
"""

import asyncio
import contextlib
import json
import random
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from zoneinfo import ZoneInfo

import httpx
from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.raw.functions.messages import GetBotCallbackAnswer
from pyrogram.types import Message as PyrogramMessage

from pagermaid.dependence import scheduler
from pagermaid.enums import Message
from pagermaid.listener import listener, raw_listener
from pagermaid.services import bot
from pagermaid.static import working_dir
from pagermaid.utils import logs


PLUGIN_VERSION = "1.1.0"
TIME_ZONE = ZoneInfo("Asia/Shanghai")
CONFIG_PATH = Path(working_dir) / "data" / "checkin_config.json"
JOB_ID = "pagermaid_checkin_scheduler"


@dataclass
class SignTarget:
    name: str
    target: str
    command: str
    enabled: bool = True
    callback_data: str = ""
    button_text: str = ""


@dataclass
class CheckInConfig:
    run_time: str = "10:00"
    run_time_end: str = ""
    random_delay: int = 0
    timeout: int = 15
    bot_token: str = ""
    push_chat_id: str = ""
    last_run_key: str = ""
    next_run_at: str = ""
    next_run_key: str = ""
    targets: List[SignTarget] = field(default_factory=list)


@dataclass
class PendingAdd:
    prompt_id: int
    name: str
    target: str
    callback_data: str = ""
    button_text: str = ""
    expires_at: float = 0.0


class ConfigStore:
    def __init__(self, path: Path):
        self.path = path
        self.data = self._load()

    def _load(self) -> CheckInConfig:
        if not self.path.exists():
            return CheckInConfig()
        try:
            raw = json.loads(self.path.read_text("utf-8"))
            targets = []
            for item in raw.get("targets", []):
                if not isinstance(item, dict):
                    continue
                try:
                    # 兼容 v1.0：旧版 id 自动成为新版唯一任务名。
                    targets.append(
                        SignTarget(
                            name=str(item.get("id") or item.get("name") or "").strip(),
                            target=str(item.get("target") or "").strip(),
                            command=str(item.get("command") or "").strip(),
                            enabled=bool(item.get("enabled", True)),
                            callback_data=str(item.get("callback_data") or ""),
                            button_text=str(item.get("button_text") or ""),
                        )
                    )
                except TypeError:
                    logs.warning("[CheckIn] 忽略无法解析的签到目标。")
            targets = [item for item in targets if item.name and item.target and item.command]
            allowed = {
                key: value
                for key, value in raw.items()
                if key in CheckInConfig.__dataclass_fields__ and key != "targets"
            }
            return CheckInConfig(**allowed, targets=targets)
        except Exception as exc:
            logs.error(f"[CheckIn] 读取配置失败: {exc}")
            return CheckInConfig()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(asdict(self.data), ensure_ascii=False, indent=2),
            "utf-8",
        )
        temp.replace(self.path)

    def reset_schedule(self) -> None:
        self.data.next_run_at = ""
        self.data.next_run_key = ""
        self.save()


store = ConfigStore(CONFIG_PATH)
pending_adds: Dict[int, PendingAdd] = {}
run_lock = asyncio.Lock()


def now_cn() -> datetime:
    return datetime.now(TIME_ZONE)


def parse_clock(value: str) -> time:
    parsed = datetime.strptime(value, "%H:%M")
    return time(parsed.hour, parsed.minute, tzinfo=TIME_ZONE)


def peer_value(value: str) -> Union[int, str]:
    stripped = str(value).strip()
    if stripped.lstrip("-").isdigit():
        return int(stripped)
    return stripped


def message_text(message: Optional[PyrogramMessage]) -> str:
    if not message:
        return ""
    return (message.text or message.caption or "").strip()


def target_matcher_text(target: SignTarget) -> str:
    if target.callback_data:
        return f"data:{target.callback_data}"
    if target.button_text:
        return f"text:{target.button_text}"
    return "直接回复"


def mask_secret(value: str) -> str:
    if not value:
        return "未设置"
    return "***" if len(value) <= 5 else f"{value[:5]}..."


async def edit_plain(message: Message, text: str) -> None:
    with contextlib.suppress(Exception):
        await message.edit(text, parse_mode=ParseMode.DISABLED)


async def edit_html(message: Message, text: str) -> None:
    with contextlib.suppress(Exception):
        await message.edit(text, parse_mode=ParseMode.HTML)


def parse_matcher(parts: List[str]) -> Tuple[str, str]:
    raw = " ".join(parts).strip()
    if not raw:
        return "", ""
    if raw.startswith("data:"):
        return raw[5:].strip(), ""
    if raw.startswith("text:"):
        return "", raw[5:].strip()
    return raw, ""


def find_button(message: PyrogramMessage, target: SignTarget) -> Any:
    markup = getattr(message, "reply_markup", None)
    keyboard = getattr(markup, "inline_keyboard", None) or []
    for row in keyboard:
        for button in row:
            data = getattr(button, "callback_data", None)
            if isinstance(data, bytes):
                decoded = data.decode("utf-8", errors="replace")
            else:
                decoded = str(data or "")
            if target.callback_data and decoded == target.callback_data:
                return button
            if not target.callback_data and target.button_text:
                button_text = getattr(button, "text", "") or ""
                candidates = [
                    item.strip()
                    for item in target.button_text.split("|")
                    if item.strip()
                ]
                if any(candidate in button_text for candidate in candidates):
                    return button
    return None


async def recent_messages(peer: Union[int, str], limit: int = 10) -> List[PyrogramMessage]:
    result = []
    async for item in bot.get_chat_history(peer, limit=limit):
        result.append(item)
    return result


async def wait_for_reply(
    peer: Union[int, str],
    after_id: int,
    timeout: int,
    target: Optional[SignTarget] = None,
) -> Optional[PyrogramMessage]:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(1)
        try:
            for item in await recent_messages(peer):
                if item.id <= after_id or item.outgoing:
                    continue
                if target and (target.callback_data or target.button_text):
                    if not find_button(item, target):
                        continue
                return item
        except Exception as exc:
            logs.warning(f"[CheckIn] 轮询消息失败: {exc}")
    return None


async def wait_for_callback_result(
    peer: Union[int, str],
    button_message: PyrogramMessage,
    old_text: str,
    timeout: int,
) -> Optional[PyrogramMessage]:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(1)
        try:
            current = await bot.get_messages(peer, button_message.id)
            if current and message_text(current) != old_text:
                return current
            for item in await recent_messages(peer):
                if item.id > button_message.id and not item.outgoing:
                    return item
        except Exception as exc:
            logs.warning(f"[CheckIn] 等待按钮结果失败: {exc}")
    return None


async def click_button(
    peer: Union[int, str], message: PyrogramMessage, target: SignTarget
) -> str:
    button = find_button(message, target)
    if not button:
        raise RuntimeError("未找到指定的回调按钮")
    data = getattr(button, "callback_data", None)
    if isinstance(data, str):
        data = data.encode("utf-8")
    if not data and target.callback_data:
        data = target.callback_data.encode("utf-8")
    if not data:
        raise RuntimeError("按钮没有 callback_data，无法点击")
    answer = await bot.invoke(
        GetBotCallbackAnswer(
            peer=await bot.resolve_peer(peer),
            msg_id=message.id,
            data=data,
        )
    )
    return str(getattr(answer, "message", "") or "")


async def run_single(target: SignTarget) -> Tuple[bool, str]:
    peer = peer_value(target.target)
    try:
        sent = await bot.send_message(peer, target.command, parse_mode=ParseMode.DISABLED)
        first = await wait_for_reply(peer, sent.id, store.data.timeout, target)
        if not first:
            return False, f"{store.data.timeout} 秒内未收到签到结果"
        if not (target.callback_data or target.button_text):
            return True, message_text(first) or "签到命令已发送"

        old_text = message_text(first)
        callback_answer = await click_button(peer, first, target)
        second = await wait_for_callback_result(
            peer, first, old_text, store.data.timeout
        )
        return True, message_text(second) or callback_answer or old_text or "按钮已点击"
    except Exception as exc:
        return False, str(exc) or "执行失败"


def build_summary(source: str, results: List[Tuple[SignTarget, bool, str]]) -> str:
    ok_count = sum(1 for _, ok, _ in results if ok)
    lines = [
        "CheckIn 签到汇总",
        f"时间：{now_cn().strftime('%Y-%m-%d %H:%M:%S')}",
        f"来源：{source}",
        f"结果：{ok_count} 成功 / {len(results) - ok_count} 失败",
        "",
    ]
    for index, (target, ok, detail) in enumerate(results, 1):
        lines.append(f"{'✅' if ok else '❌'} {index}. {target.name}")
        lines.append(f"   {detail or ('成功' if ok else '失败')}")
    return "\n".join(lines)


async def push_via_bot(text: str) -> bool:
    conf = store.data
    if not conf.bot_token or not conf.push_chat_id:
        return False
    url = f"https://api.telegram.org/bot{conf.bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                url,
                json={"chat_id": conf.push_chat_id, "text": text},
            )
            response.raise_for_status()
        return True
    except Exception as exc:
        logs.error(f"[CheckIn] Bot 推送失败: {exc}")
        return False


async def push_summary(text: str) -> bool:
    return await push_via_bot(text)


async def run_all(source: str, push: bool = True) -> str:
    enabled = [target for target in store.data.targets if target.enabled]
    if not enabled:
        return "没有启用的签到目标。"
    results = []
    for index, target in enumerate(enabled):
        ok, detail = await run_single(target)
        results.append((target, ok, detail))
        if index < len(enabled) - 1:
            await asyncio.sleep(2)
    summary = build_summary(source, results)
    if push:
        await push_summary(summary)
    return summary


def window_for_day(day: date) -> Tuple[datetime, datetime]:
    start = datetime.combine(day, parse_clock(store.data.run_time), TIME_ZONE)
    if not store.data.run_time_end:
        return start, start
    end = datetime.combine(day, parse_clock(store.data.run_time_end), TIME_ZONE)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def generate_next_schedule(current: datetime) -> Tuple[datetime, str]:
    last_key = store.data.last_run_key
    for offset in range(-1, 4):
        key_day = current.date() + timedelta(days=offset)
        key = key_day.isoformat()
        if last_key and key <= last_key:
            continue
        start, end = window_for_day(key_day)
        if start == end:
            candidate = start
        else:
            total_minutes = max(0, int((end - start).total_seconds() // 60))
            candidate = start + timedelta(minutes=random.randint(0, total_minutes))
        if store.data.random_delay:
            candidate += timedelta(minutes=random.randint(0, store.data.random_delay))
        if candidate >= current - timedelta(minutes=1):
            return candidate, key

    tomorrow = current.date() + timedelta(days=1)
    start, _ = window_for_day(tomorrow)
    return start, tomorrow.isoformat()


def ensure_next_schedule(current: datetime) -> datetime:
    if store.data.next_run_at and store.data.next_run_key:
        try:
            scheduled = datetime.fromisoformat(store.data.next_run_at)
            if scheduled.tzinfo is None:
                scheduled = scheduled.replace(tzinfo=TIME_ZONE)
            if store.data.next_run_key != store.data.last_run_key:
                return scheduled.astimezone(TIME_ZONE)
        except ValueError:
            pass

    scheduled, key = generate_next_schedule(current)
    store.data.next_run_at = scheduled.isoformat()
    store.data.next_run_key = key
    store.save()
    return scheduled


async def scheduler_tick() -> None:
    if run_lock.locked():
        return
    current = now_cn()
    scheduled = ensure_next_schedule(current)
    if current < scheduled:
        return
    if store.data.next_run_key == store.data.last_run_key:
        store.reset_schedule()
        ensure_next_schedule(current)
        return

    async with run_lock:
        run_key = store.data.next_run_key
        try:
            await run_all("自动定时任务", push=True)
        except Exception as exc:
            logs.error(f"[CheckIn] 定时任务失败: {exc}")
            return
        store.data.last_run_key = run_key
        store.data.next_run_at = ""
        store.data.next_run_key = ""
        store.save()
        ensure_next_schedule(now_cn())


HELP_HTML = """<b>Telegram Bot 自动签到</b>

<b>基础命令</b>
<code>,ck</code>
手动执行全部签到并推送

<code>,ck help</code>
显示本菜单

<code>,ck settings</code>
查看配置和下次执行时间

<code>,ck reset</code>
重置每日运行状态

<code>,ck testpush</code>
测试 Bot API 推送，不执行签到

<b>任务管理</b>
<code>,ck add [任务名] [目标] [text:按钮|data:回调]</code>
添加或更新任务；随后回复提示消息填写签到命令

<code>,ck list</code>
查看全部任务

<code>,ck test [任务名]</code>
真实测试一个任务

<code>,ck toggle [任务名]</code>
启用或停用任务

<code>,ck del [任务名]</code>
删除任务

<b>时间设置</b>
<code>,ck set time 00:00</code>
设置每天开始时间

<code>,ck set range 01:00</code>
在开始时间至结束时间之间随机执行

<code>,ck set range</code>
清除时间范围，改为固定时间

<code>,ck set delay 0</code>
设置额外随机延迟，范围 0～60 分钟

<code>,ck set timeout 20</code>
设置机器人回复超时，范围 5～120 秒

<b>推送设置</b>
<code>,ck set bot [BotToken] [ChatID]</code>
设置 Bot API 推送

<code>,ck set bot clear</code>
清除 Bot API 推送配置

<b>添加任务示例</b>
<code>,ck add youno @YounoEmbyAgain_bot text:签到|簽到</code>
然后回复插件消息：<code>/start</code>"""


@raw_listener(filters.me & filters.reply & ~filters.forwarded)
async def capture_checkin_command(_: Any, message: PyrogramMessage) -> None:
    chat_id = message.chat.id
    pending = pending_adds.get(chat_id)
    if not pending:
        return
    if asyncio.get_running_loop().time() > pending.expires_at:
        pending_adds.pop(chat_id, None)
        return
    if message.reply_to_message_id != pending.prompt_id:
        return
    command = message_text(message)
    if not command:
        await bot.send_message(chat_id, "签到命令不能为空，请重新回复提示消息。")
        return

    pending_adds.pop(chat_id, None)
    new_target = SignTarget(
        name=pending.name,
        target=pending.target,
        command=command,
        callback_data=pending.callback_data,
        button_text=pending.button_text,
    )
    old_index = next(
        (i for i, item in enumerate(store.data.targets) if item.name == new_target.name),
        -1,
    )
    if old_index >= 0:
        store.data.targets[old_index] = new_target
    else:
        store.data.targets.append(new_target)
    store.save()
    await bot.send_message(
        chat_id,
        f"✅ 已{'更新' if old_index >= 0 else '添加'}签到目标："
        f"{new_target.name}\n命令：{command}",
        parse_mode=ParseMode.DISABLED,
    )


@listener(
    is_plugin=True,
    outgoing=True,
    command="ck",
    description="管理并定时执行 Telegram 机器人签到任务。",
    parameters="<help|add|del|list|toggle|test|set|settings|reset>",
)
async def checkin(message: Message) -> None:
    args = list(message.parameter or [])
    action = args[0].lower() if args else ""

    if not action:
        if not store.data.targets:
            await edit_html(message, HELP_HTML)
            return
        if run_lock.locked():
            await edit_plain(message, "已有签到任务正在执行，请稍后再试。")
            return
        await edit_plain(message, "正在执行所有签到任务……")
        async with run_lock:
            summary = await run_all("手动触发", push=True)
        await edit_plain(message, summary)
        return

    if action in {"help", "h"}:
        await edit_html(message, HELP_HTML)
        return

    if action == "testpush":
        if not store.data.bot_token or not store.data.push_chat_id:
            await edit_plain(message, "请先使用 ,ck set bot Token ChatID 配置 Bot 推送。")
            return
        test_text = f"✅ CheckIn Bot 推送测试成功\n时间：{now_cn().strftime('%Y-%m-%d %H:%M:%S')}"
        if await push_via_bot(test_text):
            await edit_plain(message, "✅ Bot API 推送测试成功，请检查接收聊天。")
        else:
            await edit_plain(
                message,
                "❌ Bot API 推送失败。请确认已向通知机器人发送 /start，且 Token、ChatID 正确。",
            )
        return

    if action == "add":
        if len(args) < 3:
            await edit_plain(
                message,
                "格式：,ck add 任务名 目标 [text:按钮|data:回调]",
            )
            return
        callback_data, button_text = parse_matcher(args[3:])
        prompt = await bot.send_message(
            message.chat.id,
            "请回复此消息，发送签到命令（支持空格和特殊字符）。\n\n"
            f"任务名：{args[1]}\n目标：{args[2]}\n"
            f"按钮匹配：{('data:' + callback_data) if callback_data else ('text:' + button_text) if button_text else '无'}\n\n"
            "此添加请求10分钟后失效。",
            parse_mode=ParseMode.DISABLED,
        )
        pending_adds[message.chat.id] = PendingAdd(
            prompt_id=prompt.id,
            name=args[1],
            target=args[2],
            callback_data=callback_data,
            button_text=button_text,
            expires_at=asyncio.get_running_loop().time() + 600,
        )
        with contextlib.suppress(Exception):
            await message.delete()
        return

    if action in {"del", "delete"}:
        if len(args) < 2:
            await edit_plain(message, "格式：,ck del 任务名")
            return
        before = len(store.data.targets)
        store.data.targets = [item for item in store.data.targets if item.name != args[1]]
        store.save()
        text = "✅ 已删除目标。" if len(store.data.targets) < before else "没有找到该目标。"
        await edit_plain(message, text)
        return

    if action == "list":
        if not store.data.targets:
            await edit_plain(message, "当前没有配置签到目标。")
            return
        lines = ["签到目标列表：", ""]
        for index, target in enumerate(store.data.targets, 1):
            lines.extend(
                [
                    f"{'🟢' if target.enabled else '🔴'} {index}. {target.name}",
                    f"目标：{target.target}",
                    f"命令：{target.command}",
                    f"按钮：{target_matcher_text(target)}",
                    "",
                ]
            )
        await edit_plain(message, "\n".join(lines).rstrip())
        return

    if action == "toggle":
        if len(args) < 2:
            await edit_plain(message, "格式：,ck toggle 任务名")
            return
        target = next((item for item in store.data.targets if item.name == args[1]), None)
        if not target:
            await edit_plain(message, "没有找到该目标。")
            return
        target.enabled = not target.enabled
        store.save()
        await edit_plain(message, f"✅ 已{'启用' if target.enabled else '禁用'}：{target.name}")
        return

    if action == "test":
        if len(args) < 2:
            await edit_plain(message, "格式：,ck test 任务名")
            return
        target = next((item for item in store.data.targets if item.name == args[1]), None)
        if not target:
            await edit_plain(message, "没有找到该目标。")
            return
        await edit_plain(message, f"正在测试：{target.name}……")
        ok, detail = await run_single(target)
        await edit_plain(message, f"{'✅ 成功' if ok else '❌ 失败'}：{target.name}\n{detail}")
        return

    if action in {"settings", "config", "info"}:
        next_run = ensure_next_schedule(now_cn())
        end = store.data.run_time_end or "固定时间"
        enabled = sum(1 for item in store.data.targets if item.enabled)
        push_chat_display = store.data.push_chat_id or "未设置"
        if ":" in push_chat_display:
            push_chat_display = "配置错误（此处疑似填入了 Bot Token）"
        text = (
            "CheckIn 当前配置\n\n"
            f"开始时间：{store.data.run_time}\n"
            f"结束时间：{end}\n"
            f"额外延迟：0~{store.data.random_delay} 分钟\n"
            f"回复超时：{store.data.timeout} 秒\n"
            f"下次执行：{next_run.strftime('%Y-%m-%d %H:%M')}\n"
            f"Bot Token：{mask_secret(store.data.bot_token)}\n"
            f"Bot ChatID：{push_chat_display}\n"
            f"启用目标：{enabled}/{len(store.data.targets)}"
        )
        await edit_plain(message, text)
        return

    if action == "reset":
        store.data.last_run_key = ""
        store.data.next_run_at = ""
        store.data.next_run_key = ""
        store.save()
        next_run = ensure_next_schedule(now_cn())
        await edit_plain(
            message,
            f"✅ 已重置运行状态。下次执行：{next_run.strftime('%Y-%m-%d %H:%M')}",
        )
        return

    if action == "set":
        if len(args) < 2:
            await edit_plain(message, "支持设置：time、range、delay、timeout、bot")
            return
        kind = args[1].lower()
        value = args[2] if len(args) > 2 else ""

        if kind == "time":
            try:
                parse_clock(value)
            except ValueError:
                await edit_plain(message, "时间格式错误，请使用 HH:MM，例如 10:00。")
                return
            store.data.run_time = value
            store.reset_schedule()
            await edit_plain(message, f"✅ 开始时间已设置为 {value}。")
            return

        if kind == "range":
            if not value:
                store.data.run_time_end = ""
                store.reset_schedule()
                await edit_plain(message, "✅ 已改为固定时间执行。")
                return
            try:
                parse_clock(value)
            except ValueError:
                await edit_plain(message, "时间格式错误，请使用 HH:MM，例如 11:30。")
                return
            store.data.run_time_end = value
            store.reset_schedule()
            await edit_plain(
                message,
                f"✅ 将在 {store.data.run_time}～{value} 之间每天随机执行。",
            )
            return

        if kind in {"delay", "timeout"}:
            try:
                number = int(value)
            except ValueError:
                await edit_plain(message, "请输入整数。")
                return
            minimum, maximum = (0, 60) if kind == "delay" else (5, 120)
            if not minimum <= number <= maximum:
                await edit_plain(message, f"请输入 {minimum}～{maximum} 之间的整数。")
                return
            if kind == "delay":
                store.data.random_delay = number
                store.reset_schedule()
                await edit_plain(message, f"✅ 额外随机延迟已设置为 0～{number} 分钟。")
            else:
                store.data.timeout = number
                store.save()
                await edit_plain(message, f"✅ 机器人回复超时已设置为 {number} 秒。")
            return

        if kind == "bot":
            if value.lower() == "clear":
                store.data.bot_token = ""
                store.data.push_chat_id = ""
                store.save()
                await edit_plain(message, "✅ Bot 推送配置已清除。")
                return
            if len(args) < 4:
                await edit_plain(message, "格式：,ck set bot Token ChatID")
                return
            chat_id = args[3]
            if ":" not in value:
                if ":" in chat_id:
                    await edit_plain(
                        message,
                        "参数顺序填反了。正确格式：,ck set bot BotToken ChatID",
                    )
                else:
                    await edit_plain(message, "Bot Token 格式错误，Token 中应包含冒号。")
                return
            if ":" in chat_id:
                await edit_plain(message, "ChatID 格式错误，不能把 Bot Token 填入 ChatID。")
                return
            if not (chat_id.lstrip("-").isdigit() or chat_id.startswith("@")):
                await edit_plain(message, "ChatID 应为纯数字，频道也可以填写 @用户名。")
                return
            store.data.bot_token = value
            store.data.push_chat_id = chat_id
            store.save()
            await edit_plain(
                message,
                f"✅ Bot 推送已设置：{mask_secret(value)} / {chat_id}",
            )
            return

        await edit_plain(message, "未知设置项。支持：time、range、delay、timeout、bot")
        return

    await edit_plain(message, "未知命令，请使用 ,ck help 查看帮助。")


with contextlib.suppress(Exception):
    scheduler.remove_job(JOB_ID)

scheduler.add_job(
    scheduler_tick,
    "interval",
    minutes=1,
    id=JOB_ID,
    replace_existing=True,
    coalesce=True,
    max_instances=1,
)
