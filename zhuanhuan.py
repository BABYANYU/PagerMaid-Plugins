import asyncio
import re
from pathlib import Path

from pyrogram.enums import ParseMode
from pyrogram.types import Message

from pagermaid.listener import listener
from pagermaid.services import client as http_client
from pagermaid.static import working_dir
from pagermaid.utils import alias_command

PUBLISH_URL = "https://sub.fakeyou.eu.org/api/plugin/publish"
TOKEN_PATH = Path(working_dir) / "data" / "yanyu_sub_plugin.token"
SHORT_URL = re.compile(r"https://sub\.fakeyou\.eu\.org/s/[A-Za-z0-9]{8}")
SOURCE_URL = re.compile(r"(?<![\w+.-])[a-z][a-z0-9+.-]*://[^\s<>\"'\x60]+", re.I)


async def publish(target, sources, token):
    response = await http_client.post(
        PUBLISH_URL,
        json={"target": target, "sources": sources},
        headers={"Authorization": "Bearer " + token, "User-Agent": "YanyuSub-PagerMaid/1.0"},
        timeout=30,
        follow_redirects=False,
    )
    if response.status_code != 200:
        if response.status_code == 422:
            raise ValueError("来源不可读取或格式不兼容")
        if response.status_code == 429:
            raise ValueError("请求过于频繁或短链数量已达上限")
        raise ValueError(f"发布失败（HTTP {response.status_code}）")
    data = response.json()
    url = data.get("url") if isinstance(data, dict) else None
    if not isinstance(url, str) or not SHORT_URL.fullmatch(url):
        raise ValueError("服务未返回有效订阅链接")
    return url


@listener(is_plugin=True, outgoing=True, command=alias_command("z"),
          description="生成 Mihomo 与 Surge 订阅链接", parameters="<url / node>")
async def convert_all(_, msg: Message):
    reply = msg.reply_to_message
    raw = (reply and (reply.caption or reply.text)) or msg.caption or msg.text or ""
    links = list(dict.fromkeys(SOURCE_URL.findall(raw)))
    options = {"parse_mode": ParseMode.HTML, "disable_web_page_preview": True}
    if not links:
        await msg.edit("未找到可转换的节点或订阅链接。", **options)
        return
    sources = [link for link in links if link.lower().startswith(("https://", "http://"))]
    nodes = [link for link in links if not link.lower().startswith(("https://", "http://"))]
    if nodes:
        sources.append("\n".join(nodes))
    if len(sources) > 4:
        await msg.edit("每次最多转换 4 个来源。", **options)
        return
    try:
        token = TOKEN_PATH.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        token = ""
    if not re.fullmatch(r"[0-9a-f]{64}", token):
        await msg.edit("未配置 YANYU Sub 插件令牌。", **options)
        return

    targets = ("Mihomo", "Surge")
    results = await asyncio.gather(*(publish(target, sources, token) for target in targets),
                                   return_exceptions=True)
    rows = []
    for target, result in zip(targets, results):
        if isinstance(result, str):
            rows.append(f'<a href="{result}">[{target}]</a>')
        else:
            detail = str(result) if type(result) is ValueError else "转换服务暂时不可用"
            rows.append(f"{target} · {detail}")
    separator = "  " if all(isinstance(result, str) for result in results) else "\n"
    await msg.edit(separator.join(rows), **options)
