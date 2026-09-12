"""PagerMaid-Pyro：查询 Codex 使用额度与可用重置。

安装后使用：,gpt help
本插件不保存 ChatGPT Token；它调用当前系统中已登录的 Codex CLI。
"""

import asyncio
import contextlib
import json
import math
import os
import signal
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from pyrogram.enums import ParseMode

from pagermaid.enums import Client, Message
from pagermaid.listener import listener
from pagermaid.static import working_dir


TIME_ZONE = ZoneInfo("Asia/Shanghai")
CONFIG_PATH = Path(working_dir) / "data" / "gpt_codex_usage.json"
CARD_TEMP_DIR = Path(working_dir) / "data" / "gpt_codex_usage" / "temp"
RPC_TIMEOUT = 20


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


async def stop_codex_process(process: asyncio.subprocess.Process) -> None:
    async def stop_process() -> None:
        if process.stdin is not None:
            with contextlib.suppress(Exception):
                process.stdin.close()
        if process.returncode is not None:
            await process.wait()
            return
        with contextlib.suppress(ProcessLookupError):
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            await process.wait()

    cleanup = asyncio.create_task(stop_process())
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        with contextlib.suppress(BaseException):
            await cleanup
        raise


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
        start_new_session=(os.name == "posix"),
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
        await stop_codex_process(process)

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
        return f"✦ {title}：未返回"
    used = max(0, min(100, int(round(float(window.get("usedPercent", 0))))))
    remaining = 100 - used
    return (
        f"✦ {title}\n"
        f"✦ 已用：{used}%　剩余：{remaining}%\n"
        f"✦ 重置：{format_time(window.get('resetsAt'))}"
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
            ]
        )

    return "\n".join(lines)


def format_result(data: Dict[str, Any]) -> str:
    limits = data.get("limits") or {}
    quota = [
        "<b>✦ Codex 使用额度</b>",
        format_window("5 小时使用限额", limits.get("primary")),
        format_window("每周使用限额", limits.get("secondary")),
    ]
    return "\n\n".join(quota)


def card_window(window: Optional[Dict[str, Any]]) -> Tuple[str, float, str]:
    if not window:
        return "—", 0.0, "—"
    try:
        used = max(0.0, min(100.0, float(window.get("usedPercent", 0))))
    except (TypeError, ValueError):
        used = 0.0
    remaining = max(0.0, min(100.0, 100.0 - used))
    remaining_text = f"{int(round(remaining))}%"
    parsed = parse_datetime(window.get("resetsAt"))
    reset_text = parsed.strftime("%m-%d %H:%M") if parsed else "—"
    return remaining_text, remaining / 100.0, reset_text


def card_reset_credit(data: Optional[Dict[str, Any]]) -> Tuple[str, str, str]:
    data = data or {}
    credits = [
        item
        for item in data.get("credits", [])
        if isinstance(item, dict) and item.get("status") == "available"
    ]
    try:
        available = max(0, int(data.get("availableCount", len(credits)) or 0))
    except (TypeError, ValueError):
        available = len(credits)
    if not available or not credits:
        return str(available), "—", "—"

    credit = credits[0]
    reset_type = {
        "codexRateLimits": "FULL RESET",
    }.get(str(credit.get("resetType") or ""), "RESET")
    parsed = parse_datetime(credit.get("expiresAt"))
    expires = parsed.strftime("%m-%d %H:%M") if parsed else "—"
    return str(available), reset_type, expires


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


class ScaledImageDraw:
    """Draw at high resolution using logical 1200x600 coordinates."""

    def __init__(self, image: Image.Image, scale: int) -> None:
        self.draw = ImageDraw.Draw(image)
        self.scale = scale

    def _coordinates(self, values):
        return tuple(value * self.scale for value in values)

    def textbbox(self, xy, text, font, **kwargs):
        result = self.draw.textbbox(
            self._coordinates(xy), text, font=font, **kwargs
        )
        return tuple(value / self.scale for value in result)

    def text(self, xy, text, font, fill, **kwargs) -> None:
        self.draw.text(
            self._coordinates(xy), text, font=font, fill=fill, **kwargs
        )

    def line(self, xy, fill, width=1, **kwargs) -> None:
        self.draw.line(
            self._coordinates(xy),
            fill=fill,
            width=max(1, int(width * self.scale)),
            **kwargs,
        )

    def polygon(self, xy, fill=None, outline=None) -> None:
        points = [
            (x * self.scale, y * self.scale)
            for x, y in xy
        ]
        self.draw.polygon(points, fill=fill, outline=outline)

    def ellipse(self, xy, fill=None, outline=None, width=1) -> None:
        self.draw.ellipse(
            self._coordinates(xy),
            fill=fill,
            outline=outline,
            width=max(1, int(width * self.scale)),
        )

    def rounded_rectangle(
        self, xy, radius=0, fill=None, outline=None, width=1
    ) -> None:
        self.draw.rounded_rectangle(
            self._coordinates(xy),
            radius=int(radius * self.scale),
            fill=fill,
            outline=outline,
            width=max(1, int(width * self.scale)),
        )

    def arc(self, xy, start, end, fill, width=1) -> None:
        self.draw.arc(
            self._coordinates(xy),
            start,
            end,
            fill=fill,
            width=max(1, int(width * self.scale)),
        )


def create_usage_card_legacy(data: Dict[str, Any]) -> Optional[Path]:
    CARD_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    image_path = CARD_TEMP_DIR / f"codex_usage_{uuid.uuid4().hex}.png"
    try:
        limits = data.get("limits") or {}
        primary = limits.get("primary")
        secondary = limits.get("secondary")
        primary_text, primary_ratio, primary_reset_full = card_window(primary)
        weekly_text, weekly_ratio, weekly_reset_full = card_window(secondary)
        credit_count, reset_type, _ = card_reset_credit(data.get("reset_credits"))
        primary_reset = primary_reset_full[-5:] if primary_reset_full != "—" else "—"
        weekly_date = parse_datetime((secondary or {}).get("resetsAt"))
        weekly_reset = weekly_date.strftime("%b %d").upper() if weekly_date else "—"
        expiry_date = parse_datetime(
            next(
                (
                    item.get("expiresAt")
                    for item in (data.get("reset_credits") or {}).get("credits", [])
                    if isinstance(item, dict) and item.get("status") == "available"
                ),
                None,
            )
        )
        expiry_short = expiry_date.strftime("%b %d").upper() if expiry_date else "—"

        width, height = 1200, 600
        scale = 3
        image = Image.new("RGBA", (width * scale, height * scale), "#05070d")
        ambient = Image.new("RGBA", image.size, (0, 0, 0, 0))
        ambient_draw = ScaledImageDraw(ambient, scale)
        ambient_draw.ellipse((-80, 60, 650, 680), fill=(19, 200, 235, 42))
        ambient_draw.ellipse((550, 60, 1280, 680), fill=(139, 92, 246, 40))
        ambient = ambient.filter(ImageFilter.GaussianBlur(105 * scale))
        image = Image.alpha_composite(image, ambient)
        draw = ScaledImageDraw(image, scale)

        white = "#f8fafc"
        muted = "#8491a8"
        cyan = "#35d8f5"
        violet = "#9b70ff"
        panel = "#090e17"
        title_font = load_card_font(58 * scale, bold=True)
        subtitle_font = load_card_font(18 * scale, bold=True)
        label_font = load_card_font(29 * scale, bold=True)
        value_font = load_card_font(112 * scale, bold=True)
        time_font = load_card_font(29 * scale, bold=True)
        scale_font = load_card_font(16 * scale, bold=True)
        strip_label_font = load_card_font(22 * scale, bold=True)
        strip_value_font = load_card_font(42 * scale, bold=True)

        def text_width(text: str, font: ImageFont.ImageFont) -> int:
            box = draw.textbbox((0, 0), text, font=font)
            return box[2] - box[0]

        def centered(text: str, x: int, y: int, font, fill: str) -> None:
            draw.text((x - text_width(text, font) / 2, y), text, font=font, fill=fill)

        # Double technical frame and a centered header match the selected V1.
        draw.rounded_rectangle((12, 12, 1188, 588), radius=16, outline="#1e4f9d", width=3)
        draw.rounded_rectangle((22, 22, 1178, 578), radius=13, outline="#153665", width=2)
        draw.line((30, 36, 330, 36), fill="#2267d1", width=2)
        draw.line((870, 36, 1170, 36), fill="#2267d1", width=2)
        draw.line((30, 564, 470, 564), fill="#2267d1", width=2)
        draw.line((730, 564, 1170, 564), fill="#2267d1", width=2)

        # Opaque title cabin interrupts both top frame lines so they never
        # pass through the anti-aliased title glyphs.
        draw.polygon(
            ((350, 0), (850, 0), (805, 112), (395, 112)),
            fill="#05070d",
        )
        draw.line(
            (350, 0, 395, 112, 805, 112, 850, 0),
            fill="#24559a",
            width=2,
            joint="curve",
        )
        draw.line(
            (370, 0, 410, 102, 790, 102, 830, 0),
            fill="#153665",
            width=1,
            joint="curve",
        )

        brand_left, brand_right = "Yanyu", "Bot"
        brand_width = text_width(brand_left, title_font) + text_width(brand_right, title_font)
        brand_x = (width - brand_width) / 2
        draw.text((brand_x, 5), brand_left, font=title_font, fill=white)
        draw.text(
            (brand_x + text_width(brand_left, title_font), 5),
            brand_right,
            font=title_font,
            fill=cyan,
        )
        centered("C O D E X   U S A G E", 600, 80, subtitle_font, muted)

        # Quiet grid keeps the card technical without adding decorative copy.
        for x in range(24, width - 23, 48):
            draw.line((x, 112, x, 555), fill=(24, 38, 58, 75), width=1)
        for y in range(112, 556, 40):
            draw.line((24, y, 1176, y), fill=(24, 38, 58, 70), width=1)

        draw.line((600, 126, 600, 426), fill="#24426e", width=2)
        draw.ellipse((595, 270, 605, 280), fill="#3274e5")
        gauges = (
            (300, "5 HOURS", primary_text, primary_ratio, primary_reset, cyan),
            (900, "WEEKLY", weekly_text, weekly_ratio, weekly_reset, violet),
        )
        arc_boxes = ((54, 118, 546, 610), (654, 118, 1146, 610))

        glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
        glow_draw = ScaledImageDraw(glow, scale)
        for box, (_, _, _, ratio, _, color) in zip(arc_boxes, gauges):
            if ratio > 0:
                glow_draw.arc(box, 180, 180 + 180 * ratio, fill=color, width=28)
        image = Image.alpha_composite(
            image, glow.filter(ImageFilter.GaussianBlur(18 * scale))
        )
        draw = ScaledImageDraw(image, scale)

        for box, (center_x, label, value, ratio, reset, color) in zip(arc_boxes, gauges):
            draw.arc(box, 180, 360, fill="#263754", width=28)
            if ratio > 0:
                draw.arc(box, 180, 180 + 180 * ratio, fill=color, width=28)

            # Dial ticks make the large gauge readable even in Telegram thumbnails.
            dial_center_y = 364
            for index in range(31):
                angle = math.radians(180 + index * 6)
                outer_radius = 224
                inner_radius = 211 if index % 5 else 205
                x1 = center_x + math.cos(angle) * inner_radius
                y1 = dial_center_y + math.sin(angle) * inner_radius
                x2 = center_x + math.cos(angle) * outer_radius
                y2 = dial_center_y + math.sin(angle) * outer_radius
                draw.line((x1, y1, x2, y2), fill="#315080", width=2)

            centered(label, center_x, 164, label_font, color)
            centered(value, center_x, 200, value_font, white)
            centered(f"RESET  {reset}", center_x, 326, time_font, color)
            draw.text((67 if center_x == 300 else 667, 371), "0%", font=scale_font, fill=muted)
            right_scale = "100%"
            scale_x = 533 if center_x == 300 else 1133
            draw.text(
                (scale_x - text_width(right_scale, scale_font), 371),
                right_scale,
                font=scale_font,
                fill=muted,
            )

        # One-line information rail remains legible at Telegram thumbnail size.
        draw.rounded_rectangle(
            (42, 439, 1158, 548), radius=18, fill=panel, outline="#24559a", width=3
        )
        draw.line((360, 461, 360, 525), fill="#315080", width=2)
        draw.line((760, 461, 760, 525), fill="#315080", width=2)
        draw.text((75, 479), "RESET CREDIT", font=strip_label_font, fill=muted)
        centered(credit_count, 312, 464, strip_value_font, cyan)
        centered(reset_type, 560, 472, strip_value_font, white)
        draw.text((795, 479), "EXPIRES", font=strip_label_font, fill=muted)
        centered(expiry_short, 1050, 472, strip_value_font, violet)

        resampling = getattr(Image, "Resampling", Image)
        final_image = image.convert("RGB").resize(
            (width, height), resampling.LANCZOS
        )
        final_image.save(image_path, "PNG", optimize=True)
        return image_path
    except Exception:
        with contextlib.suppress(Exception):
            image_path.unlink()
        return None


def create_usage_card(data: Dict[str, Any]) -> Optional[Path]:
    """Render the selected V3 minimal dual-ring usage card."""
    CARD_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    image_path = CARD_TEMP_DIR / f"codex_usage_{uuid.uuid4().hex}.png"
    try:
        limits = data.get("limits") or {}
        primary = limits.get("primary")
        secondary = limits.get("secondary")
        primary_text, primary_ratio, primary_reset_full = card_window(primary)
        weekly_text, weekly_ratio, _ = card_window(secondary)
        credit_count, reset_type, _ = card_reset_credit(data.get("reset_credits"))

        primary_reset = primary_reset_full[-5:] if primary_reset_full != "—" else "—"
        weekly_date = parse_datetime((secondary or {}).get("resetsAt"))
        weekly_reset = weekly_date.strftime("%b %d").upper() if weekly_date else "—"
        expiry_date = parse_datetime(
            next(
                (
                    item.get("expiresAt")
                    for item in (data.get("reset_credits") or {}).get("credits", [])
                    if isinstance(item, dict) and item.get("status") == "available"
                ),
                None,
            )
        )
        expiry_short = expiry_date.strftime("%b %d").upper() if expiry_date else "—"

        width, height = 1200, 600
        scale = 3
        image = Image.new("RGBA", (width * scale, height * scale), "#06080b")

        ambient = Image.new("RGBA", image.size, (0, 0, 0, 0))
        ambient_draw = ScaledImageDraw(ambient, scale)
        ambient_draw.ellipse((-180, 70, 560, 690), fill=(35, 216, 245, 24))
        ambient_draw.ellipse((620, 70, 1360, 690), fill=(143, 104, 255, 22))
        ambient = ambient.filter(ImageFilter.GaussianBlur(120 * scale))
        image = Image.alpha_composite(image, ambient)
        draw = ScaledImageDraw(image, scale)

        white = "#f5f7fa"
        muted = "#8b94a3"
        dim = "#262c34"
        cyan = "#35d8f5"
        violet = "#8f68ff"
        panel = "#090c11"

        title_font = load_card_font(58 * scale, bold=True)
        subtitle_font = load_card_font(18 * scale, bold=True)
        ring_value_font = load_card_font(86 * scale, bold=True)
        label_font = load_card_font(31 * scale, bold=True)
        small_font = load_card_font(25 * scale, bold=True)
        reset_font = load_card_font(27 * scale)
        strip_label_font = load_card_font(23 * scale, bold=True)
        strip_value_font = load_card_font(39 * scale, bold=True)

        def text_width(text: str, font: ImageFont.ImageFont) -> float:
            box = draw.textbbox((0, 0), text, font=font)
            return box[2] - box[0]

        def centered(text: str, x: int, y: int, font, fill: str) -> None:
            draw.text((x - text_width(text, font) / 2, y), text, font=font, fill=fill)

        brand_left, brand_right = "Yanyu", "Bot"
        brand_width = text_width(brand_left, title_font) + text_width(brand_right, title_font)
        brand_x = (width - brand_width) / 2
        draw.text((brand_x, 6), brand_left, font=title_font, fill=white)
        draw.text(
            (brand_x + text_width(brand_left, title_font), 6),
            brand_right,
            font=title_font,
            fill=cyan,
        )
        centered("C O D E X   U S A G E", 600, 85, subtitle_font, muted)
        draw.rounded_rectangle((530, 119, 670, 124), radius=3, fill=dim)
        draw.rounded_rectangle((530, 119, 600, 124), radius=3, fill=cyan)
        draw.rounded_rectangle((600, 119, 670, 124), radius=3, fill=violet)

        gauges = (
            ((48, 152, 348, 452), 198, "5 HOURS", primary_text,
             primary_ratio, primary_reset, cyan, 382),
            ((628, 152, 928, 452), 778, "WEEKLY", weekly_text,
             weekly_ratio, weekly_reset, violet, 962),
        )

        glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
        glow_draw = ScaledImageDraw(glow, scale)
        for box, _, _, _, ratio, _, color, _ in gauges:
            if ratio > 0:
                glow_draw.arc(box, -90, -90 + 360 * ratio, fill=color, width=22)
        glow = glow.filter(ImageFilter.GaussianBlur(16 * scale))
        image = Image.alpha_composite(image, glow)
        draw = ScaledImageDraw(image, scale)

        for box, center_x, label, value, ratio, reset, color, text_x in gauges:
            draw.arc(box, 0, 360, fill=dim, width=22)
            if ratio > 0:
                draw.arc(box, -90, -90 + 360 * ratio, fill=color, width=22)
            centered(value, center_x, 247, ring_value_font, white)
            draw.text((text_x, 183), label, font=label_font, fill=white)
            draw.line((text_x, 235, text_x + 158, 235), fill=dim, width=2)
            draw.text((text_x, 260), "REMAINING", font=small_font, fill=muted)
            draw.text((text_x, 322), f"RESET {reset}", font=reset_font, fill=muted)

        draw.line((600, 146, 600, 454), fill=dim, width=2)
        draw.rounded_rectangle(
            (28, 476, 1172, 580), radius=24, fill=panel, outline=dim, width=2
        )
        draw.line((345, 498, 345, 558), fill=dim, width=2)
        draw.line((770, 498, 770, 558), fill=dim, width=2)
        draw.text((62, 516), "RESET CREDIT", font=strip_label_font, fill=muted)
        centered(credit_count, 420, 498, strip_value_font, fill=white)
        centered(reset_type, 600, 512, strip_label_font, fill=white)
        draw.text((802, 516), "EXPIRES", font=strip_label_font, fill=muted)
        draw.text((952, 508), expiry_short, font=strip_value_font, fill=white)

        resampling = getattr(Image, "Resampling", Image)
        image.convert("RGB").resize(
            (width, height), resampling.LANCZOS
        ).save(image_path, "PNG", optimize=True)
        return image_path
    except Exception:
        with contextlib.suppress(Exception):
            image_path.unlink()
        return None


async def send_usage_result(client: Client, message: Message, data: Dict[str, Any]) -> None:
    caption = format_result(data)
    image_path = await asyncio.to_thread(create_usage_card, data)
    if image_path is None:
        await edit_html(message, caption)
        return

    try:
        await client.send_photo(
            chat_id=message.chat.id,
            photo=str(image_path),
            caption=caption,
            parse_mode=ParseMode.HTML,
        )
        await message.safe_delete()
    except Exception:
        await edit_html(message, caption)
    finally:
        with contextlib.suppress(Exception):
            image_path.unlink()


HELP_HTML = """<b>Codex 用量查询</b>

<code>,gpt</code>
查询 5 小时额度、每周额度和可用额度重置

<code>,gpt help</code>
显示本菜单

<code>,gpt set path /usr/local/bin/codex</code>
Codex 无法自动识别时，手动设置程序路径

<code>,gpt set path clear</code>
清除手动设置的程序路径

<b>使用条件</b>
PagerMaid 所在服务器需要安装 Codex CLI，并使用 ChatGPT 账号完成登录。插件不会保存或显示账号 Token。"""


@listener(
    is_plugin=True,
    outgoing=True,
    command="gpt",
    description="查询 Codex 使用额度与可用重置。",
    parameters="<help|set path>",
)
async def gpt_usage(client: Client, message: Message) -> None:
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
        await send_usage_result(client, message, result)
    except FileNotFoundError:
        await edit_plain(message, "Codex 程序不存在，请使用 ,gpt set path 重新设置。")
    except PermissionError:
        await edit_plain(message, "PagerMaid 没有执行 Codex CLI 的权限。")
    except Exception as exc:
        await edit_plain(message, f"查询失败：{exc}")
