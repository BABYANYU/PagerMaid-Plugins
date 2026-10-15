"""水獭服务比价：,gjg / ,cjg，分别展示最低价前 10 家。

分别读取两个商品页面中的公开报价快照，价格从低到高排列。
不访问商家账户、不下单；无需新增 Python 包。
"""

import asyncio
import html
import json
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser

from pagermaid.listener import listener
from pyrogram.enums import ParseMode

SERVICES = {
    "gpt": ("gpt_plus_topup", "ChatGPT Plus 月付直充"),
    "claude": ("claude_pro", "Claude Pro"),
}
HELP = (
    "<b>✦ AI订阅价格</b>\n\n"
    "<blockquote>✦ <code>,gjg</code>　ChatGPT Plus 月付直充\n\n"
    "✦ <code>,cjg</code>　Claude Pro</blockquote>\n\n"
    "价格升序 · 展示最低价前 10 家"
)
RUNNING = set()


class PriceError(Exception):
    pass


class BootDataParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.collect = False
        self.parts = []
        self.found = False

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            attrs = dict(attrs)
            self.collect = attrs.get("id") == "boot-data" and attrs.get("type") == "application/json"
            if self.collect:
                self.found = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.collect = False

    def handle_data(self, data):
        if self.collect:
            self.parts.append(data)


def parse_page(document, sku):
    parser = BootDataParser()
    parser.feed(document)
    if not parser.found:
        raise PriceError("页面没有返回报价数据，可能网站验证了访问，或页面格式发生变化。")
    try:
        data = json.loads("".join(parser.parts), parse_float=Decimal)
    except ValueError:
        raise PriceError("页面报价数据无法解析，请稍后重试。") from None
    if not isinstance(data, dict) or not isinstance(data.get("shop_floors"), list):
        raise PriceError("网站报价格式发生变化，请更新插件。")
    rows = []
    for quote in data["shop_floors"]:
        if not isinstance(quote, dict) or quote.get("sku") != sku or quote.get("live") is False:
            continue
        price = quote.get("price")
        if isinstance(price, bool) or price is None:
            continue
        try:
            price = Decimal(str(price))
        except (InvalidOperation, ValueError):
            continue
        if not price.is_finite() or price <= 0:
            continue
        row = dict(quote)
        row["price"] = price
        rows.append(row)
    rows.sort(key=lambda row: (row["price"], str(row.get("shop_name") or "")))
    if not rows:
        raise PriceError("当前页面没有该服务的有效报价，请稍后重试。")
    updated = data.get("stats", {}).get("updated") if isinstance(data.get("stats"), dict) else None
    return rows, str(updated or data.get("generated_at") or "未提供")


def fetch_page(sku):
    url = "https://shuita.xyz/sku/" + sku
    req = urllib.request.Request(url, headers={
        "User-Agent": "PagerMaid-PriceLookup/1.0", "Accept": "text/html",
        "Cache-Control": "no-cache",
    })
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            raw = response.read(5 * 1024 * 1024 + 1)
        if len(raw) > 5 * 1024 * 1024:
            raise PriceError("网页数据过大，请稍后重试。")
        return parse_page(raw.decode("utf-8-sig"), sku)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise PriceError("网站请求失败（HTTP %s），请稍后重试。" % exc.code) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise PriceError("连接失败，请检查 VPS 能否访问 shuita.xyz。") from None
    except UnicodeError:
        raise PriceError("网页编码发生变化，请更新插件。") from None


def escaped(value, limit):
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        text = text[:limit - 1] + "…"
    return html.escape(text)


def safe_url(value):
    if not isinstance(value, str) or len(value) > 700 or any(c.isspace() for c in value):
        return None
    try:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
            return None
    except ValueError:
        return None
    return html.escape(value, quote=True)


def quote_text(row, index):
    name = escaped(row.get("shop_name") or "未命名店铺", 45)
    link = safe_url(row.get("item_url")) or safe_url(row.get("shop_url"))
    shop = '<a href="%s">%s</a>' % (link, name) if link else name
    price = format(row["price"], "f")
    if "." in price:
        price = price.rstrip("0").rstrip(".")
    return "%02d · <b>¥%s</b>\n%s" % (index, price, shop)


def render_pages(service, rows, updated):
    _, title = SERVICES[service]
    selected = rows[:10]
    # 超长消息按完整报价拆分，保持所有标签完整。
    groups, current, current_size = [], [], 0
    for index, row in enumerate(selected, 1):
        text = quote_text(row, index)
        size = len(text.encode("utf-16-le")) // 2
        if current and current_size + size > 3100:
            groups.append(current)
            current, current_size = [], 0
        current.append(text)
        current_size += size + 2
    if current:
        groups.append(current)
    pages = []
    for number, group in enumerate(groups, 1):
        header = "<b>" + html.escape(title) + "</b>\n\n价格升序 · %s/%s 家" % (len(selected), len(rows))
        if len(groups) > 1:
            header += " · %s/%s" % (number, len(groups))
        content = header + "\n\n" + "\n\n".join(group)
        pages.append(content)
    return pages


async def price_lookup(client, message, service):
    args = getattr(message, "parameter", None) or []
    if isinstance(args, str):
        args = args.split()
    if args:
        return await message.edit(HELP, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    key = message.chat.id
    if key in RUNNING:
        return await message.edit("当前聊天已有比价查询，请稍后重试。", parse_mode=ParseMode.DISABLED)
    RUNNING.add(key)
    try:
        await message.edit("正在查询 " + SERVICES[service][1] + "…", parse_mode=ParseMode.DISABLED)
        rows, updated = await asyncio.to_thread(fetch_page, SERVICES[service][0])
        pages = render_pages(service, rows, updated)
        await message.edit(pages[0], parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        for page in pages[1:]:
            options = {"parse_mode": ParseMode.HTML, "disable_web_page_preview": True}
            thread_id = getattr(message, "message_thread_id", None)
            if thread_id is not None:
                options["message_thread_id"] = thread_id
            await client.send_message(message.chat.id, page, **options)
    except PriceError as exc:
        await message.edit(str(exc), parse_mode=ParseMode.DISABLED)
    except Exception as exc:
        await message.edit("比价查询未完成 · " + type(exc).__name__, parse_mode=ParseMode.DISABLED)
    finally:
        RUNNING.discard(key)


@listener(command="gjg", outgoing=True, ignore_edited=True,
          description="GPT Plus 月付直充价格，最低价前 10 家。")
async def gpt_price_lookup(client, message):
    await price_lookup(client, message, "gpt")


@listener(command="cjg", outgoing=True, ignore_edited=True,
          description="Claude Pro 价格，最低价前 10 家。")
async def claude_price_lookup(client, message):
    await price_lookup(client, message, "claude")


@listener(command="jg", outgoing=True, ignore_edited=True,
          description="AI订阅价格查询使用说明。")
async def price_menu(message):
    await message.edit(HELP, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
