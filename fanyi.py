"""PagerMaid-Pyro plugin: reply with ,f / ，f to translate a message to Chinese."""

import asyncio
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pyrogram import enums

from pagermaid.enums import Client, Message
from pagermaid.listener import listener


GOOGLE_TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
DEEPL_FREE_URL = "https://api-free.deepl.com/v2/translate"
DEEPL_PAID_URL = "https://api.deepl.com/v2/translate"

CONFIG_FILE = Path(__file__).with_name("f_translate_config.json")
REQUEST_TIMEOUT = 20
MAX_TELEGRAM_TEXT = 4096


def _get_deepl_key() -> str:
    try:
        data = json.loads(
            CONFIG_FILE.read_text(
                encoding="utf-8"
            )
        )

        return str(
            data.get(
                "deepl_auth_key",
                "",
            )
        ).strip()

    except (
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
    ):
        return ""


def _save_deepl_key(
    auth_key: str,
) -> None:

    temporary = CONFIG_FILE.with_suffix(
        ".tmp"
    )

    temporary.write_text(
        json.dumps(
            {
                "deepl_auth_key": auth_key
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    temporary.replace(
        CONFIG_FILE
    )

    try:
        CONFIG_FILE.chmod(
            0o600
        )
    except OSError:
        pass


def _clear_deepl_key() -> None:
    try:
        CONFIG_FILE.unlink()

    except FileNotFoundError:
        pass


def _translate_google_sync(
    text: str,
) -> str:

    form = urlencode(
        {
            "client": "gtx",
            "sl": "auto",
            "tl": "zh-CN",
            "dt": "t",
            "q": text,
        }
    ).encode(
        "utf-8"
    )

    request = Request(
        GOOGLE_TRANSLATE_URL,
        data=form,
        headers={
            "Content-Type":
                "application/x-www-form-urlencoded; charset=UTF-8",

            "User-Agent":
                "Mozilla/5.0",
        },
        method="POST",
    )

    with urlopen(
        request,
        timeout=REQUEST_TIMEOUT,
    ) as response:

        payload = json.loads(
            response
            .read()
            .decode(
                "utf-8"
            )
        )

    if (
        not payload
        or not payload[0]
    ):
        raise ValueError(
            "Google 没有返回翻译内容"
        )

    translated = "".join(
        part[0]
        for part in payload[0]

        if (
            part
            and isinstance(
                part[0],
                str,
            )
        )
    ).strip()

    if not translated:
        raise ValueError(
            "Google 翻译结果为空"
        )

    return translated


def _translate_deepl_sync(
    text: str,
    auth_key: str,
) -> str:

    if auth_key.endswith(
        ":fx"
    ):
        endpoint = (
            DEEPL_FREE_URL
        )
    else:
        endpoint = (
            DEEPL_PAID_URL
        )

    body = json.dumps(
        {
            "text": [
                text
            ],
            "target_lang":
                "ZH-HANS",
        }
    ).encode(
        "utf-8"
    )

    request = Request(
        endpoint,
        data=body,
        headers={
            "Authorization":
                f"DeepL-Auth-Key {auth_key}",

            "Content-Type":
                "application/json",

            "User-Agent":
                "PagerMaid-f-translate/3.0",
        },
        method="POST",
    )

    with urlopen(
        request,
        timeout=REQUEST_TIMEOUT,
    ) as response:

        payload = json.loads(
            response
            .read()
            .decode(
                "utf-8"
            )
        )

    translated = (
        payload[
            "translations"
        ][0][
            "text"
        ]
        .strip()
    )

    if not translated:
        raise ValueError(
            "DeepL 翻译结果为空"
        )

    return translated


def _translate_sync(
    text: str,
) -> str:

    auth_key = (
        _get_deepl_key()
    )

    if auth_key:

        try:
            return (
                _translate_deepl_sync(
                    text,
                    auth_key,
                )
            )

        except (
            HTTPError,
            URLError,
            TimeoutError,
            ValueError,
            TypeError,
            KeyError,
            json.JSONDecodeError,
        ):
            pass

    return (
        _translate_google_sync(
            text
        )
    )


async def _translate(
    text: str,
) -> str:

    return await asyncio.to_thread(
        _translate_sync,
        text,
    )


async def _edit_plain(
    message: Message,
    text: str,
):

    return await message.edit(
        text,
        parse_mode=(
            enums
            .ParseMode
            .DISABLED
        ),
    )


async def _translate_reply(
    message: Message,
):

    replied = (
        message.reply_to_message
    )

    if not replied:

        return await _edit_plain(
            message,
            "请先回复一条消息，再输入 ，f"
        )

    source_text = (
        replied.text
        or replied.caption
        or ""
    ).strip()

    if not source_text:

        return await _edit_plain(
            message,
            "无法翻译：被回复的消息没有文字。"
        )

    try:

        translated = (
            await _translate(
                source_text
            )
        )

    except HTTPError as error:

        if error.code == 429:

            error_text = (
                "翻译失败：请求过于频繁，请稍后再试。"
            )

        else:

            error_text = (
                "翻译失败："
                f"HTTP {error.code}"
            )

        return await _edit_plain(
            message,
            error_text,
        )

    except (
        URLError,
        TimeoutError,
        asyncio.TimeoutError,
    ):

        return await _edit_plain(
            message,
            "翻译失败：无法连接翻译服务器。"
        )

    except (
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
    ):

        return await _edit_plain(
            message,
            "翻译失败：服务器返回的数据无效。"
        )

    except Exception as error:

        return await _edit_plain(
            message,
            "翻译失败："
            f"{type(error).__name__}"
        )

    if (
        len(translated)
        > MAX_TELEGRAM_TEXT
    ):

        translated = (
            translated[
                :MAX_TELEGRAM_TEXT - 1
            ]
            + "…"
        )

    return await _edit_plain(
        message,
        translated,
    )


@listener(
    pattern=r"^(,|，)f(?: |$)([\s\S]*)",
    priority=1,
    block_process=True,
    ignore_edited=True,
)
async def f_translate(
    _: Client,
    message: Message,
):

    value = (
        message.arguments
        or ""
    ).strip()

    # =====================
    # 回复消息直接翻译
    # =====================

    if not value:

        return await _translate_reply(
            message
        )

    lower_value = (
        value.lower()
    )

    # =====================
    # 帮助菜单
    # =====================

    if lower_value == "help":

        await message.edit(
            "回复消息翻译：\n"
            "`，f`\n\n"
            "设置 DeepL Key：\n"
            "`，f key 你的Key`\n\n"
            "清除 DeepL Key：\n"
            "`，f clear`",
            parse_mode=enums.ParseMode.MARKDOWN,
        )

        await asyncio.sleep(
            20
        )

        return await message.delete()

    # =====================
    # 清除 DeepL Key
    # =====================

    if lower_value == "clear":

        _clear_deepl_key()

        await _edit_plain(
            message,
            "已清除 DeepL Key，将使用 Google 翻译。"
        )

        await asyncio.sleep(
            3
        )

        return await message.delete()

    # =====================
    # 输入 key 但没提供 Key
    # =====================

    if lower_value == "key":

        return await message.edit(
            "设置 DeepL Key：\n"
            "`，f key YOUR_KEY`",
            parse_mode=enums.ParseMode.MARKDOWN,
        )

    # =====================
    # 设置 DeepL Key
    # =====================

    if lower_value.startswith(
        "key "
    ):

        auth_key = (
            value[4:]
            .strip()
        )

    else:

        # 兼容旧格式：
        # ，f YOUR_DEEPL_KEY
        auth_key = value

    if (
        len(auth_key) < 20
        or any(
            character.isspace()
            for character
            in auth_key
        )
    ):

        return await message.edit(
            "DeepL API Key 格式无效。\n\n"
            "正确格式：\n"
            "`，f key 你的KEY`",
            parse_mode=enums.ParseMode.MARKDOWN,
        )

    _save_deepl_key(
        auth_key
    )

    await _edit_plain(
        message,
        "DeepL Key 已保存，后续翻译将优先使用 DeepL。"
    )

    await asyncio.sleep(
        3
    )

    return await message.delete()