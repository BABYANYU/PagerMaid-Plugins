"""PagerMaid-Pyro plugin: compact runtime status.

Usage:
    ,zt
    ，zt
"""

import asyncio
import os
import time
import uuid
from pathlib import Path
from statistics import median
from typing import Any, Iterable, List, Optional, Tuple

import psutil
import pagermaid
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from pyrogram import enums, raw

from pagermaid.enums import Client, Message
from pagermaid.listener import listener
from pagermaid.static import working_dir
from pagermaid.utils import safe_remove


TITLE = "YanyuBot - 运行状态"
CONNECT_TIMEOUT = 3.0
PROBE_COUNT = 3
VALID_DC_IDS = {1, 2, 3, 4, 5}
STATUS_TEMP_DIR = Path(working_dir) / "data" / "status" / "temp"


def format_uptime(seconds: float) -> str:
    total_minutes = max(0, int(seconds)) // 60
    days, remainder = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remainder, 60)

    parts = []
    if days:
        parts.append(f"{days} 天")
    if days or hours:
        parts.append(f"{hours} 小时")
    parts.append(f"{minutes} 分")
    return " ".join(parts)


def count_python_plugins(plugin_dir: Path) -> int:
    if not plugin_dir.is_dir():
        return 0

    file_plugins = sum(
        1
        for path in plugin_dir.glob("*.py")
        if path.is_file() and path.name != "__init__.py"
    )
    directory_plugins = sum(
        1
        for path in plugin_dir.iterdir()
        if path.is_dir() and (path / "main.py").is_file()
    )
    return file_plugins + directory_plugins


def count_plugins() -> int:
    custom_dir = Path(__file__).resolve().parent
    custom_count = count_python_plugins(custom_dir)

    package_file = getattr(pagermaid, "__file__", None)
    if not package_file:
        return custom_count

    builtin_dir = Path(package_file).resolve().parent / "modules"
    return custom_count + count_python_plugins(builtin_dir)


async def read_account_dc(bot: Client) -> Optional[int]:
    try:
        value = bot.storage.dc_id()
        if hasattr(value, "__await__"):
            value = await value
        dc_id = int(value)
        return dc_id if dc_id in VALID_DC_IDS else None
    except (AttributeError, TypeError, ValueError, RuntimeError):
        return None


def collect_dc_endpoints(
    dc_options: Iterable[Any], dc_id: int
) -> List[Tuple[str, int]]:
    endpoints: List[Tuple[str, int]] = []

    for option in dc_options:
        if int(getattr(option, "id", 0) or 0) != dc_id:
            continue
        if getattr(option, "ipv6", False):
            continue
        if getattr(option, "media_only", False):
            continue
        if getattr(option, "cdn", False):
            continue

        host = str(getattr(option, "ip_address", "") or "").strip()
        port = int(getattr(option, "port", 443) or 443)
        endpoint = (host, port)

        if host and endpoint not in endpoints:
            endpoints.append(endpoint)

    return endpoints


async def probe_once(host: str, port: int) -> Optional[float]:
    loop = asyncio.get_running_loop()
    writer = None
    started = loop.time()

    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=CONNECT_TIMEOUT,
        )
        return (loop.time() - started) * 1000
    except (asyncio.TimeoutError, OSError):
        return None
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, RuntimeError):
                pass


async def probe_endpoint(host: str, port: int) -> Optional[int]:
    samples = []
    for _ in range(PROBE_COUNT):
        latency = await probe_once(host, port)
        if latency is not None:
            samples.append(latency)

    return max(1, round(median(samples))) if samples else None


async def measure_current_dc(bot: Client, dc_id: Optional[int]) -> Optional[int]:
    if dc_id is None:
        return None

    try:
        config = await bot.invoke(raw.functions.help.GetConfig())
        endpoints = collect_dc_endpoints(
            getattr(config, "dc_options", []), dc_id
        )
        if not endpoints:
            return None

        results = await asyncio.gather(
            *(probe_endpoint(host, port) for host, port in endpoints),
            return_exceptions=True,
        )
        values = [value for value in results if isinstance(value, int)]
        return min(values) if values else None
    except Exception:
        return None


def get_process_stats() -> Tuple[float, float, float]:
    process = psutil.Process(os.getpid())
    memory_mb = process.memory_info().rss / 1024 / 1024
    cpu_percent = process.cpu_percent(interval=1.0)
    uptime_seconds = time.time() - process.create_time()
    return memory_mb, cpu_percent, uptime_seconds


def build_result(
    memory_mb: float,
    cpu_percent: float,
    plugin_count: int,
    dc_id: Optional[int],
    latency_ms: Optional[int],
    uptime_seconds: float,
) -> str:
    dc_name = f"DC{dc_id}" if dc_id is not None else "未知"
    latency = f"{latency_ms} ms" if latency_ms is not None else "连接失败"

    status_lines = [
        f"✦ 进程内存：<code>{memory_mb:.0f} MB</code>",
        f"✦ 进程 CPU：<code>{cpu_percent:.2f}%</code>",
        f"✦ 插件数量：<code>{plugin_count} 个</code>",
        f"✦ 数据中心：<code>{dc_name} · {latency}</code>",
        f"✦ 运行时间：<code>{format_uptime(uptime_seconds)}</code>",
    ]
    status_block = "\n\n".join(status_lines)
    return f"<b>{TITLE}</b>\n\n<blockquote>{status_block}</blockquote>"


def format_card_uptime(seconds: float) -> str:
    total_minutes = max(0, int(seconds)) // 60
    days, remainder = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remainder, 60)
    return f"{days}d {hours}h {minutes}m"


def load_card_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    names = (
        [
            "DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        ]
        if bold
        else [
            "DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        ]
    )
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def create_status_card(
    memory_mb: float,
    cpu_percent: float,
    plugin_count: int,
    dc_id: Optional[int],
    latency_ms: Optional[int],
    uptime_seconds: float,
) -> Optional[Path]:
    STATUS_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    image_path = STATUS_TEMP_DIR / f"status_{uuid.uuid4().hex}.png"

    try:
        width, height = 1200, 600
        image = Image.new("RGBA", (width, height), "#05070d")

        ambient = Image.new("RGBA", image.size, (0, 0, 0, 0))
        ambient_draw = ImageDraw.Draw(ambient)
        ambient_draw.ellipse((760, -260, 1360, 330), fill=(36, 211, 238, 42))
        ambient_draw.ellipse((-220, 320, 360, 820), fill=(139, 92, 246, 34))
        ambient = ambient.filter(ImageFilter.GaussianBlur(120))
        image = Image.alpha_composite(image, ambient)
        draw = ImageDraw.Draw(image)

        white = "#f8fafc"
        muted = "#77849a"
        cyan = "#38d6ee"
        violet = "#a78bfa"
        blue = "#60a5fa"
        panel = "#090d15"
        line = "#1b2636"

        title_font = load_card_font(58, bold=True)
        subtitle_font = load_card_font(16, bold=True)
        label_font = load_card_font(17, bold=True)
        value_font = load_card_font(52, bold=True)
        small_value_font = load_card_font(34, bold=True)

        def text_width(text: str, font: ImageFont.ImageFont) -> int:
            box = draw.textbbox((0, 0), text, font=font)
            return box[2] - box[0]

        def centered(text: str, center_x: int, y: int, font, fill: str) -> None:
            draw.text(
                (center_x - text_width(text, font) / 2, y),
                text,
                font=font,
                fill=fill,
            )

        # Subtle technical grid.
        for x in range(0, 1201, 60):
            draw.line((x, 112, x, 600), fill=(22, 34, 52, 65), width=1)
        for y in range(112, 601, 48):
            draw.line((0, y, 1200, y), fill=(22, 34, 52, 65), width=1)

        brand_left = "Yanyu"
        brand_right = "Bot"
        brand_x = 55
        draw.text((brand_x, 20), brand_left, font=title_font, fill=white)
        draw.text(
            (brand_x + text_width(brand_left, title_font), 20),
            brand_right,
            font=title_font,
            fill=cyan,
        )
        draw.text((57, 88), "SYSTEM STATUS  /  ONLINE", font=subtitle_font, fill=muted)

        draw.rounded_rectangle(
            (44, 120, 1156, 552), radius=28, fill=panel, outline=line, width=2
        )

        main_items = [
            (78, "MEMORY", f"{memory_mb:.0f} MB", cyan, memory_mb / 1024.0),
            (438, "CPU", f"{cpu_percent:.2f}%", violet, cpu_percent / 100.0),
            (798, "PLUGINS", str(plugin_count), blue, plugin_count / 100.0),
        ]
        for left, label, value, color, ratio in main_items:
            draw.text((left, 164), label, font=label_font, fill=muted)
            draw.text((left, 207), value, font=value_font, fill=white)
            draw.rounded_rectangle((left, 280, left + 290, 289), radius=5, fill="#172235")
            fill_width = int(290 * min(1.0, max(0.08, ratio)))
            draw.rounded_rectangle(
                (left, 280, left + fill_width, 289), radius=5, fill=color
            )

        dc_text = f"DC{dc_id}" if dc_id is not None else "Unknown"
        latency_text = f"{latency_ms} ms" if latency_ms is not None else "Failed"
        uptime_text = format_card_uptime(uptime_seconds)

        draw.line((78, 337, 1122, 337), fill=line, width=2)
        lower_items = [
            (78, "DATA CENTER", dc_text, cyan),
            (438, "LATENCY", latency_text, violet),
            (798, "UPTIME", uptime_text, blue),
        ]
        for left, label, value, color in lower_items:
            draw.text((left, 384), label, font=label_font, fill=color)
            draw.text((left, 427), value, font=small_value_font, fill=white)

        draw.text(
            (78, 516),
            "TELEGRAM NETWORK CONNECTED",
            font=subtitle_font,
            fill="#516075",
        )
        draw.ellipse((1110, 519, 1122, 531), fill="#34d399")

        image.convert("RGB").save(image_path, "PNG", optimize=True)
        return image_path
    except Exception:
        safe_remove(str(image_path))
        return None


async def send_status_result(
    bot: Client,
    message: Message,
    memory_mb: float,
    cpu_percent: float,
    plugin_count: int,
    dc_id: Optional[int],
    latency_ms: Optional[int],
    uptime_seconds: float,
) -> None:
    caption = build_result(
        memory_mb,
        cpu_percent,
        plugin_count,
        dc_id,
        latency_ms,
        uptime_seconds,
    )
    image_path = await asyncio.to_thread(
        create_status_card,
        memory_mb,
        cpu_percent,
        plugin_count,
        dc_id,
        latency_ms,
        uptime_seconds,
    )
    if image_path is None:
        await message.edit(
            caption,
            parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    try:
        await bot.send_photo(
            chat_id=message.chat.id,
            photo=str(image_path),
            caption=caption,
            parse_mode=enums.ParseMode.HTML,
        )
        await message.safe_delete()
    except Exception:
        await message.edit(
            caption,
            parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True,
        )
    finally:
        safe_remove(str(image_path))


@listener(
    pattern=r"^(?:,|，)zt(?:\s|$)",
    priority=1,
    block_process=True,
    ignore_edited=True,
)
async def status_handler(bot: Client, message: Message):
    try:
        await message.edit(
            "正在读取运行状态...",
            parse_mode=enums.ParseMode.DISABLED,
        )

        dc_id = await read_account_dc(bot)
        stats_task = asyncio.to_thread(get_process_stats)
        latency_task = measure_current_dc(bot, dc_id)
        (memory_mb, cpu_percent, uptime_seconds), latency_ms = (
            await asyncio.gather(stats_task, latency_task)
        )

        await send_status_result(
            bot,
            message,
            memory_mb,
            cpu_percent,
            count_plugins(),
            dc_id,
            latency_ms,
            uptime_seconds,
        )
    except Exception as exc:
        detail = str(exc) or exc.__class__.__name__
        if len(detail) > 160:
            detail = detail[:160] + "..."

        await message.edit(
            f"运行状态读取失败\n\n{detail}",
            parse_mode=enums.ParseMode.DISABLED,
        )
