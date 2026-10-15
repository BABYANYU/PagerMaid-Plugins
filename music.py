"""PagerMaid 音乐查询：展示前七条结果，回复序号获取 FLAC 下载直链。

用法：
    ,y 歌名
    ,y 歌名 歌手

说明：
    - 搜索：POST /ajax.php?act=search
    - 音源：POST /ajax.php?act=getUrl
    - 默认请求最高 FLAC 档：format=flac, bitrate=2000
    - 回复搜索结果发送 1～7 选择歌曲
    - 仅提供下载链接，不在 VPS 下载或上传音频
    - 不需要额外 Python 包
    - 如果站点要求 SafeLine 浏览器会话，可在 VPS 环境变量 FLAC_COOKIE
      中放入你自己浏览器当前 Cookie；插件不会自动破解验证码/挑战。
"""

import asyncio
import html
import http.cookiejar
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pagermaid.listener import listener
from pagermaid.services import bot
from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.handlers import MessageHandler


BASE_URL = "https://flac.music.hi.cn"
AJAX_URL = BASE_URL + "/ajax.php"
REQUEST_TIMEOUT = 20
MAX_RESPONSE_SIZE = 5 * 1024 * 1024

HELP = (
    "<b>音乐中心</b>\n\n"
    "<blockquote><code>,y 歌名</code>\n\n"
    "<code>,y 歌名 歌手</code></blockquote>"
)

RUNNING = set()
SEARCH_RESULTS = {}
RESULT_TTL = 10 * 60


class MusicError(Exception):
    pass


class SiteBlocked(MusicError):
    pass


def _headers(ajax=False):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/150.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": BASE_URL + "/",
        "Cache-Control": "no-cache",
    }

    if ajax:
        headers.update({
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": BASE_URL,
            "X-Requested-With": "XMLHttpRequest",
        })
    else:
        headers["Accept"] = (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        )

    cookie = os.environ.get("FLAC_COOKIE", "").strip()
    if cookie:
        headers["Cookie"] = cookie

    return headers


def _build_opener():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar)
    )


def _read_limited(response):
    raw = response.read(MAX_RESPONSE_SIZE + 1)
    if len(raw) > MAX_RESPONSE_SIZE:
        raise MusicError("网站返回数据过大，请稍后重试。")
    return raw


def _bootstrap(opener):
    """正常访问首页建立站点会话；不处理或破解 SafeLine 挑战。"""
    request = urllib.request.Request(
        BASE_URL + "/",
        headers=_headers(ajax=False),
    )

    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT) as response:
            _read_limited(response)
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        if code in (403, 429, 468):
            raise SiteBlocked(
                "网站防护拒绝访问（HTTP %s）。"
                "如浏览器可以正常访问，请设置 FLAC_COOKIE 后重试。" % code
            ) from None
        raise MusicError("网站请求失败（HTTP %s）。" % code) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise MusicError("连接失败，请检查 VPS 能否访问 flac.music.hi.cn。") from None


def _post_json(opener, action, data):
    url = AJAX_URL + "?" + urllib.parse.urlencode({"act": action})
    body = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers=_headers(ajax=True),
        method="POST",
    )

    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT) as response:
            raw = _read_limited(response)
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        code = exc.code
        try:
            raw = _read_limited(exc)
        except Exception:
            raw = b""
        finally:
            exc.close()

        if code in (403, 429, 468):
            raise SiteBlocked(
                "网站防护拒绝接口请求（HTTP %s）。"
                "如浏览器可以正常访问，请设置 FLAC_COOKIE 后重试。" % code
            ) from None

        detail = raw.decode("utf-8", errors="replace")[:180]
        raise MusicError(
            "接口请求失败（HTTP %s）%s" %
            (code, ("：" + detail) if detail else "")
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise MusicError("音乐接口连接失败，请稍后重试。") from None

    if not 200 <= status < 300:
        raise MusicError("音乐接口返回 HTTP %s。" % status)

    text = raw.decode("utf-8", errors="replace").strip()

    try:
        result = json.loads(text)
    except ValueError:
        lowered = text.lower()
        if (
            "safeline" in lowered
            or "<html" in lowered
            or "<script" in lowered
        ):
            raise SiteBlocked(
                "网站返回了防护验证页面，而不是音乐数据。"
                "请设置你自己浏览器当前的 FLAC_COOKIE 后重试。"
            ) from None
        raise MusicError("音乐接口返回的数据无法解析。") from None

    if not isinstance(result, dict):
        raise MusicError("音乐接口返回格式异常。")

    return result


def _clean(value):
    return " ".join(str(value or "").split())


def _get_first(mapping, *keys):
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return ""


def search_music(keyword):
    opener = _build_opener()
    _bootstrap(opener)

    result = _post_json(
        opener,
        "search",
        {
            "keyword": keyword,
            "page": "1",
            "size": "7",
        },
    )

    if result.get("code") != 0:
        raise MusicError(
            "搜索失败：%s" %
            _clean(
                result.get("msg")
                or result.get("message")
                or result.get("code")
            )
        )

    data = result.get("data") or {}
    songs = data.get("list") or []

    if not isinstance(songs, list) or not songs:
        raise MusicError("没有搜索到匹配的歌曲。")

    return songs[:7]


def resolve_best_url(opener, item):
    song_id = _get_first(item, "id", "songid", "song_id")
    if song_id in (None, ""):
        raise MusicError("搜索结果缺少歌曲 ID。")

    payload = {
        "songid": str(song_id),
        "format": "flac",
        "bitrate": "2000",
    }

    if item.get("time") not in (None, ""):
        payload["time"] = str(item["time"])
    if item.get("sign") not in (None, ""):
        payload["sign"] = str(item["sign"])

    result = _post_json(opener, "getUrl", payload)

    if result.get("code") != 0:
        raise MusicError(
            "最高音质解析失败：%s" %
            _clean(
                result.get("msg")
                or result.get("message")
                or result.get("code")
            )
        )

    data = result.get("data") or {}
    url = _clean(data.get("url"))

    if not url:
        raise MusicError("接口没有返回可用的最高音质链接。")

    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise MusicError("接口返回的音乐链接格式异常。")

    return url, data


def resolve_song(item):
    opener = _build_opener()
    _bootstrap(opener)
    return resolve_best_url(opener, item)


def _escaped(value, limit=100):
    text = _clean(value)
    if len(text) > limit:
        text = text[:limit - 1] + "…"
    return html.escape(text)


def _song_fields(item):
    name = _escaped(
        _get_first(item, "name", "songname", "title") or "未知歌曲",
        70,
    )
    artist = _escaped(
        _get_first(item, "artist", "singer", "author") or "未知歌手",
        60,
    )
    album = _escaped(
        _get_first(item, "album_name", "album", "albumName"),
        60,
    )
    return name, artist, album


def _search_caption(songs):
    lines = ["<b>音乐搜索</b>  %d 首" % len(songs), "", "<blockquote>"]
    for index, item in enumerate(songs, 1):
        name, artist, album = _song_fields(item)
        lines.append("<b>%d  %s</b>" % (index, name))
        lines.append("%s%s" % (artist, " · " + album if album else ""))
        if index != len(songs):
            lines.append("")
    lines.append("</blockquote>")
    return "\n".join(lines)


def _result_caption(item, _detail, url):
    name, artist, album = _song_fields(item)
    lines = ["<b>%s</b>" % name, artist]
    if album:
        lines.extend(["", "专辑　%s" % album])
    lines += ["", "<a href=\"%s\">下载歌曲</a>" % html.escape(url, quote=True)]
    return "\n".join(lines)


def _prune_results():
    now = time.monotonic()
    for key, (created, _) in list(SEARCH_RESULTS.items()):
        if now - created > RESULT_TTL:
            SEARCH_RESULTS.pop(key, None)


def _recover_song(replied, index):
    """刷新插件后内存缓存消失时，根据被回复的列表重新定位歌曲。"""
    text = getattr(replied, "text", None) or ""
    if not text.startswith("音乐搜索"):
        return None

    lines = [line.strip() for line in text.splitlines()]
    wanted = None
    for position, line in enumerate(lines):
        match = re.match(r"^([1-7])\s+(.+)$", line)
        if match and int(match.group(1)) == index + 1:
            subtitle = lines[position + 1] if position + 1 < len(lines) else ""
            artist, _, album = subtitle.partition(" · ")
            wanted = (match.group(2), artist, album)
            break
    if not wanted:
        return None

    title, artist, album = wanted
    for item in search_music(title):
        found_title, found_artist, found_album = (
            _clean(_get_first(item, "name", "songname", "title")),
            _clean(_get_first(item, "artist", "singer", "author")),
            _clean(_get_first(item, "album_name", "album", "albumName")),
        )
        if (found_title, found_artist, found_album) == (title, artist, album):
            return item
    return None


def _extract_keyword(message):
    # PagerMaid command 模式优先使用 parameter，兼容不同版本。
    value = getattr(message, "parameter", None)

    if isinstance(value, (list, tuple)):
        keyword = " ".join(str(part) for part in value).strip()
        if keyword:
            return keyword

    if isinstance(value, str):
        keyword = value.strip()
        if keyword:
            return keyword

    arguments = getattr(message, "arguments", None)
    if isinstance(arguments, str) and arguments.strip():
        return arguments.strip()

    text = (getattr(message, "text", None) or "").strip()
    for prefix in (",y", "，y", ",Y", "，Y"):
        if text.startswith(prefix):
            return text[len(prefix):].strip()

    return ""


@listener(
    command="y",
    outgoing=True,
    ignore_edited=True,
    description="搜索前七首歌曲，回复序号获取 FLAC 下载链接。",
)
async def music_lookup(client, message):
    keyword = _extract_keyword(message)

    if not keyword:
        return await message.edit(
            HELP,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    key = message.chat.id
    if key in RUNNING:
        return await message.edit(
            "当前聊天已有音乐查询，请稍后重试。",
            parse_mode=ParseMode.DISABLED,
        )

    RUNNING.add(key)

    try:
        songs = await asyncio.to_thread(search_music, keyword)
        await message.edit(
            _search_caption(songs),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        _prune_results()
        SEARCH_RESULTS[(key, message.id)] = (time.monotonic(), songs)

    except SiteBlocked as exc:
        await message.edit(
            str(exc),
            parse_mode=ParseMode.DISABLED,
            disable_web_page_preview=True,
        )

    except MusicError as exc:
        await message.edit(
            str(exc),
            parse_mode=ParseMode.DISABLED,
            disable_web_page_preview=True,
        )

    except Exception as exc:
        await message.edit(
            "音乐查询未完成 · " + type(exc).__name__,
            parse_mode=ParseMode.DISABLED,
            disable_web_page_preview=True,
        )

    finally:
        RUNNING.discard(key)


async def music_select(client, message):
    sender = getattr(message, "from_user", None)
    if not (getattr(message, "outgoing", False) or getattr(sender, "is_self", False)):
        return

    replied = getattr(message, "reply_to_message", None)
    reply_id = getattr(message, "reply_to_message_id", None) or getattr(replied, "id", None)
    if reply_id is None:
        return
    if replied is None:
        try:
            replied = await client.get_messages(message.chat.id, reply_id)
        except Exception:
            replied = None

    key = message.chat.id
    index = int(message.text) - 1
    entry = SEARCH_RESULTS.get((key, reply_id))
    item = None
    if entry is not None:
        created, songs = entry
        if time.monotonic() - created <= RESULT_TTL and index < len(songs):
            item = songs[index]
        elif index >= len(songs):
            return await message.edit("没有这条歌曲，请回复列表中的数字。", parse_mode=ParseMode.DISABLED)
        else:
            SEARCH_RESULTS.pop((key, reply_id), None)

    if item is None and not (getattr(replied, "text", None) or "").startswith("音乐搜索"):
        return

    if key in RUNNING:
        return await message.edit("当前聊天已有音乐查询，请稍后重试。", parse_mode=ParseMode.DISABLED)

    RUNNING.add(key)
    try:
        if item is None:
            item = await asyncio.to_thread(_recover_song, replied, index)
        if item is None:
            return await message.edit("未能定位这首歌曲，请重新搜索。", parse_mode=ParseMode.DISABLED)

        url, detail = await asyncio.to_thread(resolve_song, item)
        await message.edit(
            _result_caption(item, detail, url),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except MusicError as exc:
        await message.edit(str(exc), parse_mode=ParseMode.DISABLED)
    except Exception as exc:
        await message.edit("音乐解析未完成 · " + type(exc).__name__, parse_mode=ParseMode.DISABLED)
    finally:
        RUNNING.discard(key)


# 原始消息处理器只接收数字，不依赖指令前缀或 PagerMaid 的命令解析。
bot.add_handler(
    MessageHandler(music_select, filters=filters.text & filters.regex(r"^[1-7]$")),
    group=2,
)
