"""PagerMaid-Pyro：查询 Codex 使用额度与可用重置。

安装后使用：,gpt help
本插件不保存 ChatGPT Token；它调用当前系统中已登录的 Codex CLI。
"""

import asyncio
import contextlib
import json
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from pyrogram.enums import ParseMode

from pagermaid.enums import Message
from pagermaid.listener import listener
from pagermaid.static import working_dir


TIME_ZONE = ZoneInfo("Asia/Shanghai")
CONFIG_PATH = Path(working_dir) / "data" / "gpt_codex_usage.json"
RPC_TIMEOUT = 20
RESULT_DELETE_DELAY = 20


def load_config() -> Dict[str, str]:
    try:
        raw = json.loads(CONFIG_PATH.read_text("utf-8"))
        return {"codex_path": str(raw.get("codex_path") or "").strip()}
    except Exception:
        return {"codex_path": ""}


def save_config(config: Dict[str, str]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = CONFIG_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(config, ensure_ascii=False, indent=2), "utf-8")
    temp.replace(CONFIG_PATH)


def find_codex() -> Optional[str]:
    configured = load_config().get("codex_path", "")
    if configured and Path(configured).is_file():
        return configured
    discovered = shutil.which("codex") or shutil.which("codex.exe")
    if discovered:
        return discovered
    for candidate in (
        Path.home() / ".npm-global" / "bin" / "codex",
        Path.home() / ".local" / "bin" / "codex",
        Path("/usr/local/bin/codex"),
        Path("/usr/bin/codex"),
    ):
        if candidate.is_file():
            return str(candidate)
    return None


async def edit_plain(message: Message, text: str) -> None:
    with contextlib.suppress(Exception):
        await message.edit(text, parse_mode=ParseMode.DISABLED)


async def edit_html(message: Message, text: str) -> None:
    with contextlib.suppress(Exception):
        await message.edit(text, parse_mode=ParseMode.HTML)


async def delete_after(message: Message, delay: int = RESULT_DELETE_DELAY) -> None:
    await asyncio.sleep(delay)
    with contextlib.suppress(Exception):
        await message.delete()


async def call_codex_app_server(
    codex_path: str,
    calls: tuple,
    expected_ids: tuple,
) -> Dict[int, Dict[str, Any]]:
    process = await asyncio.create_subprocess_exec(
        codex_path,
        "app-server",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdin and process.stdout
    requests = [
        {
            "method": "initialize",
            "id": 1,
            "params": {
                "clientInfo": {
                    "name": "pagermaid-gpt-usage",
                    "title": "PagerMaid GPT Usage",
                    "version": "1.0.0",
                }
            },
        },
        {"method": "initialized", "params": {}},
        *calls,
    ]
    payload = "\n".join(json.dumps(item, separators=(",", ":")) for item in requests)
    process.stdin.write((payload + "\n").encode())
    await process.stdin.drain()

    responses: Dict[int, Dict[str, Any]] = {}
    try:
        deadline = asyncio.get_running_loop().time() + RPC_TIMEOUT
        while not all(response_id in responses for response_id in expected_ids):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            line = await asyncio.wait_for(process.stdout.readline(), timeout=remaining)
            if not line:
                break
            try:
                item = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            response_id = item.get("id")
            if response_id in expected_ids:
                responses[int(response_id)] = item
    except asyncio.TimeoutError:
        pass
    finally:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout=2)

    for response_id in expected_ids:
        response = responses.get(response_id)
        if not response:
            raise RuntimeError("Codex CLI 返回超时。")
        if response.get("error"):
            error = response["error"]
            raise RuntimeError(str(error.get("message") if isinstance(error, dict) else error))

    return responses


async def read_codex_account(codex_path: str) -> Dict[str, Any]:
    responses = await call_codex_app_server(
        codex_path,
        (
            {"method": "account/read", "id": 2, "params": {}},
            {"method": "account/rateLimits/read", "id": 3, "params": {}},
        ),
        (1, 2, 3),
    )

    limits_result = responses[3].get("result", {})
    return {
        "account": responses[2].get("result", {}).get("account"),
        "limits": limits_result.get("rateLimits"),
        "reset_credits": limits_result.get("rateLimitResetCredits"),
    }


async def consume_reset_credit(codex_path: str) -> str:
    responses = await call_codex_app_server(
        codex_path,
        (
            {
                "method": "account/rateLimitResetCredit/consume",
                "id": 2,
                "params": {"idempotencyKey": str(uuid.uuid4())},
            },
        ),
        (1, 2),
    )
    return str(responses[2].get("result", {}).get("outcome") or "unknown")


def parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            if value > 10_000_000_000:
                value /= 1000
            return datetime.fromtimestamp(value, TIME_ZONE)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(TIME_ZONE)
    except (TypeError, ValueError, OSError):
        return None


def format_time(value: Any) -> str:
    parsed = parse_datetime(value)
    return parsed.strftime("%Y-%m-%d %H:%M") if parsed else "未知"


def format_window(title: str, window: Optional[Dict[str, Any]]) -> str:
    if not window:
        return f"{title}：未返回"
    used = max(0, min(100, int(round(float(window.get("usedPercent", 0))))))
    remaining = 100 - used
    return (
        f"{title}\n"
        f"已用：{used}%　剩余：{remaining}%\n"
        f"重置：{format_time(window.get('resetsAt'))}"
    )


def format_reset_credits(data: Optional[Dict[str, Any]]) -> str:
    data = data or {}
    credits = [
        item
        for item in data.get("credits", [])
        if isinstance(item, dict) and item.get("status") == "available"
    ]
    available = int(data.get("availableCount", len(credits)) or 0)
    lines = ["额度重置卡", f"可用：{available} 次"]

    if available and credits:
        credit = credits[0]
        reset_type = {
            "codexRateLimits": "完全重置",
        }.get(str(credit.get("resetType") or ""), str(credit.get("title") or "未知"))
        lines.extend(
            [
                f"类型：{reset_type}",
                f"到期：{format_time(credit.get('expiresAt'))}",
                "重置指令：codex reset",
            ]
        )

    return "\n".join(lines)


def format_result(data: Dict[str, Any]) -> str:
    limits = data.get("limits") or {}
    quota = [
        "Codex 使用额度",
        format_window("5 小时使用限额", limits.get("primary")),
        format_window("每周使用限额", limits.get("secondary")),
    ]
    return "\n\n".join(quota + [format_reset_credits(data.get("reset_credits"))])


HELP_HTML = """<b>Codex 用量查询</b>

<code>,gpt</code>
查询 5 小时额度、每周额度和可用额度重置

<code>,gpt help</code>
显示本菜单

<code>,gpt set path /usr/local/bin/codex</code>
Codex 无法自动识别时，手动设置程序路径

<code>,gpt set path clear</code>
清除手动设置的程序路径

<code>codex reset</code>
准备使用额度重置卡；再次确认后才会真正重置

<b>使用条件</b>
PagerMaid 所在服务器需要安装 Codex CLI，并使用 ChatGPT 账号完成登录。插件不会保存或显示账号 Token。"""


@listener(
    is_plugin=True,
    outgoing=True,
    command="gpt",
    description="查询 Codex 使用额度与可用重置。",
    parameters="<help|set path>",
)
async def gpt_usage(message: Message) -> None:
    args = list(message.parameter or [])
    action = args[0].lower() if args else ""

    if action == "help":
        await edit_html(message, HELP_HTML)
        return

    if action == "set":
        if len(args) < 3 or args[1].lower() != "path":
            await edit_plain(message, "格式：,gpt set path /usr/local/bin/codex")
            return
        value = " ".join(args[2:]).strip()
        if value.lower() == "clear":
            save_config({"codex_path": ""})
            await edit_plain(message, "✅ 已清除 Codex 路径，将恢复自动识别。")
            return
        candidate = Path(value).expanduser()
        if not candidate.is_file():
            await edit_plain(message, "找不到该文件，请检查 Codex 路径。")
            return
        save_config({"codex_path": str(candidate)})
        await edit_plain(message, f"✅ Codex 路径已设置：{candidate}")
        return

    if action:
        await edit_plain(message, "未知命令，请使用 ,gpt help 查看帮助。")
        return

    codex_path = find_codex()
    if not codex_path:
        await edit_plain(
            message,
            "没有找到 Codex CLI。请先在 PagerMaid 服务器安装并登录 Codex，"
            "或使用 ,gpt set path 设置程序路径。",
        )
        return

    await edit_plain(message, "正在读取 Codex 使用额度……")
    try:
        result = await read_codex_account(codex_path)
        if not result.get("account"):
            await edit_plain(message, "Codex CLI 尚未登录 ChatGPT 账号，请先执行 codex login。")
            return
        await edit_plain(message, format_result(result))
    except FileNotFoundError:
        await edit_plain(message, "Codex 程序不存在，请使用 ,gpt set path 重新设置。")
    except PermissionError:
        await edit_plain(message, "PagerMaid 没有执行 Codex CLI 的权限。")
    except Exception as exc:
        await edit_plain(message, f"查询失败：{exc}")


@listener(
    is_plugin=True,
    outgoing=True,
    pattern=r"^(?:,|，)?codex\s+reset(?:\s+confirm)?\s*$",
    ignore_edited=True,
)
async def codex_reset(message: Message) -> None:
    text = str(getattr(message, "text", "") or "").strip()
    confirmed = bool(re.fullmatch(r"(?:,|，)?codex\s+reset\s+confirm", text, re.I))
    codex_path = find_codex()

    if not codex_path:
        await edit_plain(message, "没有找到 Codex CLI，请使用 ,gpt set path 设置程序路径。")
        return

    try:
        if not confirmed:
            data = await read_codex_account(codex_path)
            reset_data = data.get("reset_credits") or {}
            available = int(reset_data.get("availableCount", 0) or 0)
            if available <= 0:
                await edit_plain(message, "当前没有可用的额度重置卡。")
                await delete_after(message)
                return
            await edit_plain(
                message,
                "确认使用 1 次完全重置？\n\n确认指令：codex reset confirm",
            )
            return

        await edit_plain(message, "正在重置 Codex 使用额度……")
        outcome = await consume_reset_credit(codex_path)
        result_text = {
            "reset": "额度重置成功。",
            "nothingToReset": "当前额度无需重置，重置卡未消耗。",
            "noCredit": "当前没有可用的额度重置卡。",
            "alreadyRedeemed": "本次重置已经完成，请勿重复操作。",
        }.get(outcome, f"额度重置返回未知状态：{outcome}")
        await edit_plain(message, result_text)
        await delete_after(message)
    except FileNotFoundError:
        await edit_plain(message, "Codex 程序不存在，请使用 ,gpt set path 重新设置。")
    except PermissionError:
        await edit_plain(message, "PagerMaid 没有执行 Codex CLI 的权限。")
    except Exception as exc:
        await edit_plain(message, f"重置失败：{exc}")
