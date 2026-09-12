# -*- coding: utf-8 -*-
"""PagerMaid-Pyro 插件列表：列出已安装脚本及其功能说明。"""

import ast
import html
import json
import re
from pathlib import Path
from typing import Iterable, List, Optional

from pagermaid.enums import Client, Message
from pagermaid.listener import listener
from pagermaid.static import working_dir
from pyrogram import enums


PLUGINS_DIR = Path(working_dir) / "plugins"
NOTES_PATH = Path(working_dir) / "data" / "cj_notes.json"
MAX_MESSAGE_LENGTH = 3900
DESCRIPTION_OVERRIDES = {
    "autochangename.py": "账号名字加时间和彩云天气",
    "dc.py": "账号DC查询",
    "dme.py": "一键清除消息",
    "fanyi.py": "DeepL 翻译",
    "ip.py": "查询 IP 和路由",
    "lianjie.py": "节点或文件转订阅链接",
    "LL.py": "TG 聊天窗口使用测速 BOT",
    "pm.py": "安装、卸载、刷新、上传和备份插件",
    "pmcaptcha.py": "陌生人私聊验证码",
    "zt.py": "机器人状态查询",
}


def load_notes() -> dict:
    if not NOTES_PATH.is_file():
        return {}
    try:
        data = json.loads(NOTES_PATH.read_text("utf-8"))
        return {
            str(name): str(value)
            for name, value in data.items()
            if isinstance(name, str) and isinstance(value, str) and value.strip()
        } if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_notes() -> None:
    NOTES_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = NOTES_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(MANUAL_NOTES, ensure_ascii=False, indent=2), "utf-8"
    )
    temporary.replace(NOTES_PATH)


MANUAL_NOTES = load_notes()


def clean_description(value: object) -> str:
    """将源码中的说明整理成适合 Telegram 单行显示的文字。"""
    text = re.sub(r"<[^>]+>", "", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip(" .。；;")
    for prefix in ("PagerMaid-Pyro:", "PagerMaid-Pyro：", "PagerMaid:", "PagerMaid："):
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix):].strip()
            break
    if not text:
        return "暂无功能说明"
    first = re.split(r"(?<=[。！？!?])\s+|\n", text, maxsplit=1)[0].strip()
    if len(first) > 80:
        first = first[:77].rstrip() + "…"
    return first


def literal_string(node: ast.AST) -> Optional[str]:
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return None
    return value if isinstance(value, str) else None


def listener_descriptions(tree: ast.Module) -> List[str]:
    descriptions: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            function = decorator.func
            is_listener = (
                isinstance(function, ast.Name) and function.id == "listener"
            ) or (
                isinstance(function, ast.Attribute) and function.attr == "listener"
            )
            if not is_listener:
                continue
            for keyword in decorator.keywords:
                if keyword.arg == "description":
                    value = literal_string(keyword.value)
                    if value:
                        description = clean_description(value)
                        if description not in descriptions:
                            descriptions.append(description)
    return descriptions


def describe_plugin(path: Path) -> str:
    """静态读取插件说明，不导入或执行插件代码。"""
    try:
        source = path.read_text("utf-8-sig")
        tree = ast.parse(source, filename=str(path))
    except (OSError, UnicodeError, SyntaxError):
        return "无法读取插件说明"

    descriptions = listener_descriptions(tree)
    if descriptions:
        return clean_description("；".join(descriptions))

    docstring = ast.get_docstring(tree, clean=True)
    if docstring:
        return clean_description(docstring)
    return "暂无功能说明"


def plugin_files(directory: Path) -> List[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        (
            path
            for path in directory.glob("*.py")
            if path.is_file() and path.name != "__init__.py"
        ),
        key=lambda path: path.name.casefold(),
    )


def resolve_plugin(value: str) -> Optional[Path]:
    name = Path(value.strip()).name
    if not name or name != value.strip() or any(mark in value for mark in ("/", "\\")):
        return None
    wanted = name.casefold()
    if not wanted.endswith(".py"):
        wanted += ".py"
    return next(
        (path for path in plugin_files(PLUGINS_DIR) if path.name.casefold() == wanted),
        None,
    )


def effective_description(path: Path) -> str:
    if path.name in MANUAL_NOTES:
        return MANUAL_NOTES[path.name]
    if path.name in DESCRIPTION_OVERRIDES:
        return DESCRIPTION_OVERRIDES[path.name]
    return describe_plugin(path)


def format_entries(paths: Iterable[Path]) -> List[str]:
    return [
        f"{index}. <code>{html.escape(path.name)}</code>\n"
        f"　{html.escape(effective_description(path))}"
        for index, path in enumerate(paths, 1)
    ]


def split_messages(entries: List[str]) -> List[str]:
    title = "<b>YanyuBot - 插件列表</b>"
    if not entries:
        return [title + "\n\n<blockquote>没有找到插件脚本。</blockquote>"]

    pages: List[str] = []
    remaining = list(entries)
    page_number = 1
    while remaining:
        chunk: List[str] = []

        def build_page(items: List[str]) -> str:
            body = "\n\n".join(items)
            heading = title if page_number == 1 else f"<b>YanyuBot - 插件列表（续 {page_number}）</b>"
            return f"{heading}\n\n<blockquote>{body}</blockquote>"

        while remaining and len(build_page(chunk + [remaining[0]])) <= MAX_MESSAGE_LENGTH:
            chunk.append(remaining.pop(0))
        if not chunk:
            chunk.append(remaining.pop(0))
        pages.append(build_page(chunk))
        page_number += 1
    return pages


async def edit_result(message: Message, text: str) -> None:
    await message.edit(
        text,
        parse_mode=enums.ParseMode.HTML,
        disable_web_page_preview=True,
    )


def notes_menu() -> str:
    return (
        "<b>YanyuBot - 插件备注管理</b>\n\n"
        "<code>,cj set 插件名 新备注</code>　设置备注"
    )


@listener(
    is_plugin=True,
    outgoing=True,
    command="cj",
    description="列出已安装的 PagerMaid 插件及功能说明",
    parameters="<menu/set>",
    priority=1,
)
async def plugin_list(client: Client, message: Message):
    try:
        argument = (getattr(message, "arguments", None) or "").strip()
        parts = argument.split(maxsplit=2)
        action = parts[0].lower() if parts else ""

        if action in {"menu", "help", "备注"}:
            return await edit_result(message, notes_menu())
        if action == "set":
            if len(parts) < 3:
                return await edit_result(message, notes_menu())
            path = resolve_plugin(parts[1])
            if path is None:
                return await edit_result(message, "没有找到这个插件。")
            note = clean_description(parts[2])
            if note == "暂无功能说明":
                return await edit_result(message, "备注内容不能为空。")
            MANUAL_NOTES[path.name] = note
            save_notes()
            return await edit_result(
                message,
                f"已更新 <code>{html.escape(path.name)}</code> 的备注：\n\n"
                f"<blockquote>{html.escape(note)}</blockquote>",
            )
        if action:
            return await edit_result(message, notes_menu())

        pages = split_messages(format_entries(plugin_files(PLUGINS_DIR)))
        await edit_result(message, pages[0])
        for page in pages[1:]:
            await client.send_message(
                message.chat.id,
                page,
                parse_mode=enums.ParseMode.HTML,
                disable_web_page_preview=True,
            )
    except Exception as exc:
        await edit_result(message, "读取插件列表失败：" + html.escape(str(exc)))
