"""昵称时钟与天气。保留原昵称样式及天气逻辑，,sj 查看菜单，,sj 完整地址 设置地区。

源码不内置个人地址或城市编号；地区配置仅保存到本机 data/sj.json。
"""

import asyncio
import inspect
import json
import logging
import traceback
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from urllib.request import Request, urlopen

from pagermaid.dependence import scheduler
from pagermaid.services import bot


import html
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlencode

from pagermaid.listener import listener
from pagermaid.static import working_dir
from pyrogram.enums import ParseMode

SJ_CONFIG_PATH = Path(working_dir) / "data" / "sj.json"
_settings_lock = asyncio.Lock()
_location_generation = 0
_configuration_error = ""


def _load_sj_settings():
    global _configuration_error
    if not SJ_CONFIG_PATH.exists():
        return {}
    try:
        settings = json.loads(SJ_CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(settings, dict):
            raise ValueError()
        if not isinstance(settings.get("address"), str) or not settings["address"].strip():
            raise ValueError()
        if not re.fullmatch(r"101\d{6}", str(settings.get("city_code", ""))):
            raise ValueError()
        if not isinstance(settings.get("station"), str):
            raise ValueError()
        return settings
    except (OSError, TypeError, ValueError):
        _configuration_error = "地区配置无法读取，请重新发送 ,sj 完整地址 设置。"
        return {}


_location_settings = _load_sj_settings()


def _save_sj_settings(settings):
    SJ_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".sj-", dir=str(SJ_CONFIG_PATH.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(settings, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, SJ_CONFIG_PATH)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _region_name(value):
    return re.sub(r"(?:壮族|回族|维吾尔)?自治区$|特别行政区$|街道$|[省市区县镇乡]$", "", value)


def _address_components(address):
    parts = re.findall(r".+?(?:特别行政区|自治区|自治州|地区|街道|省|市|区|县|镇|乡)", address)
    province = next((value for value in parts if value.endswith(("省", "自治区", "特别行政区"))), "")
    county = next((value for value in reversed(parts) if value.endswith(("区", "县")) and value != province), "")
    city = next((value for value in parts if value.endswith("市")), "")
    return parts, province, county, city


def _search_weather_regions(keyword):
    # 仅用于设置地区时查询原天气服务支持的城市编号；不改变天气实况请求。
    url = "https://toy1.weather.com.cn/search?" + urlencode({"cityname": keyword, "callback": "callback"})
    request = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (PagerMaid weather plugin)",
        "Referer": "https://www.weather.com.cn/",
    })
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            payload = response.read(2 * 1024 * 1024 + 1).decode("utf-8")
        if len(payload) > 2 * 1024 * 1024:
            raise ValueError()
        start, end = payload.find("["), payload.rfind("]")
        if start < 0 or end < start:
            raise ValueError()
        result = json.loads(payload[start:end + 1])
        if not isinstance(result, list):
            raise ValueError()
        return result
    except Exception:
        raise ValueError("地区查询暂时失败，请检查网络或稍后重试。") from None


def _resolve_sj_station(address):
    parts, province, county, city = _address_components(address)
    if not parts:
        raise ValueError("请填写省、市、区县及街道等完整地区名称。")
    target = parts[-1]
    if target.endswith(("街道", "镇", "乡")) and not (county or city):
        raise ValueError("请补全省、市、区县，避免同名地区匹配错误。")
    keywords = list(dict.fromkeys(_region_name(value) for value in (target, county, city) if value))
    for keyword in keywords:
        matches = {}
        for item in _search_weather_regions(keyword):
            if not isinstance(item, dict) or not isinstance(item.get("ref"), str):
                continue
            fields = item["ref"].split("~")
            if len(fields) < 6 or not re.fullmatch(r"101\d{6}(?:\d{3})?", fields[0]):
                continue
            row_province = fields[-1]
            location, parent = fields[2], fields[4]
            if province and _region_name(province) != _region_name(row_province):
                continue
            if county and _region_name(county) not in (_region_name(location), _region_name(parent)):
                continue
            if not county and city and _region_name(city) not in (_region_name(location), _region_name(parent)):
                continue
            if not county and not city and _region_name(target) not in (_region_name(location), _region_name(parent)):
                continue
            # 原实况接口使用九位站点编号。街道地址完整保留，天气仍取对应区县站点。
            code = fields[0][:9]
            station = row_province + " · " + (parent if len(fields[0]) > 9 else location)
            matches[code] = {"city_code": code, "station": station}
        if len(matches) == 1:
            return next(iter(matches.values()))
        if len(matches) > 1:
            raise ValueError("存在多个同名地区，请补全省、市、区县后重试。")
    raise ValueError("未找到与地址对应的天气地区，原设置未改变，请核对省、市和区县名称。")


def _sj_station_display(settings):
    parts = [part.strip() for part in settings["station"].split(" · ") if part.strip()]
    _, _, _, city = _address_components(settings["address"])
    if city and _region_name(city) not in {_region_name(part) for part in parts}:
        parts.insert(1 if len(parts) > 1 else 0, _region_name(city))
    return " · ".join(parts)


def _sj_help_text():
    lines = ["<b>✦ 昵称设置</b>", ""]
    if _location_settings:
        lines.append("匹配地区：" + html.escape(_sj_station_display(_location_settings)))
    else:
        lines.extend(["匹配地区：未设置", "", "填写地址后自动运行。"])
    lines.extend(["", "<code>,sj 完整地址</code>　设置或更换地区"])
    if _configuration_error:
        lines.extend(["", html.escape(_configuration_error)])
    return "\n".join(lines)


@listener(command="sj", outgoing=True, ignore_edited=True,
          description="查看昵称设置菜单，或填写完整地址设置地区。", parameters="[完整地址]")
async def sj_menu(message):
    global _location_settings, _location_generation, _configuration_error
    parameters = getattr(message, "parameter", None) or []
    address = parameters.strip() if isinstance(parameters, str) else " ".join(parameters).strip()
    if not address or address.lower() in ("help", "帮助"):
        return await message.edit(_sj_help_text(), parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    if len(address) > 200 or any(ord(char) < 32 for char in address):
        return await message.edit("请填写 200 字以内的完整地区地址。", parse_mode=ParseMode.DISABLED)
    if _settings_lock.locked():
        return await message.edit("正在设置地区，请稍后再试。", parse_mode=ParseMode.DISABLED)
    async with _settings_lock:
        await message.edit("正在匹配地区…", parse_mode=ParseMode.DISABLED)
        try:
            if _location_settings.get("address") != address:
                matched = await _run_blocking(_resolve_sj_station, address)
                settings = {"version": 2, "address": address, **matched}
                _save_sj_settings(settings)
                _location_settings = settings
                _location_generation += 1
                _configuration_error = ""
                _apply_sj_station(settings["city_code"])
                _weather_cache.update(text=None, updated_at=None)
            await refresh_weather_cache()
            await change_name_auto()
            await message.edit(_sj_help_text(), parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except ValueError as exc:
            await message.edit(str(exc), parse_mode=ParseMode.DISABLED)
        except Exception:
            await message.edit("地区设置未完成，请检查配置文件权限或稍后重试。", parse_mode=ParseMode.DISABLED)



try:
    from pagermaid.utils import logs as _pagermaid_logs
except Exception:
    _pagermaid_logs = logging.getLogger(__name__)


# ======================== 用户配置区 ========================
# 天气每 5 分钟刷新一次。
WEATHER_REFRESH_MINUTES = 5

# 天气在对应分钟的第 0 秒刷新；Telegram 姓氏在每分钟第 8 秒更新。
WEATHER_REFRESH_SECOND = 0
NAME_UPDATE_SECOND = 8

# 中国天气网请求超时。
REQUEST_TIMEOUT_SECONDS = 6

# 允许最近一次成功的天气保留 11 分钟。
# 这样偶发一次 API 请求失败时，不会马上把天气变成 --；
# 但连续两次以上刷新失败后会停止把旧数据当成实时天气。
MAX_WEATHER_AGE_SECONDS = 660
# ==========================================================


CHINA_TZ = timezone(timedelta(hours=8))
CHINA_WEATHER_CITY_CODE = _location_settings.get("city_code", "")
CHINA_WEATHER_API_URL = (
    f"https://d1.weather.com.cn/sk_2d/{CHINA_WEATHER_CITY_CODE}.html"
)
CHINA_WEATHER_REFERER = (
    f"https://www.weather.com.cn/weather1dn/{CHINA_WEATHER_CITY_CODE}.shtml"
)

if not 1 <= WEATHER_REFRESH_MINUTES <= 59:
    raise ValueError("WEATHER_REFRESH_MINUTES 必须在 1~59 之间")
if not 0 <= WEATHER_REFRESH_SECOND <= 59:
    raise ValueError("WEATHER_REFRESH_SECOND 必须在 0~59 之间")
if not 0 <= NAME_UPDATE_SECOND <= 59:
    raise ValueError("NAME_UPDATE_SECOND 必须在 0~59 之间")

_weather_cache = {
    "text": None,
    "updated_at": None,
}

BOLD_DIGITS = str.maketrans("0123456789", "𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴𝟵")

def to_bold_digits(value):
    """把普通数字转换成 Telegram 昵称使用的粗体数字。"""
    return str(value).translate(BOLD_DIGITS)


def round_half_up(value):
    """常规四舍五入，例如 24.5 -> 25。"""
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _safe_log(level, message):
    """仅用于异常日志；日志系统本身出错也不能影响定时任务。"""
    try:
        method = getattr(_pagermaid_logs, level, None)
        if method is None:
            return

        result = method(str(message))
        if inspect.isawaitable(result):
            try:
                asyncio.get_running_loop().create_task(result)
            except RuntimeError:
                pass
    except Exception:
        pass


def weather_emoji(weather_text, weather_code=""):
    """把中国天气网天气现象转换成昵称图标。"""
    text = str(weather_text or "").strip()
    code = str(weather_code or "").lower()
    is_day = not code.startswith("n")
    if not code.startswith(("d", "n")):
        hour = datetime.now(CHINA_TZ).hour
        is_day = 6 <= hour < 18

    if "雷" in text:
        return "⛈️"
    if any(word in text for word in ("雪", "冰粒", "冻雨")):
        return "🌨️"
    if any(word in text for word in ("雨", "阵雨")):
        return "🌧️"
    if any(word in text for word in ("雾", "霾", "沙", "尘")):
        return "🌫️"
    if "阴" in text:
        return "☁️"
    if "多云" in text:
        return "🌤️" if is_day else "☁️"
    if "晴" in text:
        return "☀️" if is_day else "🌙"
    return "🌡️"


def fetch_china_weather_current():
    """请求已设置地区对应站点的实时天气。"""
    request = Request(
        CHINA_WEATHER_API_URL,
        headers={
            "User-Agent": "Mozilla/5.0 (PagerMaid weather plugin)",
            "Referer": CHINA_WEATHER_REFERER,
            "Accept": "text/html,application/json",
        },
    )

    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        payload = response.read().decode("utf-8").strip()

    start = payload.find("{")
    end = payload.rfind("}")
    if start < 0 or end <= start:
        raise RuntimeError("中国天气网响应中没有天气数据")
    data = json.loads(payload[start:end + 1])
    if not isinstance(data, dict) or data.get("temp") in (None, ""):
        raise RuntimeError("中国天气网实况缺少温度")
    return data


def fetch_weather_sync():
    """使用中国天气网实况生成“天气图标 + 实时温度”。"""
    current = fetch_china_weather_current()
    raw_temperature = current.get("temp")

    temperature = round_half_up(float(raw_temperature))
    icon = weather_emoji(current.get("weather"), current.get("weathercode"))
    return f"{icon}{to_bold_digits(temperature)}°𝗖"


async def _run_blocking(func, *args):
    """在线程池执行阻塞网络请求，避免卡住 PagerMaid 事件循环。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args))


async def refresh_weather_cache():
    """刷新天气缓存；成功时静默，失败时才记录 warning。"""
    if not CHINA_WEATHER_CITY_CODE:
        return
    location_generation = _location_generation
    try:
        weather_text = await asyncio.wait_for(
            _run_blocking(fetch_weather_sync),
            timeout=REQUEST_TIMEOUT_SECONDS + 2,
        )
        if location_generation != _location_generation:
            return _weather_cache["text"]
        _weather_cache["text"] = weather_text
        _weather_cache["updated_at"] = datetime.now(timezone.utc)
        return weather_text
    except Exception:
        _safe_log("warning", "中国天气网刷新失败:\n" + traceback.format_exc())
        return _weather_cache["text"]


def _weather_cache_is_fresh():
    """判断最近一次成功的天气是否仍可继续显示。"""
    weather_text = _weather_cache.get("text")
    updated_at = _weather_cache.get("updated_at")

    if weather_text is None or updated_at is None:
        return False

    age = (datetime.now(timezone.utc) - updated_at).total_seconds()
    return age <= MAX_WEATHER_AGE_SECONDS


async def get_display_weather():
    """只显示最近成功取得的天气数据；长期失效后显示占位符。"""
    if _weather_cache_is_fresh():
        return _weather_cache["text"]
    return "🌡️--°𝗖"


# 每 5 分钟第 0 秒刷新一次天气：00、05、10、15、20……
@scheduler.scheduled_job(
    "cron",
    minute=f"*/{WEATHER_REFRESH_MINUTES}",
    second=WEATHER_REFRESH_SECOND,
    id="autochangename_weather",
    coalesce=True,
    max_instances=1,
    misfire_grace_time=30,
)
async def refresh_weather_job():
    await refresh_weather_cache()


# Telegram 昵称时间每分钟更新一次。
# 正常成功时不写日志，只有失败时才记录 warning。
@scheduler.scheduled_job(
    "cron",
    second=NAME_UPDATE_SECOND,
    id="autochangename",
    coalesce=True,
    max_instances=1,
    misfire_grace_time=30,
)
async def change_name_auto():
    if not CHINA_WEATHER_CITY_CODE:
        return
    try:
        time_text = to_bold_digits(datetime.now(CHINA_TZ).strftime("%H:%M"))
        weather_text = await get_display_weather()
        new_last_name = f"{time_text} {weather_text}"
        await bot.update_profile(last_name=new_last_name)
    except Exception:
        _safe_log("warning", "昵称更新失败:\n" + traceback.format_exc())


async def _bootstrap():
    """插件加载/刷新后立即请求一次天气并更新昵称。"""
    await refresh_weather_cache()
    await change_name_auto()


# 热重载时用固定 ID 替换旧 bootstrap，避免重复启动任务。
try:
    scheduler.add_job(
        _bootstrap,
        "date",
        id="autochangename_bootstrap",
        replace_existing=True,
    )
except Exception:
    _safe_log("warning", "注册启动任务失败:\n" + traceback.format_exc())


def _apply_sj_station(city_code):
    global CHINA_WEATHER_CITY_CODE, CHINA_WEATHER_API_URL, CHINA_WEATHER_REFERER
    CHINA_WEATHER_CITY_CODE = city_code
    CHINA_WEATHER_API_URL = f'https://d1.weather.com.cn/sk_2d/{CHINA_WEATHER_CITY_CODE}.html'
    CHINA_WEATHER_REFERER = f'https://www.weather.com.cn/weather1dn/{CHINA_WEATHER_CITY_CODE}.shtml'
