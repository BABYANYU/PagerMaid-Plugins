# -*- coding: utf-8 -*-
"""PagerMaid-Pyro 暖群插件：使用已登录的 Codex CLI。"""
import asyncio
import contextlib
import html
import json
import re
import shutil
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pyrogram import enums
from pyrogram.errors import FloodWait
from pagermaid.enums import Client, Message
from pagermaid.listener import listener
from pagermaid.static import working_dir

TITLE = "YanyuBot - 暖群"
STATE_PATH = Path(working_dir) / "data" / "nq.json"
CODEX_WORKDIR = Path(working_dir) / "data" / "nq_codex_workspace"
RPC_TIMEOUT, ANSWER_TIMEOUT, MAX_CONTEXT = 30, 90, 600
HISTORY_LIMIT, HISTORY_TTL = 10, 10 * 60
MODEL_ORDER = (
    "gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-6-astra",
    "gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano",
)
MODEL_LABELS = {
    "gpt-5.6-luna": "GPT-5.6-Luna", "gpt-5.6-sol": "GPT-5.6-Sol",
    "gpt-5.6-terra": "GPT-5.6-Terra", "gpt-6-astra": "GPT-6-Astra",
    "gpt-5.5": "GPT-5.5", "gpt-5.4": "GPT-5.4",
    "gpt-5.4-mini": "GPT-5.4-Mini", "gpt-5.4-nano": "GPT-5.4-Nano",
}
EFFORT_LABELS = {
    "none": "无", "minimal": "最低", "low": "轻度", "medium": "中",
    "high": "高", "xhigh": "极高", "max": "最大", "ultra": "超高",
}
DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "shell_snapshot", "hooks", "plugins", "apps",
    "multi_agent", "multi_agent_v2", "code_mode", "code_mode_host",
    "computer_use", "browser_use", "image_generation", "memories",
    "skill_mcp_dependency_install", "view_image",
)
PERSONA = """你是 Telegram 群聊里一位可爱、贴心、活泼的萝莉系小伙伴。
你的特点是软萌亲切、善解人意，说话自然灵动，偶尔使用“呀、呢、啦、好哒”等语气词，
但不能每句话都用，也不要刻意装幼稚、撒娇过度或使用固定口头禅。
先准确理解对方在说什么，再像熟悉的朋友一样回应：问候就自然回应，提问就认真回答，
请求帮助就给出有用办法，分享开心的事就一起开心，难过或疲惫时才贴心安慰。
不要无条件附和，不要把普通消息强行写成鼓励文案，不复述问题，不答非所问。
结合提供的该成员独立对话记录理解代词和连续追问，但绝不能臆造记录中没有的信息。
只输出最终回复，不写分析、称呼、引号、前缀或说明；通常10至50个汉字，最多一至两句。
默认不用表情符号，不调用工具、不联网、不访问文件。"""
MEDIA_CONTEXT = {
    "photo": "对方发了一张图片", "video": "对方发了一段视频",
    "animation": "对方发了一个动图", "sticker": "对方发了一个表情",
    "voice": "对方发了一条语音", "audio": "对方发了一段音频",
    "document": "对方发了一个文件", "poll": "对方发起了一个投票",
    "location": "对方分享了一个位置",
}


def default_state():
    return {"chats": {}, "settings": {"model": "gpt-5.6-luna", "effort": "low"}}


def load_state():
    clean = default_state()
    try:
        saved = json.loads(STATE_PATH.read_text("utf-8"))
        if isinstance(saved.get("chats"), dict):
            clean["chats"] = saved["chats"]
        settings = saved.get("settings", {})
        if settings.get("model") in MODEL_ORDER:
            clean["settings"]["model"] = settings["model"]
        if settings.get("effort") in EFFORT_LABELS:
            clean["settings"]["effort"] = settings["effort"]
    except Exception:
        pass
    return clean


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
    temporary.replace(STATE_PATH)


STATE = load_state()
save_state(STATE)  # 重写旧配置，彻底移除第三方 API URL、Key 和模型设置。
STATE_LOCK, CODEX_LOCK = asyncio.Lock(), asyncio.Lock()
MESSAGE_QUEUE, QUEUE_WORKER_TASK = asyncio.Queue(), None
CODEX_RPC, MODEL_CACHE = None, []
MEMBER_HISTORY: Dict[Tuple[str, str], Dict[str, Any]] = {}
RUNTIME_STATS = {"received": 0, "replied": 0, "failed": 0, "processing": 0}
LAST_ERROR, LAST_OK = "", False


def find_codex():
    found = shutil.which("codex") or shutil.which("codex.exe")
    if found:
        return found
    for path in (
        Path("/usr/local/bin/codex"), Path.home() / ".local/bin/codex",
        Path.home() / ".npm-global/bin/codex", Path("/usr/bin/codex"),
    ):
        if path.is_file():
            return str(path)
    raise RuntimeError("未找到 Codex CLI，请确认容器内 /usr/local/bin/codex 可执行")


class CodexRPC:
    """常驻 App Server，避免每条群消息重复启动 Codex。"""
    def __init__(self, executable, cwd):
        self.executable, self.cwd = executable, cwd
        self.process, self.sequence, self.events = None, 0, deque()

    async def start(self):
        overrides = []
        for name in DISABLED_FEATURES:
            overrides.extend(["-c", f"features.{name}=false"])
        self.process = await asyncio.create_subprocess_exec(
            self.executable, "app-server", *overrides,
            "-c", 'web_search="disabled"', cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=4 * 1024 * 1024,
        )
        await self.call("initialize", {
            "clientInfo": {"name": "pagermaid-nq", "version": "2.0.0"},
            "capabilities": {"experimentalApi": True},
        })
        await self.write({"method": "initialized", "params": {}})
        return self

    def alive(self):
        return self.process is not None and self.process.returncode is None

    async def close(self):
        if self.process is not None and self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 3)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self.process.kill()
                await self.process.wait()
        self.process, self.events = None, deque()

    async def write(self, payload):
        if not self.alive():
            raise RuntimeError("Codex 服务未运行")
        self.process.stdin.write(
            (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        )
        await self.process.stdin.drain()

    async def read(self):
        while True:
            if not self.alive():
                raise RuntimeError("Codex 子进程已退出，请检查登录状态和 CLI 版本")
            line = await self.process.stdout.readline()
            if not line:
                raise RuntimeError("Codex 子进程没有返回数据")
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(event, dict):
                continue
            if "method" in event and "id" in event:
                await self.write({"id": event["id"], "error": {
                    "code": -32601, "message": "暖群模式禁止调用工具和额外权限"
                }})
                raise RuntimeError("模型尝试调用工具；暖群模式仅允许文字聊天")
            return event

    async def call(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        await self.write({"id": request_id, "method": method, "params": params})

        async def receive():
            while True:
                event = await self.read()
                if event.get("id") == request_id:
                    if event.get("error"):
                        error = event["error"]
                        raise RuntimeError(str(error.get("message") or error)[:500])
                    return event.get("result") or {}
                self.events.append(event)
        return await asyncio.wait_for(receive(), RPC_TIMEOUT)

    async def next_event(self):
        return self.events.popleft() if self.events else await self.read()


async def close_codex():
    global CODEX_RPC
    if CODEX_RPC is not None:
        await CODEX_RPC.close()
    CODEX_RPC = None


async def ensure_codex():
    global CODEX_RPC
    if CODEX_RPC is not None and CODEX_RPC.alive():
        return CODEX_RPC
    await close_codex()
    CODEX_WORKDIR.mkdir(parents=True, exist_ok=True)
    CODEX_RPC = CodexRPC(find_codex(), str(CODEX_WORKDIR))
    try:
        await CODEX_RPC.start()
        if not (await CODEX_RPC.call("account/read", {})).get("account"):
            raise RuntimeError("Codex 未登录，请在 PagerMaid 容器内执行 codex login")
        return CODEX_RPC
    except BaseException:
        await close_codex()
        raise


async def read_models(rpc):
    models, seen, cursors, cursor = [], set(), set(), None
    while True:
        params = {"limit": 100, "includeHidden": True}
        if cursor:
            params["cursor"] = cursor
        result = await rpc.call("model/list", params)
        for item in result.get("data", []):
            model_id = item.get("model") or item.get("id")
            if model_id in MODEL_ORDER and model_id not in seen:
                seen.add(model_id)
                models.append({
                    "model": model_id,
                    "displayName": item.get("displayName") or MODEL_LABELS[model_id],
                    "isDefault": bool(item.get("isDefault")),
                    "defaultReasoningEffort": item.get("defaultReasoningEffort"),
                    "supportedReasoningEfforts":
                        item.get("supportedReasoningEfforts") or [],
                })
        cursor = result.get("nextCursor")
        if not cursor:
            break
        if cursor in cursors:
            raise RuntimeError("模型列表分页异常")
        cursors.add(cursor)
    models.sort(key=lambda item: MODEL_ORDER.index(item["model"]))
    if not models:
        raise RuntimeError("当前账号没有返回候选模型，请更新 Codex CLI")
    return models


def effort_options(model):
    options = []
    for item in model.get("supportedReasoningEfforts", []):
        value = item.get("reasoningEffort") if isinstance(item, dict) else item
        if isinstance(value, str) and value not in options:
            options.append(value)
    return options


def selected_model(models):
    selected = STATE["settings"]["model"]
    model = next((item for item in models if item["model"] == selected), models[0])
    if selected != model["model"]:
        STATE["settings"]["model"] = model["model"]
        save_state(STATE)
    return model


def chosen_effort(model):
    options = effort_options(model)
    saved = STATE["settings"].get("effort", "low")
    if saved in options:
        return saved
    if "low" in options:
        return "low"
    return model.get("defaultReasoningEffort") or (options[0] if options else None)


async def refresh_models():
    global MODEL_CACHE
    async with CODEX_LOCK:
        MODEL_CACHE = await read_models(await ensure_codex())
    selected_model(MODEL_CACHE)
    return MODEL_CACHE


def disabled_config(config):
    result = {f"features.{name}": False for name in DISABLED_FEATURES}
    result.update({"web_search": "disabled", "project_doc_max_bytes": 0, "notify": []})
    for group in ("mcp_servers", "plugins"):
        for name in (config.get(group) or {}):
            result[f"{group}.{json.dumps(name, ensure_ascii=False)}.enabled"] = False
    return result


async def wait_answer(rpc, thread_id, turn_id):
    answers = {}
    while True:
        event = await rpc.next_event()
        params = event.get("params") or {}
        if params.get("threadId") != thread_id:
            continue
        if event.get("method") == "item/completed" and params.get("turnId") == turn_id:
            item = params.get("item") or {}
            if item.get("type") == "agentMessage" and item.get("phase") != "commentary":
                answers[item.get("id", str(len(answers)))] = item.get("text", "")
        elif event.get("method") == "turn/completed":
            turn = params.get("turn") or {}
            if turn.get("id") != turn_id:
                continue
            if turn.get("status") != "completed":
                error = turn.get("error") or {}
                raise RuntimeError(str(error.get("message") or "Codex 回答失败")[:500])
            text = "\n\n".join(x for x in answers.values() if x.strip()).strip()
            if not text:
                raise RuntimeError("Codex 没有返回最终回答")
            return text


def get_member_history(chat_id, sender_id):
    """读取某个群内某位成员的独立历史；10分钟未互动即失效。"""
    key = (str(chat_id), str(sender_id))
    item = MEMBER_HISTORY.get(key)
    if not item or time.monotonic() - item["updated"] > HISTORY_TTL:
        MEMBER_HISTORY.pop(key, None)
        return []
    return list(item["messages"])


def remember_member_history(chat_id, sender_id, user_text, answer):
    key = (str(chat_id), str(sender_id))
    previous = get_member_history(chat_id, sender_id)
    previous.extend([
        {"role": "群友", "content": user_text},
        {"role": "你", "content": answer},
    ])
    MEMBER_HISTORY[key] = {
        "updated": time.monotonic(),
        "messages": previous[-HISTORY_LIMIT:],
    }


def build_chat_prompt(context, name, history):
    parts = ["以下记录只属于当前群里的这位成员，不含其他人的对话："]
    if history:
        parts.extend(f"{item['role']}：{item['content']}" for item in history)
    else:
        parts.append("（暂无历史）")
    parts.extend(["", f"这位群友{name}现在说：", context, "", "请直接自然简短地回复当前消息。"])
    return "\n".join(parts)


async def request_codex(context, name, history=None):
    global MODEL_CACHE
    async with CODEX_LOCK:
        rpc = await ensure_codex()
        try:
            if not MODEL_CACHE:
                MODEL_CACHE = await read_models(rpc)
            model = selected_model(MODEL_CACHE)
            config = (await rpc.call("config/read", {"includeLayers": False})).get(
                "config"
            ) or {}
            started = await rpc.call("thread/start", {
                "model": model["model"], "cwd": str(CODEX_WORKDIR),
                "ephemeral": True, "sandbox": "read-only",
                "approvalPolicy": "never", "environments": [], "dynamicTools": [],
                "config": disabled_config(config), "developerInstructions": PERSONA,
            })
            thread_id = started["thread"]["id"]
            params = {
                "threadId": thread_id,
                "input": [{"type": "text", "text":
                    build_chat_prompt(context, name, history or [])}],
                "model": model["model"],
            }
            effort = chosen_effort(model)
            if effort:
                params["effort"] = effort
            turn = await rpc.call("turn/start", params)
            return await asyncio.wait_for(
                wait_answer(rpc, thread_id, turn["turn"]["id"]), ANSWER_TIMEOUT
            )
        except BaseException:
            await close_codex()
            raise


def clean_reply(value):
    text = re.sub(r"^[\s\"'“”‘’]+|[\s\"'“”‘’]+$", "", value or "")
    text = re.sub(r"^(回复|回答|暖心回复)[:：]\s*", "", text)
    text = " ".join(line.strip() for line in text.splitlines() if line.strip())
    if len(text) > 60:
        text = text[:59].rstrip("，、；：,. ") + "。"
    return text


def chat_config(chat_id, create=False):
    chats, key = STATE.setdefault("chats", {}), str(chat_id)
    if create:
        chats.setdefault(key, {"all": False, "targets": {}})
    return chats.get(key)


def sender_info(message):
    user = getattr(message, "from_user", None)
    if user is not None:
        name = " ".join(x for x in (user.first_name, user.last_name) if x).strip()
        return str(user.id), name or user.username or "群友", bool(user.is_bot)
    sender = getattr(message, "sender_chat", None)
    if sender is not None:
        return f"chat:{sender.id}", sender.title or "群友", False
    return None, "群友", False


def message_context(message):
    text = (getattr(message, "text", None) or
            getattr(message, "caption", None) or "").strip()
    if text:
        return text[:MAX_CONTEXT]
    for attr, description in MEDIA_CONTEXT.items():
        if getattr(message, attr, None) is not None:
            return description
    return "对方发来了一条消息"


async def process_message(client, chat_id, message_id, context, name, sender_id):
    global LAST_ERROR, LAST_OK
    try:
        history = get_member_history(chat_id, sender_id)
        reply = clean_reply(await request_codex(context, name, history))
        if not reply:
            raise RuntimeError("Codex 返回内容为空")
        while True:
            try:
                await client.send_message(
                    chat_id, reply, reply_to_message_id=message_id,
                    disable_web_page_preview=True,
                )
                break
            except FloodWait as exc:
                await asyncio.sleep(max(1, int(getattr(exc, "value", 1))))
        RUNTIME_STATS["replied"] += 1
        remember_member_history(chat_id, sender_id, context, reply)
        LAST_OK, LAST_ERROR = True, ""
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        RUNTIME_STATS["failed"] += 1
        LAST_OK = False
        LAST_ERROR = f"{exc.__class__.__name__}：{str(exc)[:160]}"


async def queue_worker():
    while True:
        item = await MESSAGE_QUEUE.get()
        RUNTIME_STATS["processing"] = 1
        try:
            await process_message(*item)
        finally:
            RUNTIME_STATS["processing"] = 0
            MESSAGE_QUEUE.task_done()


def enqueue(client, message, context, name, sender_id):
    global QUEUE_WORKER_TASK
    RUNTIME_STATS["received"] += 1
    MESSAGE_QUEUE.put_nowait(
        (client, message.chat.id, message.id, context, name, sender_id)
    )
    if QUEUE_WORKER_TASK is None or QUEUE_WORKER_TASK.done():
        QUEUE_WORKER_TASK = asyncio.create_task(queue_worker())


async def replied_message(client, message):
    replied = getattr(message, "reply_to_message", None)
    if replied is not None:
        return replied
    reply_id = getattr(message, "reply_to_message_id", None)
    if reply_id is not None:
        with contextlib.suppress(Exception):
            return await client.get_messages(message.chat.id, reply_id)
    return None


async def resolve_target(client, message, argument):
    username = re.search(r"@([A-Za-z0-9_]{3,64})", argument or "")
    if username:
        with contextlib.suppress(Exception):
            user = await client.get_users("@" + username.group(1))
            name = " ".join(x for x in (user.first_name, user.last_name) if x).strip()
            return str(user.id), name or "@" + username.group(1)
    replied = await replied_message(client, message)
    if replied is not None:
        sender_id, name, is_bot = sender_info(replied)
        if sender_id and not is_bot:
            return sender_id, name
    return None


async def settings_menu():
    models = await refresh_models()
    current, effort = selected_model(models), chosen_effort(selected_model(models))
    lines = [
        "<b>YanyuBot - 暖群设置</b>", "",
        f"当前模型：{html.escape(current['displayName'])}",
        f"当前推理强度：{html.escape(EFFORT_LABELS.get(effort, effort or '默认'))}",
        "", "模型：",
    ]
    for index, model in enumerate(models, 1):
        suffix = "（当前）" if model["model"] == current["model"] else ""
        lines += ["", f"{index}. {html.escape(model['displayName'])}{suffix}",
                  f"<code>,nq model {index}</code>"]
    lines += ["", "推理强度："]
    options = effort_options(current)
    if not options:
        lines += ["", "当前模型未返回可选推理强度"]
    for index, option in enumerate(options, 1):
        suffix = "（当前）" if option == effort else ""
        lines += ["", f"{index}. {html.escape(EFFORT_LABELS.get(option, option))}{suffix}",
                  f"<code>,nq effort {html.escape(option)}</code>"]
    return "\n".join(lines)


def help_text():
    return (
        f"<b>{TITLE}</b>\n\n<blockquote>"
        "<code>,nq all</code>　开启全群回复\n\n"
        "<code>,nq</code>　回复成员后单独开启\n\n"
        "<code>,nq @username</code>　指定成员开启\n\n"
        "<code>,nq del</code>　回复成员后取消\n\n"
        "<code>,nq off</code>　关闭当前群设置\n\n"
        "<code>,nq status</code>　查看运行状态\n\n"
        "<code>,nq model</code>　选择模型和推理强度\n\n"
        "<code>,nq test</code>　测试 Codex 连接</blockquote>"
    )


async def show(message, text, delay=8):
    await message.edit(text, parse_mode=enums.ParseMode.HTML)
    await asyncio.sleep(delay)
    with contextlib.suppress(Exception):
        await message.delete()


@listener(
    is_plugin=True, command="nq", outgoing=True,
    description="使用 Codex 的群聊暖心自动回复",
    parameters="<all/off/status/del/model/effort/test/@username>",
    priority=1, block_process=True, ignore_edited=True,
)
async def nq_command(client: Client, message: Message):
    global LAST_ERROR, LAST_OK
    argument = (getattr(message, "arguments", None) or "").strip()
    action = argument.split(maxsplit=1)[0].lower() if argument else ""

    if action in {"help", "帮助"}:
        return await show(message, help_text(), 15)
    if action == "model":
        try:
            parts = argument.split()
            if len(parts) == 1:
                return await show(message, await settings_menu(), 30)
            models = await refresh_models()
            index = int(parts[1]) - 1
            if index < 0:
                raise IndexError
            model = models[index]
            async with STATE_LOCK:
                STATE["settings"]["model"] = model["model"]
                options = effort_options(model)
                if options and STATE["settings"]["effort"] not in options:
                    STATE["settings"]["effort"] = "low" if "low" in options else options[0]
                save_state(STATE)
            return await show(message, f"模型已切换为 {html.escape(model['displayName'])}")
        except (ValueError, IndexError):
            return await show(message, "模型编号无效，请发送 ,nq model 查看")
        except Exception as exc:
            LAST_OK, LAST_ERROR = False, f"{exc.__class__.__name__}：{str(exc)[:160]}"
            return await show(message, f"<b>模型读取失败</b>\n\n"
                                      f"<blockquote>{html.escape(LAST_ERROR)}</blockquote>", 20)
    if action == "effort":
        value = argument.split(maxsplit=1)[1].lower() if " " in argument else ""
        try:
            models = MODEL_CACHE or await refresh_models()
            options = effort_options(selected_model(models))
            if value not in options:
                names = "、".join(EFFORT_LABELS.get(x, x) for x in options)
                return await show(message, f"当前模型支持：{html.escape(names or '未返回')}")
            async with STATE_LOCK:
                STATE["settings"]["effort"] = value
                save_state(STATE)
            return await show(message, f"推理强度已切换为 {EFFORT_LABELS.get(value, value)}")
        except Exception as exc:
            return await show(message, html.escape(str(exc)[:180]), 20)
    if action == "test":
        try:
            reply = clean_reply(await request_codex("你好", "接口测试"))
            LAST_OK, LAST_ERROR = True, ""
            model = STATE["settings"]["model"]
            return await show(message, "<b>Codex 测试成功</b>\n\n<blockquote>"
                              f"模型：{MODEL_LABELS.get(model, model)}\n"
                              f"回复：{html.escape(reply)}</blockquote>", 15)
        except Exception as exc:
            LAST_OK, LAST_ERROR = False, f"{exc.__class__.__name__}：{str(exc)[:160]}"
            return await show(message, "<b>Codex 测试失败</b>\n\n"
                              f"<blockquote>{html.escape(LAST_ERROR)}</blockquote>", 25)

    if getattr(message.chat, "type", None) not in {
        enums.ChatType.GROUP, enums.ChatType.SUPERGROUP
    }:
        return await show(message, "暖群功能请在群聊中设置")
    chat_id = message.chat.id
    if action == "all":
        async with STATE_LOCK:
            chat_config(chat_id, True)["all"] = True
            save_state(STATE)
        return await show(message, "全群回复已开启")
    if action in {"off", "clear", "关闭"}:
        async with STATE_LOCK:
            STATE["chats"].pop(str(chat_id), None)
            save_state(STATE)
        for key in [key for key in MEMBER_HISTORY if key[0] == str(chat_id)]:
            MEMBER_HISTORY.pop(key, None)
        return await show(message, "当前群回复已关闭")
    if action in {"status", "list", "状态"}:
        config = chat_config(chat_id)
        mode = "关闭" if not config else ("全群" if config.get("all") else "指定成员")
        connection = "连接正常" if LAST_OK else ("连接异常" if LAST_ERROR else "尚未测试")
        model, effort = STATE["settings"]["model"], STATE["settings"]["effort"]
        queued = MESSAGE_QUEUE.qsize() + RUNTIME_STATS["processing"]
        sessions = sum(1 for key in MEMBER_HISTORY if key[0] == str(chat_id))
        error = f"\n最近错误：{html.escape(LAST_ERROR)}" if LAST_ERROR else ""
        return await show(message, f"<b>{TITLE}</b>\n\n<blockquote>模式：{mode}\n"
                          f"Codex：{connection}\n模型：{MODEL_LABELS.get(model, model)}\n"
                          f"推理强度：{EFFORT_LABELS.get(effort, effort)}\n"
                          f"上下文：每人最近 {HISTORY_LIMIT} 条 · {sessions} 人\n"
                          f"处理：收到 {RUNTIME_STATS['received']} · "
                          f"已回复 {RUNTIME_STATS['replied']} · 排队 {queued} · "
                          f"失败 {RUNTIME_STATS['failed']}{error}</blockquote>", 15)
    if action in {"del", "remove", "取消"}:
        target = await resolve_target(client, message, argument)
        if not target:
            return await show(message, "请回复需要取消的成员消息后发送 ,nq del")
        target_id, name = target
        async with STATE_LOCK:
            config = chat_config(chat_id)
            if config:
                config.setdefault("targets", {}).pop(target_id, None)
                if not config.get("all") and not config.get("targets"):
                    STATE["chats"].pop(str(chat_id), None)
                save_state(STATE)
            MEMBER_HISTORY.pop((str(chat_id), str(target_id)), None)
        return await show(message, f"已取消对 {html.escape(name)} 的自动回复")
    target = await resolve_target(client, message, argument)
    if target:
        target_id, name = target
        async with STATE_LOCK:
            chat_config(chat_id, True).setdefault("targets", {})[target_id] = name
            save_state(STATE)
        return await show(message, f"已为 {html.escape(name)} 开启自动回复")
    await show(message, help_text(), 15)


@listener(
    is_plugin=True, incoming=True, outgoing=False, groups_only=True,
    priority=1, ignore_edited=True, ignore_forwarded=False,
)
async def nq_incoming(client: Client, message: Message):
    if getattr(message, "service", None) is not None:
        return
    sender_id, name, is_bot = sender_info(message)
    config = chat_config(message.chat.id)
    if not sender_id or is_bot or not config:
        return
    if not config.get("all") and sender_id not in config.get("targets", {}):
        return
    enqueue(client, message, message_context(message), name, sender_id)
