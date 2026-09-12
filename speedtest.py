# -*- coding: utf-8 -*-
"""PagerMaid-Pyro plugin for Ookla speed tests."""

import asyncio
import html
import io
import json
import platform
import re
import shutil
import tarfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from pyrogram import enums

from pagermaid.enums import AsyncClient, Client, Message
from pagermaid.listener import listener
from pagermaid.static import working_dir
from pagermaid.utils import safe_remove


TITLE = "YanyuBot - Speedtest"
SMART_CANDIDATES = 3
PROCESS_TIMEOUT = 180
FALLBACK_VERSION = "1.2.0"

DATA_DIR = Path(working_dir) / "data" / "speedtest"
TEMP_DIR = DATA_DIR / "temp"
BINARY_PATH = DATA_DIR / "speedtest"


def ensure_directories() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)


def architecture_name() -> str:
    machine = platform.machine().lower()
    names = {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "aarch64": "aarch64",
        "arm64": "aarch64",
        "armv7l": "armhf",
        "armv7": "armhf",
    }
    if machine not in names:
        raise RuntimeError(f"Unsupported CPU architecture: {machine or 'unknown'}")
    return names[machine]


async def latest_cli_version(request: AsyncClient) -> str:
    try:
        response = await request.get(
            "https://install.speedtest.net/app/cli/", timeout=15
        )
        versions = re.findall(
            r"ookla-speedtest-([0-9]+(?:\.[0-9]+){2})-linux-",
            response.text,
            re.IGNORECASE,
        )
        return max(versions, key=lambda item: tuple(map(int, item.split("."))))
    except Exception:
        return FALLBACK_VERSION


def extract_speedtest_binary(archive_data: bytes) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(archive_data), mode="r:gz") as archive:
        for member in archive.getmembers():
            if member.isfile() and Path(member.name).name == "speedtest":
                source = archive.extractfile(member)
                if source is not None:
                    return source.read()
    raise RuntimeError("Speedtest binary was not found in the package")


async def download_cli(request: AsyncClient, force: bool = False) -> Path:
    ensure_directories()
    if BINARY_PATH.is_file() and not force:
        return BINARY_PATH

    versions = [await latest_cli_version(request), FALLBACK_VERSION]
    last_error = "Download failed"
    for version in dict.fromkeys(versions):
        filename = f"ookla-speedtest-{version}-linux-{architecture_name()}.tgz"
        url = f"https://install.speedtest.net/app/cli/{filename}"
        try:
            response = await request.get(url, timeout=45)
            if getattr(response, "status_code", 200) != 200:
                raise RuntimeError(f"HTTP {response.status_code}")
            binary = extract_speedtest_binary(response.content)
            if not binary:
                raise RuntimeError("Downloaded binary is empty")
            BINARY_PATH.write_bytes(binary)
            BINARY_PATH.chmod(0o755)
            return BINARY_PATH
        except Exception as exc:
            last_error = str(exc) or exc.__class__.__name__

    safe_remove(str(BINARY_PATH))
    raise RuntimeError(f"Speedtest CLI installation failed: {last_error}")


async def run_process(
    executable: Path,
    arguments: Sequence[str],
    timeout: int = PROCESS_TIMEOUT,
) -> Tuple[str, str, int]:
    process = await asyncio.create_subprocess_exec(
        str(executable),
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        raise RuntimeError(f"Speed test timed out after {timeout} seconds")

    return (
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
        process.returncode or 0,
    )


async def resolve_executable(
    request: AsyncClient, use_system: bool = False
) -> Path:
    if use_system:
        system_path = shutil.which("speedtest")
        if system_path:
            return Path(system_path)
        raise RuntimeError("System Speedtest CLI is not installed")
    return await download_cli(request)


def parse_cli_json(stdout: str, stderr: str, return_code: int) -> Dict[str, Any]:
    if return_code != 0:
        detail = stderr or stdout or f"Exit code {return_code}"
        if "NoServersException" in detail:
            raise RuntimeError("No available test server")
        raise RuntimeError("Speedtest failed: " + detail[:240])
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Invalid Speedtest response") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Speedtest returned no valid result")
    return result


async def get_nearby_servers(
    request: AsyncClient, use_system: bool = False
) -> List[Dict[str, Any]]:
    executable = await resolve_executable(request, use_system)
    stdout, stderr, code = await run_process(
        executable,
        ["--accept-license", "--accept-gdpr", "-f", "json", "-L"],
        timeout=45,
    )
    result = parse_cli_json(stdout, stderr, code)
    servers = result.get("servers", [])
    return [item for item in servers if isinstance(item, dict) and item.get("id")]


async def run_speedtest(
    request: AsyncClient,
    server_id: Optional[int] = None,
    use_system: bool = False,
) -> Dict[str, Any]:
    executable = await resolve_executable(request, use_system)
    arguments = ["--accept-license", "--accept-gdpr", "-f", "json"]
    if server_id is not None:
        arguments.extend(["-s", str(server_id)])
    stdout, stderr, code = await run_process(executable, arguments)
    return parse_cli_json(stdout, stderr, code)


def result_values(result: Dict[str, Any]) -> Tuple[float, float, float]:
    try:
        download = float(result["download"]["bandwidth"])
        upload = float(result["upload"]["bandwidth"])
        latency = max(0.01, float(result["ping"]["latency"]))
        return download, upload, latency
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Speedtest result is missing speed or latency data") from exc


def select_best_result(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Select by download bandwidth, then by lower latency."""
    if not results:
        raise RuntimeError("No valid speed test result")
    return max(results, key=lambda item: (result_values(item)[0], -result_values(item)[2]))


async def smart_speedtest(
    request: AsyncClient,
    use_system: bool = False,
) -> Dict[str, Any]:
    servers = await get_nearby_servers(request, use_system)
    candidates = servers[:SMART_CANDIDATES]
    if not candidates:
        return await run_speedtest(request, use_system=use_system)

    results: List[Dict[str, Any]] = []
    for server in candidates:
        try:
            result = await run_speedtest(request, int(server["id"]), use_system)
            result_values(result)
            results.append(result)
        except Exception:
            continue

    if not results:
        return await run_speedtest(request, use_system=use_system)
    return select_best_result(results)


def measured_mbps(bandwidth: Any) -> Optional[float]:
    try:
        return float(bandwidth) * 8 / 1_000_000
    except (TypeError, ValueError):
        return None


def format_rate(bandwidth: Any) -> str:
    value = measured_mbps(bandwidth)
    return f"{value:.2f} Mbps" if value is not None else "Unknown"


def format_result(result: Dict[str, Any]) -> str:
    server = result.get("server") or {}
    ping = result.get("ping") or {}
    download = result.get("download") or {}
    upload = result.get("upload") or {}

    server_id = html.escape(str(server.get("id") or "Unknown"))
    server_location = html.escape(str(server.get("location") or "Unknown"))
    provider = str(result.get("isp") or "Unknown").strip()
    provider = provider[:1].upper() + provider[1:]
    isp = html.escape(provider)
    try:
        latency = f"{float(ping.get('latency')):.2f} ms"
    except (TypeError, ValueError):
        latency = "Unknown"

    lines = [
        f"✦ Network: <code>{isp}</code>",
        f"✦ Server: <code>{server_id} · {server_location}</code>",
        f"✦ Ping: <code>{latency}</code>",
        f"✦ Download: <code>{format_rate(download.get('bandwidth'))}</code>",
        f"✦ Upload: <code>{format_rate(upload.get('bandwidth'))}</code>",
    ]
    return f"<b>{TITLE}</b>\n\n<blockquote>" + "\n\n".join(lines) + "</blockquote>"


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


def create_result_card(result: Dict[str, Any]) -> Optional[Path]:
    ensure_directories()
    image_path = TEMP_DIR / f"speedtest_{uuid.uuid4().hex}.png"
    try:
        server = result.get("server") or {}
        ping = result.get("ping") or {}
        download = result.get("download") or {}
        upload = result.get("upload") or {}

        provider = str(result.get("isp") or "Unknown").strip()
        provider = provider[:1].upper() + provider[1:]
        server_text = " · ".join(
            str(value)
            for value in (
                server.get("id"),
                server.get("name"),
                server.get("location"),
            )
            if value
        ) or "Unknown"
        if len(server_text) > 54:
            server_text = server_text[:51].rstrip() + "..."

        try:
            ping_text = f"{float(ping.get('latency')):.2f} ms"
        except (TypeError, ValueError):
            ping_text = "Unknown"
        down_value = measured_mbps(download.get("bandwidth"))
        up_value = measured_mbps(upload.get("bandwidth"))
        down_text = f"{down_value:.2f}" if down_value is not None else "—"
        up_text = f"{up_value:.2f}" if up_value is not None else "—"

        image = Image.new("RGBA", (1200, 600), "#05070d")

        ambient = Image.new("RGBA", image.size, (0, 0, 0, 0))
        ambient_draw = ImageDraw.Draw(ambient)
        ambient_draw.ellipse((10, 190, 610, 760), fill=(0, 196, 255, 34))
        ambient_draw.ellipse((590, 190, 1190, 760), fill=(132, 82, 255, 34))
        ambient = ambient.filter(ImageFilter.GaussianBlur(95))
        image = Image.alpha_composite(image, ambient)
        draw = ImageDraw.Draw(image)

        white = "#f8fafc"
        muted = "#8290a8"
        cyan = "#35d8f5"
        violet = "#9b70ff"
        line = "#172236"

        title_font = load_card_font(58, bold=True)
        subtitle_font = load_card_font(18, bold=True)
        label_font = load_card_font(21, bold=True)
        body_font = load_card_font(32, bold=True)
        unit_font = load_card_font(28, bold=True)

        def text_width(text: str, font: ImageFont.ImageFont) -> int:
            box = draw.textbbox((0, 0), text, font=font)
            return box[2] - box[0]

        def centered(text: str, center_x: int, y: int, font, fill: str) -> None:
            draw.text((center_x - text_width(text, font) / 2, y), text, font=font, fill=fill)

        def fitted_font(text: str, maximum_width: int, start: int = 32):
            for size in range(start, 21, -1):
                font = load_card_font(size, bold=True)
                if text_width(text, font) <= maximum_width:
                    return font
            return load_card_font(21, bold=True)

        def fitted_value_font(*values: str) -> ImageFont.ImageFont:
            # Both metrics use the same font size. This keeps the layout
            # balanced while preventing 4+ digit Mbps values from overflowing.
            for size in range(92, 55, -2):
                candidate = load_card_font(size, bold=True)
                if all(text_width(value, candidate) <= 390 for value in values):
                    return candidate
            return load_card_font(54, bold=True)

        value_font = fitted_value_font(down_text, up_text)

        brand_left = "Yanyu"
        brand_right = "Bot"
        brand_width = text_width(brand_left, title_font) + text_width(brand_right, title_font)
        brand_x = (1200 - brand_width) / 2
        draw.text((brand_x, 20), brand_left, font=title_font, fill=white)
        draw.text(
            (brand_x + text_width(brand_left, title_font), 20),
            brand_right,
            font=title_font,
            fill=cyan,
        )
        centered("N E T W O R K   S P E E D", 600, 88, subtitle_font, muted)

        # Compact information rail.
        draw.rounded_rectangle(
            (44, 124, 1156, 232), radius=22, fill="#090e17", outline=line, width=2
        )
        draw.line((44, 146, 44, 210), fill=cyan, width=4)
        draw.line((1156, 146, 1156, 210), fill=violet, width=4)
        draw.line((310, 146, 310, 210), fill="#263248", width=1)
        draw.line((942, 146, 942, 210), fill="#263248", width=1)

        draw.text((78, 148), "NETWORK", font=label_font, fill=muted)
        draw.text((78, 181), provider, font=body_font, fill=white)
        draw.text((348, 148), "SERVER", font=label_font, fill=muted)
        server_font = fitted_font(server_text, 550)
        draw.text((348, 181), server_text, font=server_font, fill=white)
        draw.text((980, 148), "PING", font=label_font, fill=muted)
        draw.text((980, 181), ping_text, font=body_font, fill=white)

        # Quiet technical grid, strongest near the bottom.
        for x in range(0, 1201, 60):
            draw.line((x, 252, x, 600), fill=(25, 37, 57, 80), width=1)
        for y in range(252, 601, 48):
            draw.line((0, y, 1200, y), fill=(25, 37, 57, 70), width=1)
        draw.line((600, 270, 600, 575), fill="#1c2638", width=1)

        # Subtle luminous half-rings inspired by the selected minimal concept.
        arc_boxes = [(50, 274, 550, 774), (650, 274, 1150, 774)]
        arc_colors = [(53, 216, 245, 255), (155, 112, 255, 255)]
        glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
        glow_draw = ImageDraw.Draw(glow)
        for box, color in zip(arc_boxes, arc_colors):
            glow_draw.arc(box, 180, 360, fill=color, width=10)
        image = Image.alpha_composite(
            image, glow.filter(ImageFilter.GaussianBlur(16))
        )
        draw = ImageDraw.Draw(image)
        for box, color in zip(arc_boxes, arc_colors):
            draw.arc(box, 180, 360, fill=color, width=5)

        centered("DOWNLOAD", 300, 310, label_font, cyan)
        centered(down_text, 300, 350, value_font, white)
        centered("Mbps", 300, 465, unit_font, muted)

        centered("UPLOAD", 900, 310, label_font, violet)
        centered(up_text, 900, 350, value_font, white)
        centered("Mbps", 900, 465, unit_font, muted)

        image.convert("RGB").save(image_path, "PNG", optimize=True)
        return image_path
    except Exception:
        safe_remove(str(image_path))
        return None


async def edit_safely(
    message: Message,
    text: str,
    parse_mode: enums.ParseMode = enums.ParseMode.HTML,
) -> None:
    try:
        await message.edit(
            text,
            parse_mode=parse_mode,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        if "MESSAGE_NOT_MODIFIED" not in str(exc):
            raise


def help_text() -> str:
    return (
        f"<b>{TITLE}</b>\n\n<blockquote>"
        "<code>,st</code>  Auto test\n\n"
        "<code>,st best</code>  Test 3 servers · Best download\n\n"
        "<code>,st list</code>  Nearby servers\n\n"
        "<code>,st ID</code>  Select a server\n\n"
        "<code>,st help</code>  Help"
        "</blockquote>"
    )


async def send_result(
    client: Client,
    message: Message,
    result: Dict[str, Any],
) -> None:
    text = format_result(result)
    image_path = await asyncio.to_thread(create_result_card, result)
    if image_path is None:
        await edit_safely(message, text)
        return

    try:
        await client.send_photo(
            chat_id=message.chat.id,
            photo=str(image_path),
            caption=text,
            parse_mode=enums.ParseMode.HTML,
        )
        await message.safe_delete()
    except Exception:
        await edit_safely(message, text)
    finally:
        safe_remove(str(image_path))


@listener(
    command="st",
    description="Ookla network speed test",
    parameters="(best/list/ID/help)",
)
async def speedtest_handler(
    client: Client, message: Message, request: AsyncClient
):
    arguments = message.arguments.strip().split()
    use_system = "--system" in arguments
    arguments = [item for item in arguments if item != "--system"]
    command = arguments[0].lower() if arguments else ""

    try:
        if command == "help":
            await edit_safely(message, help_text())
            return

        if command == "list":
            await edit_safely(message, "Loading nearby servers...", enums.ParseMode.DISABLED)
            servers = await get_nearby_servers(request, use_system)
            if not servers:
                raise RuntimeError("No nearby server found")
            lines = []
            for server in servers[:20]:
                server_id = html.escape(str(server.get("id")))
                name = html.escape(str(server.get("name") or "Unknown"))
                location = html.escape(str(server.get("location") or ""))
                suffix = f" · {location}" if location else ""
                lines.append(f"<code>{server_id}</code>　{name}{suffix}")
            text = f"<b>Nearby Servers</b>\n\n<blockquote>" + "\n\n".join(lines) + "</blockquote>"
            await edit_safely(message, text)
            return

        if command == "best":
            await edit_safely(message, "Testing...", enums.ParseMode.DISABLED)
            result = await smart_speedtest(request, use_system)
            await send_result(client, message, result)
            return

        if command and command.isdigit():
            await edit_safely(message, "Testing...", enums.ParseMode.DISABLED)
            result = await run_speedtest(request, int(command), use_system)
            await send_result(client, message, result)
            return

        if command:
            await edit_safely(message, help_text())
            return

        await edit_safely(
            message,
            "Testing...",
            enums.ParseMode.DISABLED,
        )
        result = await run_speedtest(request, use_system=use_system)
        await send_result(client, message, result)
    except Exception as exc:
        detail = html.escape(str(exc) or exc.__class__.__name__)
        await edit_safely(
            message,
            f"<b>Speedtest Failed</b>\n\n<blockquote>{detail}</blockquote>",
        )
