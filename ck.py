"""PagerMaid-Pyro 自动签到插件。

安装后使用：,ck help
配置文件：<PagerMaid 工作目录>/data/checkin_config.json
"""

import asyncio
import contextlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, List, Optional, Tuple, Union
from zoneinfo import ZoneInfo

import httpx
from pyrogram.enums import ParseMode
from pyrogram.raw.functions.messages import GetBotCallbackAnswer
from pyrogram.types import Message as PyrogramMessage

from pagermaid.dependence import scheduler
from pagermaid.enums import Message
from pagermaid.listener import listener
from pagermaid.services import bot
from pagermaid.static import working_dir
from pagermaid.utils import logs


PLUGIN_VERSION = "1.3.3"
TIME_ZONE = ZoneInfo("Asia/Shanghai")
CONFIG_PATH = Path(working_dir) / "data" / "checkin_config.json"
JOB_ID = "pagermaid_checkin_scheduler"
STEP_SEPARATOR = re.compile(r"(?<!\S)->(?!\S)")
SHORT_SEPARATOR = re.compile(r"(?<!\\)-")
MAX_BUTTON_STEPS = 10
BUTTON_DECORATION = re.compile(
    r"[\s\u200d\u20e3\ufe0e\ufe0f\u231a-\u231b\u23e9-\u23fa"
    r"\u25aa-\u25ab\u25fb-\u25fe\u2600-\u27bf\u2b50\u2b55"
    r"\U0001f170-\U0001f251\U0001f300-\U0001faff\U000e0020-\U000e007f]"
)


@dataclass
class ButtonMatcher:
    callback_data: str = ""
    button_text: str = ""


@dataclass
class SignTarget:
    name: str
    target: str
    command: str
    enabled: bool = True
    callback_data: str = ""
    button_text: str = ""
    button_sequence: str = ""


@dataclass
class CheckInConfig:
    run_time: str = "10:00"
    timeout: int = 15
    bot_token: str = ""
    push_chat_id: str = ""
    last_run_key: str = ""
    next_run_at: str = ""
    next_run_key: str = ""
    targets: List[SignTarget] = field(default_factory=list)


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
                            button_sequence=str(item.get("button_sequence") or ""),
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
    if target.button_sequence:
        return target.button_sequence
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
        return "", raw[5:].strip().replace(r"\-", "-")
    return raw, ""


def sequence_parts(raw: str) -> List[str]:
    if STEP_SEPARATOR.search(raw):
        return [part.strip() for part in STEP_SEPARATOR.split(raw)]
    if raw.startswith("text:") and SHORT_SEPARATOR.search(raw[5:]):
        return ["text:" + part.strip() for part in SHORT_SEPARATOR.split(raw[5:])]
    return []


def button_words(text: str) -> List[str]:
    return [
        word for candidate in text.split("|")
        if (word := BUTTON_DECORATION.sub("", candidate))
    ]


def parse_button_sequence(raw: str) -> List[ButtonMatcher]:
    parts = sequence_parts(raw)
    if not 2 <= len(parts) <= MAX_BUTTON_STEPS:
        raise ValueError(f"顺序点击需要 2～{MAX_BUTTON_STEPS} 步，可写 text:A-B-C")
    steps = []
    for index, part in enumerate(parts, 1):
        callback_data, button_text = parse_matcher([part])
        if not part.startswith(("text:", "data:")) or not (
            callback_data or button_words(button_text)
        ):
            raise ValueError(f"第 {index} 步需要填写 text:按钮 或 data:回调")
        steps.append(ButtonMatcher(callback_data, button_text))
    return steps


def find_button(message: PyrogramMessage, target: ButtonMatcher) -> Any:
    markup = getattr(message, "reply_markup", None)
    keyboard = getattr(markup, "inline_keyboard", None) or []
    candidates = button_words(target.button_text) if not target.callback_data else []
    for row in keyboard:
        for button in row:
            data = getattr(button, "callback_data", None)
            if not data:
                continue
            if isinstance(data, bytes):
                decoded = data.decode("utf-8", errors="replace")
            else:
                decoded = str(data or "")
            if target.callback_data and decoded == target.callback_data:
                return button
            if candidates:
                button_text = BUTTON_DECORATION.sub("", getattr(button, "text", "") or "")
                if any(candidate in button_text for candidate in candidates):
                    return button
    return None


def button_identity(button: Any) -> Tuple[Any, Any]:
    return (getattr(button, "text", None), getattr(button, "callback_data", None))


async def recent_messages(peer: Union[int, str], limit: int = 10) -> List[PyrogramMessage]:
    result = []
    async for item in bot.get_chat_history(peer, limit=limit):
        result.append(item)
    return result


async def wait_for_reply(
    peer: Union[int, str],
    after_id: int,
    timeout: int,
    target: Optional[ButtonMatcher] = None,
    edited_message: Optional[PyrogramMessage] = None,
) -> Optional[PyrogramMessage]:
    previous_button = (
        button_identity(find_button(edited_message, target))
        if edited_message is not None and target is not None else None
    )
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(1)
        if edited_message is not None and target is not None:
            try:
                current = await bot.get_messages(peer, edited_message.id)
                matching = find_button(current, target) if current and not current.outgoing else None
                if matching and button_identity(matching) != previous_button:
                    return current
            except Exception as exc:
                logs.warning(f"[CheckIn] 检查原消息更新失败: {exc}")
        try:
            for item in await recent_messages(peer):
                if item.id <= after_id or item.outgoing:
                    continue
                if target:
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
    peer: Union[int, str], message: PyrogramMessage, target: ButtonMatcher
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
        if target.button_sequence:
            steps = parse_button_sequence(target.button_sequence)
        elif target.callback_data or target.button_text:
            steps = [ButtonMatcher(target.callback_data, target.button_text)]
        else:
            steps = []
        timeout = store.data.timeout
        sent = await bot.send_message(peer, target.command, parse_mode=ParseMode.DISABLED)
        current = await wait_for_reply(peer, sent.id, timeout, steps[0] if steps else None)
        if not current:
            if steps:
                return False, f"第 1/{len(steps)} 步：{timeout} 秒内未找到指定按钮"
            return False, f"{timeout} 秒内未收到签到结果"
        if not steps:
            return True, message_text(current) or "签到命令已发送"

        for index, step in enumerate(steps[:-1], 1):
            # Existing buttons in earlier bot messages cannot advance the sequence.
            latest = await recent_messages(peer, limit=1)
            after_id = max(current.id, latest[0].id if latest else current.id)
            await click_button(peer, current, step)
            current = await wait_for_reply(peer, after_id, timeout, steps[index], current)
            if not current:
                return False, f"第 {index + 1}/{len(steps)} 步：{timeout} 秒内未找到指定按钮"
        old_text = message_text(current)
        callback = await click_button(peer, current, steps[-1])
        reply = await wait_for_callback_result(peer, current, old_text, timeout)
        return True, message_text(reply) or callback or old_text or "按钮已点击"
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


def generate_next_schedule(current: datetime) -> Tuple[datetime, str]:
    last_key = store.data.last_run_key
    run_time = parse_clock(store.data.run_time)
    for offset in range(-1, 4):
        key_day = current.date() + timedelta(days=offset)
        key = key_day.isoformat()
        if last_key and key <= last_key:
            continue
        candidate = datetime.combine(key_day, run_time, TIME_ZONE)
        if candidate >= current - timedelta(minutes=1):
            return candidate, key

    tomorrow = current.date() + timedelta(days=1)
    return datetime.combine(tomorrow, run_time, TIME_ZONE), tomorrow.isoformat()


def ensure_next_schedule(current: datetime) -> datetime:
    if store.data.next_run_at and store.data.next_run_key:
        try:
            scheduled = datetime.fromisoformat(store.data.next_run_at)
            if scheduled.tzinfo is None:
                scheduled = scheduled.replace(tzinfo=TIME_ZONE)
            scheduled = scheduled.astimezone(TIME_ZONE)
            if (
                store.data.next_run_key == scheduled.date().isoformat()
                and store.data.next_run_key > store.data.last_run_key
                and scheduled.timetz() == parse_clock(store.data.run_time)
            ):
                return scheduled
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


HELP_HTML = """<b>✦ Telegram Bot 自动签到</b>

<b>执行任务</b>
<code>,ck</code>　执行全部签到并推送

<b>任务管理</b>
<code>,ck list</code>　查看任务列表
<code>,ck del [序号]</code>　删除对应任务

<b>时间设置</b>
<code>,ck set time 00:00</code>　设定每日开始时间
<code>,ck set timeout 15</code>　每步等待，默认15秒

<b>推送设置</b>
<code>,ck set bot [BotToken] [ChatID]</code>　设置 Bot 推送
<code>,ck set bot clear</code>　清除推送配置

<b>添加任务示例</b>
<blockquote>任选一级菜单按钮</blockquote>
<code>,ck add youno @YounoEmbyAgain_bot text:签到|簽到</code>

<blockquote>按顺序点击</blockquote>
<code>,ck add 无限破甲 @XNB77_bot text:点击领取余额-点击领取每日750万token</code>"""


@listener(
    is_plugin=True,
    outgoing=True,
    command="ck",
    description="管理并定时执行 Telegram 机器人签到任务。",
    parameters="<help|del|list|set>",
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

    if action in {"del", "delete"}:
        if len(args) != 2 or not re.fullmatch(r"[1-9][0-9]*", args[1]):
            await edit_plain(message, "格式：,ck del 序号。请用 ,ck list 查看序号。")
            return
        previous = store.data.targets
        index = int(args[1]) - 1
        if index >= len(previous):
            await edit_plain(message, "没有这个序号。请用 ,ck list 查看当前列表。")
            return
        target = previous[index]
        store.data.targets = previous[:index] + previous[index + 1:]
        try:
            store.save()
        except OSError:
            store.data.targets = previous
            await edit_plain(message, "删除未保存，请检查配置文件是否可写。")
            return
        await edit_plain(message, f"✅ 已删除第 {index + 1} 项：{target.name}")
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

    if action == "set":
        if len(args) < 2:
            await edit_plain(message, "支持设置：time、timeout、bot")
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

        if kind == "timeout":
            try:
                number = int(value)
            except ValueError:
                await edit_plain(message, "请输入整数。")
                return
            minimum, maximum = 5, 120
            if not minimum <= number <= maximum:
                await edit_plain(message, f"请输入 {minimum}～{maximum} 之间的整数。")
                return
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

        await edit_plain(message, "未知设置项。支持：time、timeout、bot")
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
