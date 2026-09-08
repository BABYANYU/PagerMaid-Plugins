# -*- coding: utf-8 -*-
import asyncio
import html
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime

from pagermaid.listener import listener
from pagermaid.services import client as http_client
from pyrogram.types import Message


REMOTE_MAPPINGS_URL = "https://raw.githubusercontent.com/Hyy800/Quantumult-X/refs/heads/Nana/ymys.txt"
REMOTE_CONFIG_MAPPINGS = {}


async def install_missing_packages():
    missing_packages = []

    try:
        import urllib3  # noqa: F401
    except ImportError:
        missing_packages.append("urllib3")

    try:
        import requests  # noqa: F401
    except ImportError:
        missing_packages.append("requests")

    if missing_packages:
        for package in missing_packages:
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", package])
                print(f"✅ 已自动安装: {package}")
            except Exception as exc:
                print(f"❌ 安装 {package} 失败: {exc}")


async def load_remote_mappings():
    global REMOTE_CONFIG_MAPPINGS
    try:
        response = await http_client.get(REMOTE_MAPPINGS_URL, timeout=10)
        response.raise_for_status()

        mappings = {}
        for line in response.text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                mappings[key.strip()] = value.strip()

        REMOTE_CONFIG_MAPPINGS = mappings
        return len(REMOTE_CONFIG_MAPPINGS)
    except Exception as exc:
        print(f"⚠️ 加载远程映射失败: {exc}")
        return 0


def get_config_name_from_mappings(url):
    for key, name in REMOTE_CONFIG_MAPPINGS.items():
        if key in url:
            return name
    return None


def get_config_name_from_header(content_disposition):
    if not content_disposition:
        return None

    try:
        parts = content_disposition.split(";")

        for part in parts:
            part = part.strip()
            if part.startswith("filename*="):
                name_part = part.split("''", 1)[-1]
                if name_part:
                    return urllib.parse.unquote(name_part)

        for part in parts:
            part = part.strip()
            if part.startswith("filename="):
                name_part = part.split("=", 1)[-1].strip("\"'")
                if not name_part:
                    continue
                try:
                    repaired_name = name_part.encode("iso-8859-1").decode("utf-8")
                    unquoted_name = urllib.parse.unquote(repaired_name, encoding="utf-8")
                    return unquoted_name if unquoted_name != repaired_name else repaired_name
                except UnicodeError:
                    return urllib.parse.unquote(name_part, encoding="utf-8")
    except Exception:
        pass

    return None


def format_bytes(size):
    if not isinstance(size, (int, float)) or size < 0:
        return "0 B"
    power = 1024
    n = 0
    power_labels = {0: "B", 1: "KB", 2: "MB", 3: "GB", 4: "TB"}
    while size >= power and n < len(power_labels) - 1:
        size /= power
        n += 1
    return f"{size:.2f} {power_labels[n]}"


def extract_message_content(message):
    """提取可见文字、caption、文字超链接和网页预览链接。"""
    if not message:
        return ""

    values = []
    text = getattr(message, "text", None)
    caption = getattr(message, "caption", None)
    if text:
        values.append(text)
    if caption:
        values.append(caption)

    for attribute in ("entities", "caption_entities"):
        for entity in getattr(message, attribute, None) or []:
            entity_url = getattr(entity, "url", None)
            if entity_url:
                values.append(entity_url)

    web_page = getattr(message, "web_page", None)
    web_page_url = getattr(web_page, "url", None) if web_page else None
    if web_page_url:
        values.append(web_page_url)

    return " ".join(values)


async def get_replied_message(pyro_client, message):
    """优先使用已加载的回复消息；缺失时按消息 ID 主动获取。"""
    reply = getattr(message, "reply_to_message", None)
    if reply:
        return reply

    reply_id = getattr(message, "reply_to_message_id", None)
    if not reply_id:
        return None

    try:
        return await pyro_client.get_messages(message.chat.id, reply_id)
    except Exception as exc:
        print(f"⚠️ 获取被回复消息失败: {exc}")
        return None


def extract_command_arguments(message):
    arguments = getattr(message, "arguments", None)
    if arguments is not None:
        return arguments.strip()

    command_text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    parts = command_text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def extract_urls(source_text):
    urls = re.findall(r"https?://[^\s<>]+", source_text or "", flags=re.IGNORECASE)
    # 去掉聊天文本中常见的句末标点，不破坏 URL 内的查询参数。
    return [url.rstrip(".,;:!?，。；：！？)]}）】》\"'") for url in urls]


async def process_single_url(url):
    config_name = get_config_name_from_mappings(url)

    try:
        res = await http_client.get(
            url,
            headers={"User-Agent": "FlClash/v0.8.76 clash-verge Platform/android"},
            timeout=15,
            follow_redirects=True,
        )
        res.raise_for_status()

        if not config_name:
            config_name = get_config_name_from_header(res.headers.get("content-disposition"))

        user_info_header = res.headers.get("subscription-userinfo")
        if not user_info_header:
            return {
                "status": "失败",
                "url": url,
                "config_name": config_name,
                "data": None,
                "error": "响应中没有 subscription-userinfo 请求头",
            }

        parts = {
            key.strip().lower(): value.strip()
            for key, value in (
                item.split("=", 1)
                for item in user_info_header.split(";")
                if "=" in item
            )
        }
        upload = int(parts.get("upload", 0))
        download = int(parts.get("download", 0))
        total = int(parts.get("total", 0))
        used = upload + download
        remain = max(total - used, 0)

        expire_ts_str = parts.get("expire")
        is_expired = bool(
            expire_ts_str
            and expire_ts_str.isdigit()
            and time.time() > int(expire_ts_str)
        )
        is_exhausted = total > 0 and remain <= 0

        if is_expired:
            status = "过期"
        elif is_exhausted:
            status = "耗尽"
        else:
            status = "有效"

        data = {
            "used": used,
            "total": total,
            "remain": remain,
            "expire_ts_str": expire_ts_str,
            "percentage": (used / total * 100) if total > 0 else 0,
        }
        return {
            "status": status,
            "url": url,
            "config_name": config_name,
            "data": data,
        }
    except Exception as exc:
        return {
            "status": "失败",
            "url": url,
            "config_name": config_name,
            "data": None,
            "error": str(exc),
        }


@listener(
    is_plugin=True,
    outgoing=True,
    command="c",
    description="查询订阅链接信息",
    parameters=",c <url> 或回复包含链接的消息后发送 ,c",
    ignore_forwarded=False,
    ignore_reacted=False,
    priority=1,
)
async def sub_c_enhanced(pyro_client, message: Message):
    try:
        await install_missing_packages()

        source_parts = []
        replied_message = await get_replied_message(pyro_client, message)
        reply_content = extract_message_content(replied_message)
        if reply_content:
            source_parts.append(reply_content)

        command_arguments = extract_command_arguments(message)
        if command_arguments:
            source_parts.append(command_arguments)

        source_text = " ".join(source_parts).strip()
        if not source_text:
            await message.edit("`请提供链接或回复一条包含链接的消息。`")
            return

        unique_urls = list(dict.fromkeys(extract_urls(source_text)))
        if not unique_urls:
            await message.edit("`未在输入或被回复消息中找到有效链接。`")
            return

        await message.edit("`正在查询...`")
        await load_remote_mappings()

        results = await asyncio.gather(
            *(process_single_url(url) for url in unique_urls),
            return_exceptions=True,
        )

        valid_results = []
        errors = []
        stats = {"有效": 0, "耗尽": 0, "过期": 0, "失败": 0}

        for result in results:
            if isinstance(result, Exception):
                stats["失败"] += 1
                errors.append(str(result))
                continue

            status = result.get("status", "失败")
            stats[status if status in stats else "失败"] += 1
            if status != "有效":
                if result.get("error"):
                    errors.append(result["error"])
                continue

            url = result["url"]
            config_name = result["config_name"]
            data = result["data"]
            safe_url = html.escape(str(url))
            safe_config_name = html.escape(
                str(config_name or "未提供或无法获取")
            )

            expire_text = "长期有效"
            remaining_text = None

            expire_ts_str = data["expire_ts_str"]
            if expire_ts_str and expire_ts_str.isdigit():
                expire_ts = int(expire_ts_str)
                expire_dt = datetime.fromtimestamp(expire_ts)
                expire_text = f"{expire_dt:%Y-%m-%d %H:%M:%S}"

                delta = max(expire_ts - time.time(), 0)
                days, remainder = divmod(delta, 86400)
                hours, remainder = divmod(remainder, 3600)
                minutes, _ = divmod(remainder, 60)
                remaining_text = (
                    f"{int(days)}天{int(hours)}小时{int(minutes)}分钟"
                )

            output_lines = [
                f"机场订阅: <code>{safe_url}</code>",
                f"机场名称: {safe_config_name}",
                f"流量详情: {format_bytes(data['used'])} / {format_bytes(data['total'])}",
                f"剩余流量: {format_bytes(data['remain'])}",
                f"过期时间: {expire_text}",
            ]
            if remaining_text is not None:
                output_lines.append(f"剩余时间: {remaining_text}")

            valid_results.append("\n\n".join(output_lines))

        if valid_results:
            result_text = ("\n\n" + "=" * 30 + "\n\n").join(valid_results)
            if len(unique_urls) > 1:
                result_text += (
                    f"\n\n统计: ✅有效:{stats['有效']} | ⚠️耗尽:{stats['耗尽']} | "
                    f"过期:{stats['过期']} | ❌失败:{stats['失败']}"
                )

            try:
                await message.edit(result_text, parse_mode="HTML")
            except Exception:
                try:
                    from pyrogram import enums

                    await message.edit(result_text, parse_mode=enums.ParseMode.HTML)
                except Exception:
                    plain_text = (
                        result_text.replace("<code>", "`")
                        .replace("</code>", "`")
                        .replace("<blockquote>", "```\n")
                        .replace("</blockquote>", "\n```")
                    )
                    await message.edit(plain_text)
        else:
            stats_text = (
                f"统计: ✅有效:{stats['有效']} | ⚠️耗尽:{stats['耗尽']} | "
                f"过期:{stats['过期']} | ❌失败:{stats['失败']}"
            )
            error_text = f"\n\n错误: {errors[0]}" if errors else ""
            await message.edit(f"`未找到有效的订阅信息。`\n\n{stats_text}{error_text}")
    except Exception as exc:
        print(f"❌ c 插件发生错误: {exc}")
        try:
            await message.edit(f"`c 插件发生错误: {exc}`")
        except Exception:
            pass
