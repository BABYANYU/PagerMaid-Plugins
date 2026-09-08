# -*- coding: utf-8 -*-
"""PagerMaid-Pyro 简易插件管理器。

指令：
,az
    回复一个 .py 文件
    安装插件；同名插件存在时自动覆盖
    安装完成后自动刷新

,xz 插件名
    卸载插件
    例如：,xz ai

,sx
    刷新全部插件

,sc
    回复某条命令结果，上传生成该结果的插件源码
    也可以使用 ,sc 命令名

,bf
    打包备份 plugins 目录中的全部 .py 插件，并发送到 Telegram 收藏夹
"""

import ast
import asyncio
import contextlib
import logging
import re
import shutil
import sys
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

from pyrogram import filters
from pyrogram.handlers import MessageHandler

from pagermaid.common.plugin import plugin_manager
from pagermaid.common.reload import reload_all
from pagermaid.enums import Client, Message
from pagermaid.enums.command import CommandHandler
from pagermaid.listener import listener
from pagermaid.services import bot, sqlite
from pagermaid.static import working_dir


LOGGER = logging.getLogger(__name__)
PLUGIN_DIR = Path(working_dir) / "plugins"

BUSY = False
AUTO_DELETE_SECONDS = 10
AUTO_DELETE_TASKS: Set[asyncio.Task] = set()

MAX_SOURCE_SIZE = 2 * 1024 * 1024
ORIGIN_TTL = 24 * 60 * 60
ORIGIN_LIMIT = 2000
ORIGIN_INDEX_KEY = "sc.origin.index"
COMMAND_PATTERN = re.compile(r"^[,，]([^\s]+)", re.IGNORECASE)

HELP_TEXT = (
    "插件管理\n\n"
    "安装或覆盖插件（回复py文件）：\n"
    "<code>,az</code>\n\n"
    "卸载插件（回复py文件）：\n"
    "<code>,xz 插件名</code>\n\n"
    "刷新全部插件：\n"
    "<code>,sx</code>\n\n"
    "回复命令结果上传对应插件：\n"
    "<code>,sc</code>\n\n"
    "按插件名上传对应插件：\n"
    "<code>,sc 命令名</code>\n\n"
    "备份全部插件到收藏夹：\n"
    "<code>,bf</code>"
)


def create_backup(zip_path: Path) -> tuple[int, int]:
    """将 plugins 目录中的全部 .py 文件压缩到 ZIP。"""
    if not PLUGIN_DIR.is_dir():
        raise RuntimeError(f"备份目录不存在：{PLUGIN_DIR}")

    py_files = sorted(path for path in PLUGIN_DIR.rglob("*.py") if path.is_file())
    if not py_files:
        raise RuntimeError(f"没有找到 .py 文件：{PLUGIN_DIR}")

    with zipfile.ZipFile(
        zip_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for file_path in py_files:
            archive.write(file_path, arcname=str(file_path.relative_to(PLUGIN_DIR)))

    return len(py_files), zip_path.stat().st_size


def format_size(size: int) -> str:
    """将字节数格式化为易读大小。"""
    value = float(size)
    units = ("B", "KB", "MB", "GB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{size} B"


def get_args(message: Message) -> list[str]:
    """兼容不同 PagerMaid-Pyro 版本取得命令参数。"""
    parameter = getattr(message, "parameter", None)
    if parameter:
        return [
            str(item).strip()
            for item in parameter
            if str(item).strip()
        ]

    text = (getattr(message, "text", None) or "").strip()
    if not text:
        return []

    parts = text.split()
    return parts[1:] if len(parts) > 1 else []


async def delete_message_later(message: Message) -> None:
    await asyncio.sleep(AUTO_DELETE_SECONDS)
    with contextlib.suppress(Exception):
        safe_delete = getattr(message, "safe_delete", None)
        if callable(safe_delete):
            await safe_delete()
        else:
            await message.delete()


async def temporary_response(message: Message, text: str):
    """编辑为结果提示，并在 10 秒后自动删除。"""
    edited = await message.edit(text)
    target = edited or message
    task = asyncio.create_task(delete_message_later(target))
    AUTO_DELETE_TASKS.add(task)
    task.add_done_callback(AUTO_DELETE_TASKS.discard)
    return edited


def clean_command(value: object) -> str:
    return str(value or "").strip().lower().lstrip(",，/")


def origin_key(chat_id: int, message_id: int) -> str:
    return f"sc.origin.{chat_id}.{message_id}"


def message_timestamp(message: Message) -> float:
    date = getattr(message, "date", None)
    with contextlib.suppress(AttributeError, TypeError, ValueError):
        return float(date.timestamp())
    return time.time()


def save_origin(message: Message, command: str) -> None:
    """持久记录原始命令，插件刷新后仍可根据结果反查源码。"""
    command = clean_command(command)
    if not command or command == "sc":
        return

    key = origin_key(message.chat.id, message.id)
    now = time.time()
    record = {
        "key": key,
        "command": command,
        "created_at": now,
        "message_at": message_timestamp(message),
    }

    try:
        sqlite[key] = record
        index = sqlite.get(ORIGIN_INDEX_KEY, [])
        if not isinstance(index, list):
            index = []

        index = [
            item
            for item in index
            if isinstance(item, dict) and item.get("key") != key
        ]
        index.append({"key": key, "created_at": now})

        kept: List[Dict[str, object]] = []
        for item in index:
            created_at = float(item.get("created_at", 0) or 0)
            old_key = str(item.get("key", ""))
            if old_key and now - created_at <= ORIGIN_TTL:
                kept.append(item)
            elif old_key:
                with contextlib.suppress(KeyError):
                    del sqlite[old_key]

        while len(kept) > ORIGIN_LIMIT:
            old = kept.pop(0)
            with contextlib.suppress(KeyError):
                del sqlite[str(old["key"])]

        sqlite[ORIGIN_INDEX_KEY] = kept
    except Exception as exc:
        LOGGER.warning(
            "[SC] 保存命令来源失败: %s: %s",
            type(exc).__name__,
            exc,
        )


async def record_outgoing_command(_, message: Message) -> None:
    content = str(
        getattr(message, "text", "")
        or getattr(message, "caption", "")
        or ""
    )
    match = COMMAND_PATTERN.match(content.strip())
    if match:
        save_origin(message, match.group(1))


# 在 PagerMaid 编辑命令消息以前保存原始指令。
bot.add_handler(
    MessageHandler(
        record_outgoing_command,
        filters.me
        & ~filters.via_bot
        & filters.regex(COMMAND_PATTERN),
    ),
    group=-100,
)


def command_from_reply(message: Message) -> Optional[str]:
    reply = getattr(message, "reply_to_message", None)
    if reply is None:
        return None

    key = origin_key(message.chat.id, reply.id)
    try:
        record = sqlite.get(key, None)
    except Exception:
        return None
    if not isinstance(record, dict):
        return None

    created_at = float(record.get("created_at", 0) or 0)
    if created_at <= 0 or time.time() - created_at > ORIGIN_TTL:
        with contextlib.suppress(KeyError):
            del sqlite[key]
        return None

    recorded_message_at = float(record.get("message_at", 0) or 0)
    current_message_at = message_timestamp(reply)
    if recorded_message_at and abs(recorded_message_at - current_message_at) > 5:
        return None

    command = clean_command(record.get("command", ""))
    return command or None


def decorator_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def commands_in_source(path: Path) -> Set[str]:
    try:
        if not path.is_file() or path.stat().st_size > MAX_SOURCE_SIZE:
            return set()
        source = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(source, filename=str(path))
    except (OSError, UnicodeError, SyntaxError):
        return set()

    commands: Set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            if decorator_name(decorator.func) not in {"listener", "sub_command"}:
                continue
            for keyword in decorator.keywords:
                if (
                    keyword.arg == "command"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    commands.add(clean_command(keyword.value.value))
    return commands


def find_loaded_command_file(
    command: str,
    source_directories: List[Path],
) -> Optional[Path]:
    """从已加载的命令处理器反查真实源码文件。"""
    resolved_directories = [directory.resolve() for directory in source_directories]
    for module_name, module in list(sys.modules.items()):
        if (
            not module_name.startswith(("plugins.", "pagermaid.modules."))
            or module is None
        ):
            continue

        module_file = getattr(module, "__file__", None)
        if not module_file:
            continue
        try:
            source_path = Path(module_file).resolve()
        except (OSError, ValueError):
            continue

        allowed = False
        for directory in resolved_directories:
            with contextlib.suppress(ValueError):
                source_path.relative_to(directory)
                allowed = True
                break
        if not allowed:
            continue

        for value in vars(module).values():
            if not isinstance(value, CommandHandler):
                continue
            registered = clean_command(getattr(value, "_pgp_command__", ""))
            if registered == command and source_path.is_file():
                return source_path
    return None


def plugin_source_directories() -> List[Path]:
    """返回本地插件目录和 PagerMaid 内置模块目录。"""
    directories = [PLUGIN_DIR.resolve()]
    pagermaid_package = sys.modules.get("pagermaid")
    package_file = getattr(pagermaid_package, "__file__", None)
    if package_file:
        directories.append(Path(package_file).resolve().parent / "modules")

    result: List[Path] = []
    for directory in directories:
        if directory.is_dir() and directory not in result:
            result.append(directory)
    return result


def find_plugin_file(command: str) -> Optional[Path]:
    source_directories = plugin_source_directories()
    if not source_directories:
        return None

    loaded_file = find_loaded_command_file(command, source_directories)
    if loaded_file is not None:
        return loaded_file

    candidates: List[Path] = []
    for directory in source_directories:
        candidates.extend(sorted(directory.glob("*.py")))
        candidates.extend(sorted(directory.glob("*.py.disabled")))
    for candidate in candidates:
        if command in commands_in_source(candidate):
            return candidate

    safe_name = command.replace("/", "").replace("\\", "")
    if safe_name and safe_name not in {".", ".."}:
        for filename in (f"{safe_name}.py", f"{safe_name}.py.disabled"):
            for directory in source_directories:
                candidate = directory / filename
                if candidate.is_file():
                    return candidate
    return None


def upload_name(path: Path) -> str:
    name = path.name
    return name[:-9] if name.endswith(".disabled") else name


def plugin_name_from_reply(message: Message) -> Optional[str]:
    """从回复的 .py 文件名中取得插件名。"""
    reply = getattr(message, "reply_to_message", None)
    document = getattr(reply, "document", None) if reply is not None else None
    file_name = str(getattr(document, "file_name", "") or "").strip()
    if not file_name:
        return None

    file_name = Path(file_name).name
    lower_name = file_name.lower()
    if lower_name.endswith(".py.disabled"):
        return file_name[:-12]
    if lower_name.endswith(".py"):
        return file_name[:-3]
    return None


async def install_from_reply(message: Message):
    """安装/覆盖回复消息中的 .py 插件。"""
    file_path = await plugin_manager.download_from_message(message)

    if not file_path:
        await temporary_response(
            message,
            "安装失败：请回复一个 .py 插件文件"
        )
        return

    source = Path(file_path)
    plugin_name = source.name

    if not plugin_name.lower().endswith(".py"):
        with contextlib.suppress(Exception):
            source.unlink()
        await temporary_response(message, "安装失败：文件必须是 .py 格式")
        return

    plugin_name = Path(plugin_name).name
    name = plugin_name[:-3]

    if not name:
        with contextlib.suppress(Exception):
            source.unlink()
        await temporary_response(message, "安装失败：插件文件名无效")
        return

    PLUGIN_DIR.mkdir(parents=True, exist_ok=True)

    destination = PLUGIN_DIR / plugin_name
    disabled_destination = PLUGIN_DIR / f"{plugin_name}.disabled"

    try:
        # 删除旧版本及禁用版本，确保新插件直接启用。
        with contextlib.suppress(FileNotFoundError):
            destination.unlink()

        with contextlib.suppress(FileNotFoundError):
            disabled_destination.unlink()

        # 清理远程插件版本记录，避免覆盖后的本地修改受旧记录影响。
        try:
            plugin_manager.load_local_plugins()
            if name in plugin_manager.version_map:
                plugin_manager.version_map.pop(name, None)
                plugin_manager.save_local_version_map()
        except Exception:
            LOGGER.exception(
                "清理插件版本记录失败：%s",
                name,
            )

        shutil.move(
            str(source),
            str(destination),
        )

        await reload_all()

        with contextlib.suppress(Exception):
            await temporary_response(
                message,
                f"安装成功 · 刷新成功：<code>{plugin_name}</code>"
            )

    finally:
        # 下载后的临时文件如果没有被 move，强制删除。
        with contextlib.suppress(Exception):
            if (
                source.exists()
                and source.resolve() != destination.resolve()
            ):
                source.unlink()


async def remove_plugin(message: Message, name: str):
    """卸载插件并刷新。"""
    name = name.strip()

    if name.lower().endswith(".py"):
        name = name[:-3]

    if (
        not name
        or "/" in name
        or "\\" in name
        or name in {".", ".."}
    ):
        await temporary_response(message, "卸载失败：插件名无效")
        return

    plugin_manager.load_local_plugins()

    removed = plugin_manager.remove_plugin(name)

    if not removed:
        # 兼容插件管理缓存异常，直接检查本地文件。
        normal = PLUGIN_DIR / f"{name}.py"
        disabled = PLUGIN_DIR / f"{name}.py.disabled"

        if not normal.exists() and not disabled.exists():
            await temporary_response(
                message,
                f"卸载失败：没有找到插件 <code>{name}</code>"
            )
            return

        with contextlib.suppress(FileNotFoundError):
            normal.unlink()

        with contextlib.suppress(FileNotFoundError):
            disabled.unlink()

        try:
            if name in plugin_manager.version_map:
                plugin_manager.version_map.pop(name, None)
                plugin_manager.save_local_version_map()
        except Exception:
            LOGGER.exception(
                "清理插件版本记录失败：%s",
                name,
            )

    await reload_all()

    with contextlib.suppress(Exception):
        await temporary_response(
            message,
            f"卸载成功 · 刷新成功：<code>{name}</code>"
        )


@listener(
    is_plugin=True,
    outgoing=True,
    command="pm",
    description="插件管理帮助",
    parameters="[help]",
    priority=1,
    ignore_edited=True,
    ignore_forwarded=False,
    ignore_reacted=False,
)
async def help_command(message: Message) -> None:
    args = get_args(message)
    if not args or (len(args) == 1 and args[0].lower() == "help"):
        return await temporary_response(message, HELP_TEXT)
    return await temporary_response(
        message,
        "格式：<code>,pm help</code>",
    )


@listener(
    is_plugin=True,
    command="bf",
    outgoing=True,
    description="备份 PagerMaid 插件",
    parameters="",
    priority=1,
    ignore_edited=True,
    ignore_forwarded=False,
    ignore_reacted=False,
)
async def backup_plugins(client: Client, message: Message):
    """压缩全部 .py 插件，并把备份发送到 Telegram 收藏夹。"""
    global BUSY

    if BUSY:
        return await temporary_response(message, "已有插件管理任务正在执行，请稍后再试")

    BUSY = True
    try:
        await message.edit("正在备份插件...")
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        zip_name = f"PagerMaid_plugins_{timestamp}.zip"

        with tempfile.TemporaryDirectory(prefix="pagermaid-bf-") as temp_dir:
            zip_path = Path(temp_dir) / zip_name
            file_count, zip_size = await asyncio.to_thread(create_backup, zip_path)
            caption = (
                "PagerMaid 插件备份\n\n"
                f"文件数量：{file_count}\n"
                f"压缩包大小：{format_size(zip_size)}\n"
                f"备份时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            await client.send_document(
                chat_id="me",
                document=str(zip_path),
                caption=caption,
            )

        with contextlib.suppress(Exception):
            safe_delete = getattr(message, "safe_delete", None)
            if callable(safe_delete):
                await safe_delete()
            else:
                await message.delete()

    except Exception as exc:
        LOGGER.exception("PagerMaid 插件备份失败")
        with contextlib.suppress(Exception):
            await temporary_response(message, "备份失败：" + str(exc)[:800])
    finally:
        BUSY = False


@listener(
    is_plugin=True,
    outgoing=True,
    command="sc",
    description="回复命令结果上传对应源码，也可指定命令名",
    parameters="[命令名]",
    priority=1,
    ignore_edited=True,
    ignore_forwarded=False,
    ignore_reacted=False,
)
async def send_command_source(message: Message) -> None:
    args = get_args(message)
    command = clean_command(args[0]) if args else ""
    if not command:
        command = command_from_reply(message) or ""
    if not command:
        await temporary_response(
            message,
            "没有找到这条结果对应的命令。请重新执行一次命令后回复结果发送 "
            "<code>,sc</code>；也可以使用 <code>,sc 命令名</code>。"
        )
        return

    source_path = await asyncio.to_thread(find_plugin_file, command)
    if source_path is None:
        await temporary_response(
            message,
            f"没有找到命令 <code>{command}</code> 对应的本地源码。"
        )
        return

    reply = getattr(message, "reply_to_message", None)
    kwargs: Dict[str, object] = {
        "file_name": upload_name(source_path),
    }
    if reply is not None:
        kwargs["reply_to_message_id"] = reply.id

    thread_id = getattr(message, "message_thread_id", None)
    if thread_id is not None:
        kwargs["message_thread_id"] = thread_id

    try:
        await bot.send_document(
            message.chat.id,
            str(source_path),
            **kwargs,
        )
        safe_delete = getattr(message, "safe_delete", None)
        if callable(safe_delete):
            await safe_delete()
        else:
            await message.delete()
    except Exception as exc:
        LOGGER.warning(
            "[SC] 上传插件失败: %s: %s",
            type(exc).__name__,
            exc,
        )
        await temporary_response(
            message,
            "上传插件失败，请查看 PagerMaid 日志。",
        )


@listener(
    is_plugin=True,
    command="az",
    outgoing=True,
    description="安装插件",
    parameters="",
    priority=1,
    ignore_edited=True,
    ignore_forwarded=False,
    ignore_reacted=False,
)
async def install_command(client: Client, message: Message):
    del client
    global BUSY

    if BUSY:
        return await temporary_response(
            message,
            "已有插件管理任务正在执行，请稍后再试"
        )

    BUSY = True

    try:
        await install_from_reply(message)

    except Exception as exc:
        LOGGER.exception("插件安装失败")
        with contextlib.suppress(Exception):
            await temporary_response(
                message,
                "安装失败："
                + str(exc)[:800]
            )

    finally:
        BUSY = False


@listener(
    is_plugin=True,
    command="xz",
    outgoing=True,
    description="卸载插件",
    parameters="<插件名>",
    priority=1,
    ignore_edited=True,
    ignore_forwarded=False,
    ignore_reacted=False,
)
async def uninstall_command(client: Client, message: Message):
    del client
    global BUSY

    if BUSY:
        return await temporary_response(
            message,
            "已有插件管理任务正在执行，请稍后再试"
        )

    args = get_args(message)

    if len(args) > 1:
        return await temporary_response(
            message,
            "格式：<code>,xz 插件名</code>；或回复 .py 文件发送 <code>,xz</code>"
        )

    name = args[0] if args else plugin_name_from_reply(message)
    if not name:
        return await temporary_response(
            message,
            "请输入插件名，或回复一个 .py 插件文件发送 <code>,xz</code>",
        )

    BUSY = True

    try:
        await remove_plugin(
            message,
            name,
        )

    except Exception as exc:
        LOGGER.exception("插件卸载失败")
        with contextlib.suppress(Exception):
            await temporary_response(
                message,
                "卸载失败："
                + str(exc)[:800]
            )

    finally:
        BUSY = False


@listener(
    is_plugin=True,
    command="sx",
    outgoing=True,
    description="刷新插件",
    parameters="",
    priority=1,
    ignore_edited=True,
    ignore_forwarded=False,
    ignore_reacted=False,
)
async def reload_command(client: Client, message: Message):
    del client
    global BUSY

    if BUSY:
        return await temporary_response(
            message,
            "已有插件管理任务正在执行，请稍后再试"
        )

    BUSY = True

    try:
        await message.edit(
            "正在刷新插件..."
        )

        await reload_all()

        with contextlib.suppress(Exception):
            await temporary_response(
                message,
                "插件刷新成功"
            )

    except Exception as exc:
        LOGGER.exception("插件刷新失败")
        with contextlib.suppress(Exception):
            await temporary_response(
                message,
                "刷新失败："
                + str(exc)[:800]
            )

    finally:
        BUSY = False
