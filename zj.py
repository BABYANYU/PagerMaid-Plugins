# -*- coding: utf-8 -*-
"""PagerMaid-Pyro 当日群聊总结插件。

指令：,zj / ,zj help / ,zj key <API_KEY> / ,zj test
默认按北京时间读取当前聊天从当天 00:00 到现在的消息。
"""

import asyncio
import contextlib
import html
import io
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from pagermaid.enums import Client, Message
from pagermaid.listener import listener
from pagermaid.static import working_dir
from pyrogram import enums


CONFIG_PATH = Path(working_dir) / "data" / "zj.json"
MINIMAX_CONFIG_PATH = Path(working_dir) / "data" / "ai_minimax.json"
API_URL = "https://api.minimax.io/v1"
MODEL = "MiniMax-M3"
SUMMARY_TIMEZONE = "Asia/Shanghai"
REQUEST_TIMEOUT = 180
MAX_MESSAGES = 2000
MAX_INPUT_CHARS = 120000
MAX_MESSAGE_CHARS = 1500
MAX_OUTPUT_TOKENS = 3000
CARD_WIDTH = 1200
CARD_HEIGHT = 600
LOGGER = logging.getLogger(__name__)
BUSY = False


def load_api_key() -> str:
    """优先读取本插件配置，其次复用 minimax.py 已保存的 Key。"""
    for path in (CONFIG_PATH, MINIMAX_CONFIG_PATH):
        if not path.is_file():
            continue
        try:
            saved = json.loads(path.read_text("utf-8"))
        except Exception:
            LOGGER.warning("无法读取 MiniMax 配置：%s", path)
            continue
        key = saved.get("api_key") if isinstance(saved, dict) else ""
        if isinstance(key, str) and key.strip():
            return key.strip()
    return ""


def save_api_key(api_key: str) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"api_key": api_key}, ensure_ascii=False, indent=2),
        "utf-8",
    )
    temporary.replace(CONFIG_PATH)
    if os.name == "posix":
        with contextlib.suppress(OSError):
            CONFIG_PATH.chmod(0o600)


def command_arguments(message: Message) -> str:
    parameter = getattr(message, "parameter", None)
    if parameter:
        return " ".join(str(item) for item in parameter).strip()
    raw = (getattr(message, "text", None) or "").strip()
    parts = raw.split(maxsplit=1)
    return parts[1].strip() if len(parts) == 2 else ""


def chat_id_of(message: Message):
    chat = getattr(message, "chat", None)
    return getattr(chat, "id", None) or getattr(message, "chat_id", None)


def sender_name(message) -> str:
    user = getattr(message, "from_user", None)
    if user is not None:
        full_name = " ".join(
            value for value in (
                getattr(user, "first_name", None),
                getattr(user, "last_name", None),
            ) if value
        ).strip()
        return full_name or getattr(user, "username", None) or str(getattr(user, "id", "用户"))
    sender_chat = getattr(message, "sender_chat", None)
    if sender_chat is not None:
        return getattr(sender_chat, "title", None) or getattr(sender_chat, "username", None) or "频道"
    return "未知用户"


def visible_content(message) -> str:
    text = (
        getattr(message, "text", None)
        or getattr(message, "caption", None)
        or ""
    ).strip()
    if text:
        return re.sub(r"\s+", " ", text)[:MAX_MESSAGE_CHARS]

    document = getattr(message, "document", None)
    if document is not None:
        name = getattr(document, "file_name", None) or "文件"
        return "[文件：" + str(name)[:200] + "]"
    media_names = (
        ("photo", "[图片]"), ("video", "[视频]"),
        ("audio", "[音频]"), ("voice", "[语音]"),
        ("animation", "[动画]"), ("sticker", "[贴纸]"),
        ("poll", "[投票]"), ("location", "[位置]"),
    )
    for attr, label in media_names:
        if getattr(message, attr, None) is not None:
            return label
    return ""


def message_date(message) -> Optional[datetime]:
    value = getattr(message, "date", None)
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


async def collect_today_messages(
    client: Client,
    command_message: Message,
) -> Tuple[List[Dict[str, Any]], datetime, datetime, bool]:
    chat_id = chat_id_of(command_message)
    if chat_id is None:
        raise RuntimeError("无法取得当前聊天 ID")

    local_tz = ZoneInfo(SUMMARY_TIMEZONE)
    now_local = datetime.now(local_tz)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_local.astimezone(timezone.utc)
    command_id = getattr(command_message, "id", None)
    records: List[Dict[str, Any]] = []
    capped = False

    async for item in client.get_chat_history(chat_id):
        date = message_date(item)
        if date is None:
            continue
        if date.astimezone(timezone.utc) < start_utc:
            break
        if getattr(item, "id", None) == command_id:
            continue
        content = visible_content(item)
        if not content:
            continue
        # 避免把本插件的历史命令当作群聊内容总结。
        if re.match(r"^[,，./!]?zj(?:\s|$)", content, flags=re.I):
            continue
        records.append({
            "id": int(getattr(item, "id", 0)),
            "time": date.astimezone(local_tz).strftime("%H:%M"),
            "sender": sender_name(item),
            "text": content,
        })
        if len(records) >= MAX_MESSAGES:
            capped = True
            break

    records.reverse()
    return records, start_local, now_local, capped


def transcript_for_ai(records: List[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]], bool]:
    lines: List[str] = []
    kept: List[Dict[str, Any]] = []
    length = 0
    truncated = False
    # 优先保留最新消息；最后再恢复时间顺序。
    for item in reversed(records):
        line = f"[ID {item['id']}][{item['time']}][{item['sender']}] {item['text']}"
        if length + len(line) + 1 > MAX_INPUT_CHARS:
            truncated = True
            break
        lines.append(line)
        kept.append(item)
        length += len(line) + 1
    lines.reverse()
    kept.reverse()
    return "\n".join(lines), kept, truncated


def summary_prompt(transcript: str) -> str:
    return (
        "你是 Telegram 群聊总结助手。根据记录输出简洁、准确的中文总结。"
        "忽略寒暄、表情、机器人状态、广告和重复内容；不得把猜测写成事实。"
        "只返回一个 JSON 对象，不要 Markdown、代码块或额外说明。格式："
        '{"overview":"两三句话的整体概括","points":['
        '{"title":"中文短标题","card_title":"2至3个英文单词，最多18个字符",'
        '"text":"一至两句话"}]}。'
        "points 保留最重要的 3 至 6 条。不要输出来源、链接或消息 ID。"
        "card_title 仅用于图片卡片，必须只含简洁英文和数字，包含空格在内不得超过18个字符；"
        "使用正常英文大小写，不要全部大写，并正确保留 iPhone、iOS、5G 等写法。"
        "没有值得总结的内容时 points 返回空数组。\n\n聊天记录：\n"
        + transcript
    )


def response_text(data: dict) -> str:
    try:
        content = data["choices"][0]["message"].get("content", "")
    except (KeyError, IndexError, TypeError, AttributeError):
        content = ""
    if isinstance(content, list):
        content = "".join(
            str(item.get("text", "")) for item in content if isinstance(item, dict)
        )
    text = re.sub(r"<think>.*?</think>", "", str(content or ""), flags=re.S | re.I).strip()
    if text:
        return text
    error = data.get("error") if isinstance(data, dict) else None
    detail = error.get("message") if isinstance(error, dict) else ""
    raise RuntimeError(str(detail or "MiniMax 没有返回总结")[:500])


async def call_minimax(api_key: str, transcript: str) -> str:
    timeout = httpx.Timeout(REQUEST_TIMEOUT, connect=12)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as session:
        response = await session.post(
            API_URL + "/chat/completions",
            headers={
                "Authorization": "Bearer " + api_key,
                "Content-Type": "application/json",
            },
            json={
                "model": MODEL,
                "messages": [
                    {"role": "user", "content": summary_prompt(transcript)},
                ],
                "stream": False,
                "max_tokens": MAX_OUTPUT_TOKENS,
                "temperature": 0.3,
                "reasoning_split": True,
            },
        )
    if response.status_code >= 400:
        raise RuntimeError(f"MiniMax HTTP {response.status_code}：{response.text[:600]}")
    try:
        return response_text(response.json())
    except ValueError as exc:
        raise RuntimeError("MiniMax 返回的不是有效 JSON") from exc


def parse_summary(raw: str) -> dict:
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.I)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise RuntimeError("MiniMax 返回的总结格式不正确")
    try:
        data = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        raise RuntimeError("MiniMax 返回的总结 JSON 无法解析") from exc
    if not isinstance(data, dict):
        raise RuntimeError("MiniMax 返回的总结格式不正确")
    return data


def clean_output_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\[([^\]]+)\]\(https?://[^)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s*(?:消息)?来源\s*[:：]?\s*", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def clean_card_title(value: Any, index: int) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[^A-Za-z0-9+&./# -]", "", text)
    text = re.sub(r"\s+", " ", text).strip(" .-/")
    if text and text == text.upper():
        brand_case = {
            "IPHONE": "iPhone", "IPAD": "iPad", "IOS": "iOS",
            "MACOS": "macOS", "AI": "AI", "API": "API",
            "VPN": "VPN", "VPS": "VPS", "CPU": "CPU", "GPU": "GPU",
            "5G": "5G", "4G": "4G", "USB": "USB",
        }
        text = " ".join(
            brand_case.get(word, word.capitalize()) for word in text.split()
        )
    if len(text) > 18:
        words = text.split()
        while len(words) > 1 and len(" ".join(words)) > 18:
            words.pop()
        text = " ".join(words)
        if len(text) > 18:
            text = text[:18].rstrip(" .-/")
    return text or f"Topic {index}"


def render_summary(
    data: dict,
    records: List[Dict[str, Any]],
    start_local: datetime,
    now_local: datetime,
    limited: bool,
) -> str:
    overview = clean_output_text(
        data.get("overview") or "今日暂无可提炼的重点内容。", 180
    )
    points = data.get("points") if isinstance(data.get("points"), list) else []
    output = [
        "<b>今日群聊总结</b>",
        "",
        "<blockquote>",
        f"✦ <b>时段</b>：<code>{start_local.strftime('%H:%M')}—{now_local.strftime('%H:%M')}</code>",
        "",
        f"✦ <b>消息</b>：<code>{len(records)} 条</code>",
        "",
        "✦ <b>概览</b>：" + html.escape(overview),
    ]
    if points:
        for point in points[:6]:
            if not isinstance(point, dict):
                continue
            title = clean_output_text(point.get("title") or "重点", 20)
            text = clean_output_text(point.get("text"), 70)
            line = f"✦ <b>{html.escape(title)}</b>"
            if text:
                line += "：" + html.escape(text)
            output.extend(["", line])
    if limited:
        output.extend(["", "✦ <i>消息较多，本次优先分析较新的内容。</i>"])
    output.append("</blockquote>")
    return "\n".join(output)


def load_card_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    regular = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/msyh.ttc",
        "DejaVuSans.ttf",
    ]
    bold_names = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "C:/Windows/Fonts/msyhbd.ttc",
        "DejaVuSans-Bold.ttf",
    ]
    for name in bold_names if bold else regular:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def load_symbol_font(size: int) -> ImageFont.ImageFont:
    for name in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "C:/Windows/Fonts/seguisym.ttf",
        "DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return load_card_font(size)


def text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def fit_card_text(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> str:
    value = clean_output_text(text, 80)
    if text_width(draw, value, font) <= width:
        return value
    while value and text_width(draw, value + "…", font) > width:
        value = value[:-1]
    return value + "…" if value else ""


def fit_focus_title(draw, text: str, width: int, scale: int):
    """优先完整显示主题，只对较宽的标题逐级缩小字号。"""
    for size in range(23, 17, -1):
        font = load_card_font(size * scale, True)
        if text_width(draw, text, font) <= width:
            return text, font
    font = load_card_font(17 * scale, True)
    return fit_card_text(draw, text, font, width), font


def peak_hour(records: List[Dict[str, Any]]) -> str:
    counts: Dict[str, int] = {}
    for item in records:
        hour = str(item.get("time") or "")[:2]
        if hour.isdigit():
            counts[hour] = counts.get(hour, 0) + 1
    if not counts:
        return "--"
    hour = max(counts, key=lambda value: (counts[value], value))
    return f"{hour}:00"


def create_summary_card(
    data: dict,
    records: List[Dict[str, Any]],
    start_local: datetime,
    now_local: datetime,
) -> bytes:
    # 使用 2 倍分辨率直接绘制，避免 Telegram 缩放后文字和细线发虚。
    scale = 2
    width, height = CARD_WIDTH * scale, CARD_HEIGHT * scale
    sc = lambda value: int(round(value * scale))
    box = lambda values: tuple(sc(value) for value in values)
    image = Image.new("RGBA", (width, height), "#05070d")

    ambient = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ambient_draw = ImageDraw.Draw(ambient)
    ambient_draw.ellipse(box((760, -260, 1360, 330)), fill=(36, 211, 238, 42))
    ambient_draw.ellipse(box((-220, 320, 360, 820)), fill=(139, 92, 246, 34))
    ambient = ambient.filter(ImageFilter.GaussianBlur(sc(120)))
    image = Image.alpha_composite(image, ambient)
    draw = ImageDraw.Draw(image)

    white = "#f8fafc"
    muted = "#77849a"
    cyan = "#38d6ee"
    violet = "#a78bfa"
    blue = "#60a5fa"
    panel = "#090d15"
    line = "#1b2636"

    title_font = load_card_font(sc(58), True)
    subtitle_font = load_card_font(sc(18), True)
    label_font = load_card_font(sc(20), True)
    value_font = load_card_font(sc(46), True)
    symbol_font = load_symbol_font(sc(23))

    for x in range(0, width + 1, sc(60)):
        draw.line((x, sc(112), x, height), fill=(22, 34, 52, 65), width=sc(1))
    for y in range(sc(112), height + 1, sc(48)):
        draw.line((0, y, width, y), fill=(22, 34, 52, 65), width=sc(1))

    brand_left, brand_right = "Yanyu", "Bot"
    brand_x = sc(55)
    draw.text((brand_x, sc(16)), brand_left, font=title_font, fill=white)
    draw.text(
        (brand_x + text_width(draw, brand_left, title_font), sc(16)),
        brand_right,
        font=title_font,
        fill=cyan,
    )
    draw.text(
        (sc(57), sc(90)), "Daily Group Summary",
        font=subtitle_font, fill=muted,
    )
    draw.rounded_rectangle(
        box((44, 124, 1156, 552)), radius=sc(28),
        fill=panel, outline=line, width=sc(2)
    )

    points = data.get("points") if isinstance(data.get("points"), list) else []
    focus_titles = [
        clean_card_title(item.get("card_title"), index)
        for index, item in enumerate(points, 1)
        if isinstance(item, dict)
    ][:6]
    members = len({str(item.get("sender") or "") for item in records})
    main_items = [
        (78, "Messages", str(len(records)), cyan),
        (438, "Members", str(members), violet),
        (798, "Topics", str(len(focus_titles)), blue),
    ]
    for left, label, value, color in main_items:
        left = sc(left)
        draw.text((left, sc(158)), label, font=label_font, fill=muted)
        draw.text((left, sc(198)), value, font=value_font, fill=white)
        draw.rounded_rectangle(
            (left, sc(264), left + sc(290), sc(273)),
            radius=sc(5), fill="#172235"
        )
        draw.rounded_rectangle(
            (left, sc(264), left + sc(118), sc(273)),
            radius=sc(5), fill=color
        )

    draw.line(box((78, 315, 1122, 315)), fill=line, width=sc(2))
    draw.text((sc(78), sc(344)), "Today Topics", font=label_font, fill=cyan)
    peak_label = "Peak Hour  " + peak_hour(records)
    peak_x = sc(1122) - text_width(draw, peak_label, subtitle_font)
    draw.text(
        (peak_x, sc(344)), peak_label,
        font=subtitle_font, fill=muted,
    )

    if not focus_titles:
        focus_titles = ["No Key Topics"]
    chip_width, chip_height = sc(320), sc(52)
    chip_x = tuple(sc(value) for value in (78, 438, 798))
    chip_colors = (cyan, violet, blue, cyan, violet, blue)
    for index, title in enumerate(focus_titles):
        row, column = divmod(index, 3)
        x, y = chip_x[column], sc(384 + row * 66)
        color = chip_colors[index]
        draw.rounded_rectangle(
            (x, y, x + chip_width, y + chip_height),
            radius=sc(14), fill="#101827", outline=color, width=sc(2),
        )
        draw.text((x + sc(17), y + sc(12)), "✦", font=symbol_font, fill=color)
        fitted, fitted_font = fit_focus_title(
            draw, title, chip_width - sc(60), scale
        )
        draw.text(
            (x + sc(43), y + sc(12)), fitted,
            font=fitted_font, fill=white
        )

    period = f"{start_local.strftime('%H:%M')} — {now_local.strftime('%H:%M')}  ·  Beijing Time"
    draw.text((sc(78), sc(525)), period, font=subtitle_font, fill="#516075")
    draw.ellipse(box((1110, 525, 1122, 537)), fill="#34d399")

    output = io.BytesIO()
    image.convert("RGB").save(output, "PNG", optimize=True)
    return output.getvalue()


async def send_html(client: Client, chat_id, text: str):
    return await client.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=enums.ParseMode.HTML,
        disable_web_page_preview=True,
    )


def telegram_text_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


async def send_summary_result(
    client: Client,
    chat_id,
    data: dict,
    records: List[Dict[str, Any]],
    start_local: datetime,
    now_local: datetime,
    caption: str,
) -> None:
    # Telegram 图片说明上限为 1024；正常输出约 900，超限则安全回退文本。
    if telegram_text_length(caption) > 1024:
        await send_html(client, chat_id, caption)
        return
    try:
        content = await asyncio.to_thread(
            create_summary_card, data, records, start_local, now_local
        )
        photo = io.BytesIO(content)
        photo.name = "today-summary.png"
        try:
            await client.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=caption,
                parse_mode=enums.ParseMode.HTML,
            )
        finally:
            photo.close()
    except Exception:
        LOGGER.exception("群聊总结图片渲染或发送失败，回退文字结果")
        await send_html(client, chat_id, caption)


async def send_temporary_error(client: Client, chat_id, text: str) -> None:
    sent = await send_html(client, chat_id, html.escape(text))
    await asyncio.sleep(5)
    with contextlib.suppress(Exception):
        await sent.delete()


async def temporary_feedback(message: Message, text: str) -> None:
    edited = await message.edit(html.escape(text), parse_mode=enums.ParseMode.HTML)
    await asyncio.sleep(3)
    with contextlib.suppress(Exception):
        await edited.delete()


def help_text() -> str:
    key_status = "已设置" if load_api_key() else "未设置"
    return "\n".join([
        "<b>群聊总结设置</b>",
        "",
        "<blockquote>",
        "<b>模型：</b><code>MiniMax-M3</code>",
        "",
        "<b>API：</b><code>https://api.minimax.io/v1</code>",
        "",
        "<b>API Key：</b>" + key_status,
        "</blockquote>",
        "",
        "<b>API 设置</b>",
        "",
        "<code>,zj key API密钥</code>",
        "",
        "<code>,zj test</code>　测试接口",
    ])


async def test_api(api_key: str) -> None:
    timeout = httpx.Timeout(30, connect=12)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as session:
        response = await session.get(
            API_URL + "/models",
            headers={"Authorization": "Bearer " + api_key},
        )
    if response.status_code >= 400:
        raise RuntimeError(f"接口测试失败（HTTP {response.status_code}）")


@listener(
    is_plugin=True,
    command="zj",
    outgoing=True,
    description="MiniMax 当日群聊总结",
    parameters="<help/key/test>",
    priority=1,
    ignore_edited=True,
    ignore_forwarded=False,
    ignore_reacted=False,
)
async def zj_command(client: Client, message: Message):
    global BUSY
    argument = command_arguments(message)
    parts = argument.split(maxsplit=1)
    action = parts[0].lower() if parts else ""

    if action == "help":
        return await message.edit(help_text(), parse_mode=enums.ParseMode.HTML)
    if action == "key":
        if len(parts) != 2 or not parts[1].strip():
            return await temporary_feedback(message, "格式：,zj key API_KEY")
        save_api_key(parts[1].strip())
        return await temporary_feedback(message, "API Key 已保存")
    if action == "test":
        key = load_api_key()
        if not key:
            return await temporary_feedback(message, "请先设置 MiniMax API Key")
        try:
            await test_api(key)
            return await temporary_feedback(message, "MiniMax 接口正常")
        except Exception as exc:
            return await temporary_feedback(message, str(exc)[:500])
    if argument:
        return await message.edit(help_text(), parse_mode=enums.ParseMode.HTML)
    if BUSY:
        return await temporary_feedback(message, "上一条总结正在处理")

    BUSY = True
    chat_id = chat_id_of(message)
    if chat_id is None:
        BUSY = False
        return await temporary_feedback(message, "无法取得当前聊天 ID")
    try:
        safe_delete = getattr(message, "safe_delete", None)
        if callable(safe_delete):
            await safe_delete()
        else:
            await message.delete()
    except Exception:
        LOGGER.exception("删除 zj 指令失败")
    try:
        api_key = load_api_key()
        if not api_key:
            raise RuntimeError("请先发送 ,zj key API_KEY 设置 MiniMax Key")
        records, start_local, now_local, capped = await collect_today_messages(
            client, message
        )
        if not records:
            raise RuntimeError("今天 00:00 至现在没有可总结的消息")
        transcript, kept, truncated = transcript_for_ai(records)
        raw = await asyncio.wait_for(
            call_minimax(api_key, transcript), REQUEST_TIMEOUT + 5
        )
        data = parse_summary(raw)
        result = render_summary(
            data, kept, start_local, now_local, capped or truncated
        )
        await send_summary_result(
            client, chat_id, data, kept, start_local, now_local, result
        )
        return
    except asyncio.TimeoutError:
        error = "总结超时，请稍后重试"
    except Exception as exc:
        error = str(exc)[:800]
        LOGGER.warning("群聊总结失败：%s", type(exc).__name__)
    finally:
        BUSY = False

    await send_temporary_error(client, chat_id, error)
