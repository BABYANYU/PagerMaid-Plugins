"""PagerMaid-Pyro：监控 GitHub Releases，并通过 Telegram Bot API 推送。"""

import asyncio
import contextlib
import html
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from pyrogram.enums import ParseMode

from pagermaid.enums import Message
from pagermaid.listener import listener
from pagermaid.services import scheduler
from pagermaid.static import working_dir


CONFIG_PATH = Path(working_dir) / "data" / "github_release_monitor.json"
JOB_ID = "pagermaid_github_release_monitor"
DEFAULT_CHAT_ID = "5951575104"
DEFAULT_INTERVAL_MINUTES = 60
REQUEST_TIMEOUT = 20
MAX_RELEASES = 20
TIME_ZONE = ZoneInfo("Asia/Shanghai")
COMMAND_PATTERN = re.compile(
    r"^(?:,|，)git(?:\s+([\s\S]*))?$",
    re.IGNORECASE,
)
_monitor_lock = asyncio.Lock()


def default_config() -> Dict[str, Any]:
    return {
        "interval_minutes": DEFAULT_INTERVAL_MINUTES,
        "bot_token": "",
        "chat_id": DEFAULT_CHAT_ID,
        "github_token": "",
        "repositories": {},
        "last_check": "",
        "last_error": "",
    }


def load_config() -> Dict[str, Any]:
    config = default_config()
    try:
        saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            for key in (
                "interval_minutes", "bot_token", "chat_id",
                "github_token", "repositories", "last_check", "last_error",
            ):
                if key in saved:
                    config[key] = saved[key]
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass

    try:
        interval = int(config.get("interval_minutes", DEFAULT_INTERVAL_MINUTES))
    except (TypeError, ValueError):
        interval = DEFAULT_INTERVAL_MINUTES
    config["interval_minutes"] = max(5, min(interval, 1440))
    config["bot_token"] = str(config.get("bot_token") or "").strip()
    config["chat_id"] = str(config.get("chat_id") or DEFAULT_CHAT_ID).strip()
    config["github_token"] = str(config.get("github_token") or "").strip()
    config["last_check"] = str(config.get("last_check") or "")
    config["last_error"] = str(config.get("last_error") or "")

    repositories = config.get("repositories")
    if not isinstance(repositories, dict):
        repositories = {}
    normalized: Dict[str, Dict[str, Any]] = {}
    for repository, state in repositories.items():
        try:
            name = normalize_repository(str(repository))
        except ValueError:
            continue
        state = state if isinstance(state, dict) else {}
        seen_ids = state.get("seen_ids", [])
        if not isinstance(seen_ids, list):
            seen_ids = []
        normalized[name] = {
            "initialized": bool(state.get("initialized", False)),
            "seen_ids": [
                int(item) for item in seen_ids
                if str(item).isdigit()
            ][-100:],
        }
    config["repositories"] = normalized
    return config


def save_config(config: Dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with contextlib.suppress(OSError):
        os.chmod(temporary, 0o600)
    temporary.replace(CONFIG_PATH)
    with contextlib.suppress(OSError):
        os.chmod(CONFIG_PATH, 0o600)


def normalize_repository(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("项目地址不能为空")

    if "://" not in value:
        parts = value.strip("/").split("/")
    else:
        parsed = urlparse(value)
        if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
            raise ValueError("只支持 github.com 项目链接")
        parts = [part for part in parsed.path.split("/") if part]

    if len(parts) < 2:
        raise ValueError("项目格式应为 owner/repo 或 GitHub 项目链接")
    owner, repository = parts[0], parts[1]
    if repository.endswith(".git"):
        repository = repository[:-4]
    valid = re.compile(r"^[A-Za-z0-9_.-]+$")
    if not valid.fullmatch(owner) or not valid.fullmatch(repository):
        raise ValueError("GitHub 项目名称格式不正确")
    return f"{owner}/{repository}"


def command_arguments(message: Message) -> str:
    text = (getattr(message, "text", None) or "").strip()
    match = COMMAND_PATTERN.match(text)
    return (match.group(1) or "").strip() if match else ""


def split_action(arguments: str) -> Tuple[str, str]:
    if not arguments:
        return "", ""
    action, separator, remainder = arguments.partition(" ")
    return action.lower(), remainder.strip() if separator else ""


def mask_secret(value: str) -> str:
    if not value:
        return "未设置"
    return "已设置"


def github_headers(token: str = "") -> Dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "PagerMaid-GitHub-Release-Monitor/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def request_json(
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> Any:
    body = None
    request_headers = dict(headers or {})
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=request_headers)
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
            message = detail.get("message") or detail.get("description")
        except Exception:
            message = ""
        raise RuntimeError(
            f"HTTP {exc.code}" + (f"：{message}" if message else "")
        ) from exc
    except URLError as exc:
        raise RuntimeError(f"网络连接失败：{exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError("网络请求超时") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("接口返回的数据格式不正确") from exc


async def fetch_releases(repository: str, github_token: str = "") -> List[dict]:
    owner, name = repository.split("/", 1)
    url = (
        "https://api.github.com/repos/"
        f"{quote(owner, safe='')}/{quote(name, safe='')}/releases"
        f"?per_page={MAX_RELEASES}"
    )
    data = await asyncio.to_thread(
        request_json,
        url,
        headers=github_headers(github_token),
    )
    if not isinstance(data, list):
        raise RuntimeError("GitHub 没有返回 Release 列表")
    return [
        item for item in data
        if isinstance(item, dict) and not item.get("draft")
    ]


def format_time(value: str) -> str:
    if not value:
        return "未知"
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(TIME_ZONE).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return value[:16]


def clean_release_body(value: Any, limit: int = 1000) -> str:
    text = str(value or "").replace("\r", "").strip()
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"!\[[^]]*]\([^)]*\)", "", text)
    text = re.sub(r"\[([^]]+)]\(([^)]+)\)", r"\1：\2", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"[*_~`]", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def release_message(repository: str, release: dict) -> str:
    published = format_time(
        str(release.get("published_at") or release.get("created_at") or "")
    )
    lines = [
        "<b>✦ GitHub 更新</b>",
        "",
        f"<b>项目：</b>{html.escape(repository)}",
        f"<b>发布时间：</b>{html.escape(published)}",
    ]

    body = clean_release_body(release.get("body"))
    lines.extend([
        "",
        "<b>更新内容</b>",
        html.escape(body or "暂无更新说明"),
    ])

    release_url = str(release.get("html_url") or "")
    if release_url.startswith("https://"):
        lines.extend([
            "",
            f'<a href="{html.escape(release_url, quote=True)}">查看 Release</a>',
        ])
    return "\n".join(lines)


async def send_bot_message(config: Dict[str, Any], text: str) -> None:
    token = str(config.get("bot_token") or "")
    chat_id = str(config.get("chat_id") or "")
    if not token:
        raise RuntimeError("Telegram Bot Token 未设置")
    if not chat_id:
        raise RuntimeError("Telegram Chat ID 未设置")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = await asyncio.to_thread(
        request_json,
        url,
        payload={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    )
    if not isinstance(data, dict) or not data.get("ok"):
        detail = data.get("description") if isinstance(data, dict) else ""
        raise RuntimeError(str(detail or "Telegram Bot 推送失败"))


async def validate_bot_token(token: str) -> str:
    data = await asyncio.to_thread(
        request_json,
        f"https://api.telegram.org/bot{token}/getMe",
    )
    if not isinstance(data, dict) or not data.get("ok"):
        detail = data.get("description") if isinstance(data, dict) else ""
        raise RuntimeError(str(detail or "Bot Token 验证失败"))
    result = data.get("result") or {}
    return str(result.get("username") or result.get("first_name") or "Telegram Bot")


async def check_updates() -> Dict[str, int]:
    if _monitor_lock.locked():
        return {"checked": 0, "pushed": 0, "baselined": 0}
    async with _monitor_lock:
        config = load_config()
        summary = {"checked": 0, "pushed": 0, "baselined": 0}
        if not config["bot_token"]:
            return summary

        try:
            for repository, state in config["repositories"].items():
                releases = await fetch_releases(
                    repository,
                    config["github_token"],
                )
                summary["checked"] += 1
                current_ids = [
                    int(item["id"]) for item in releases
                    if str(item.get("id", "")).isdigit()
                ]

                if not state.get("initialized"):
                    state["initialized"] = True
                    state["seen_ids"] = current_ids[:100]
                    summary["baselined"] += 1
                    save_config(config)
                    continue

                seen = {int(item) for item in state.get("seen_ids", [])}
                new_releases = [
                    item for item in releases
                    if str(item.get("id", "")).isdigit()
                    and int(item["id"]) not in seen
                ]
                for release in reversed(new_releases):
                    await send_bot_message(
                        config,
                        release_message(repository, release),
                    )
                    release_id = int(release["id"])
                    seen.add(release_id)
                    state["seen_ids"] = list(seen)[-100:]
                    summary["pushed"] += 1
                    save_config(config)

            config["last_check"] = datetime.now(TIME_ZONE).isoformat()
            config["last_error"] = ""
            save_config(config)
            return summary
        except Exception as exc:
            config["last_check"] = datetime.now(TIME_ZONE).isoformat()
            config["last_error"] = str(exc)[:500]
            save_config(config)
            raise


async def scheduled_check() -> None:
    with contextlib.suppress(Exception):
        await check_updates()


def register_job(interval_minutes: Optional[int] = None) -> None:
    config = load_config()
    interval = int(interval_minutes or config["interval_minutes"])
    with contextlib.suppress(Exception):
        scheduler.remove_job(JOB_ID)
    scheduler.add_job(
        scheduled_check,
        "interval",
        minutes=interval,
        id=JOB_ID,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=300,
    )


def help_text(config: Dict[str, Any]) -> str:
    return (
        "<b>✦ GitHub 监控</b>\n\n"
        "<blockquote>"
        f"<b>检查间隔：</b>{config['interval_minutes']} 分钟\n"
        f"<b>监控项目：</b>{len(config['repositories'])} 个\n"
        f"<b>Bot Token：</b>{html.escape(mask_secret(config['bot_token']))}\n"
        f"<b>接收 ID：</b><code>{html.escape(config['chat_id'])}</code>"
        "</blockquote>\n\n"
        "<blockquote>"
        "<b>推送设置</b>　<code>,git token BotToken</code>\n\n"
        "<b>接收设置</b>　<code>,git chat ChatID</code>\n\n"
        "<b>添加项目</b>　<code>,git add 项目链接</code>\n\n"
        "<b>删除项目</b>　<code>,git del 项目编号</code>\n\n"
        "<b>检查间隔</b>　<code>,git interval 60</code>\n\n"
        "<b>监控列表</b>　<code>,git list</code>"
        "</blockquote>"
    )


async def edit_html(message: Message, text: str) -> None:
    await message.edit(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def delete_later(message: Message, seconds: int = 10) -> None:
    await asyncio.sleep(seconds)
    with contextlib.suppress(Exception):
        await message.delete()


def schedule_delete(message: Message, seconds: int = 10) -> None:
    asyncio.create_task(delete_later(message, seconds))


@listener(
    pattern=r"^(?:,|，)git(?:\s|$)[\s\S]*",
    priority=1,
    block_process=True,
    ignore_edited=True,
)
async def github_release_command(message: Message) -> None:
    arguments = command_arguments(message)
    action, value = split_action(arguments)
    config = load_config()

    if not action or action in {"help", "menu"}:
        await edit_html(message, help_text(config))
        return

    if action == "add":
        try:
            repository = normalize_repository(value)
            if repository in config["repositories"]:
                await message.edit("该项目已经在监控列表中。")
                return
            releases = await fetch_releases(repository, config["github_token"])
            config["repositories"][repository] = {
                "initialized": True,
                "seen_ids": [
                    int(item["id"]) for item in releases
                    if str(item.get("id", "")).isdigit()
                ][:100],
            }
            save_config(config)
            await message.edit(f"已监控：{repository}")
            schedule_delete(message)
        except Exception as exc:
            await message.edit(f"添加失败：{str(exc)[:500]}")
        return

    if action in {"del", "delete", "remove"}:
        if not value.isdigit():
            await message.edit("格式：,git del 项目编号；请先用 ,git list 查看编号。")
            schedule_delete(message)
            return
        repositories = list(config["repositories"])
        index = int(value)
        if index < 1 or index > len(repositories):
            await message.edit("项目编号不存在，请先用 ,git list 查看编号。")
            schedule_delete(message)
            return
        repository = repositories[index - 1]
        config["repositories"].pop(repository, None)
        save_config(config)
        await message.edit(f"已删除：{repository}")
        schedule_delete(message)
        return

    if action == "list":
        repositories = list(config["repositories"])
        if not repositories:
            await message.edit("当前没有监控项目。")
            schedule_delete(message)
            return
        lines = ["<b>✦ GitHub 监控项目</b>", "", "<blockquote>"]
        project_lines = [
            f'{index}. <a href="https://github.com/{html.escape(repository, quote=True)}">'
            f"{html.escape(repository)}</a>"
            for index, repository in enumerate(repositories, 1)
        ]
        lines.append("\n\n".join(project_lines))
        lines.append("</blockquote>")
        await edit_html(message, "\n".join(lines))
        schedule_delete(message)
        return

    if action == "token":
        if not value:
            await message.edit("格式：,git token BotToken")
            return
        if value.lower() == "clear":
            config["bot_token"] = ""
            save_config(config)
            await message.edit("Bot Token 已清除。")
            return
        if ":" not in value:
            await message.edit("Bot Token 格式不正确。")
            return
        try:
            bot_name = await validate_bot_token(value)
        except Exception as exc:
            await message.edit(f"Bot Token 验证失败：{str(exc)[:500]}")
            return
        config["bot_token"] = value
        save_config(config)
        await message.edit(f"Bot Token 设置成功：{bot_name}")
        schedule_delete(message)
        with contextlib.suppress(Exception):
            await check_updates()
        return

    if action == "chat":
        if not value or not (value.lstrip("-").isdigit() or value.startswith("@")):
            await message.edit("格式：,git chat ChatID；频道也可以填写 @用户名。")
            return
        config["chat_id"] = value
        save_config(config)
        await message.edit(f"接收位置已设置为：{value}")
        return

    if action == "interval":
        try:
            interval = int(value)
        except ValueError:
            await message.edit("格式：,git interval 分钟数")
            return
        if not 5 <= interval <= 1440:
            await message.edit("检查间隔必须在 5～1440 分钟之间。")
            return
        config["interval_minutes"] = interval
        save_config(config)
        register_job(interval)
        await message.edit(f"检查间隔已设置为 {interval} 分钟。")
        return

    if action == "status":
        await edit_html(message, help_text(config))
        return

    await message.edit("未知指令，请发送 ,git 查看菜单。")
    schedule_delete(message)


register_job()

