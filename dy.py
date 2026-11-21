"""订阅地区价格：,dy 产品名，Find Cheap Subs 数据源，最低价前十名。
只使用标准库和 PagerMaid 已有依赖，无需额外包或密钥。
不同购买项目单独排名，未知周期不擅自标为月付或年付。
"""
import asyncio
import html
import json
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, quote
from pagermaid.listener import listener
from pyrogram.enums import ParseMode

FX_URL = "https://open.er-api.com/v6/latest/EUR"
TZ = timezone(timedelta(hours=8))
TIMEOUT = 25
RUNNING = set()
CACHE = {}

COUNTRIES = dict(zip(
    "AL AR AT BA BE BG BO BR CA CH CL CO CR CY CZ DE DK DO EC EE EG ES FI FR GB GR GT HN HR HU IE IN IT LT LU LV MD MK MT MX NG NI NL NO PA PE PH PK PL PT PY RO RS SE SI SK SV TR UA US UY VE VN ZA".split(),
    "阿尔巴尼亚 阿根廷 奥地利 波黑 比利时 保加利亚 玻利维亚 巴西 加拿大 瑞士 智利 哥伦比亚 哥斯达黎加 塞浦路斯 捷克 德国 丹麦 多米尼加 厄瓜多尔 爱沙尼亚 埃及 西班牙 芬兰 法国 英国 希腊 危地马拉 洪都拉斯 克罗地亚 匈牙利 爱尔兰 印度 意大利 立陶宛 卢森堡 拉脱维亚 摩尔多瓦 北马其顿 马耳他 墨西哥 尼日利亚 尼加拉瓜 荷兰 挪威 巴拿马 秘鲁 菲律宾 巴基斯坦 波兰 葡萄牙 巴拉圭 罗马尼亚 塞尔维亚 瑞典 斯洛文尼亚 斯洛伐克 萨尔瓦多 土耳其 乌克兰 美国 乌拉圭 委内瑞拉 越南 南非".split(),
))

def positive_decimal(value):
    try:
        number = Decimal(str(value))
        if number.is_finite() and 0 < number < Decimal("1e15"):
            return number
    except (InvalidOperation, ValueError, TypeError):
        pass
    raise ValueError("无效金额")

def fetch_bytes(url, limit):
    stage = "汇率接口" if url.startswith(FX_URL) else "应用搜索接口" if "/api/o/apps?" in url else "应用价格页面"
    request = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    for attempt in range(2):
        try:
            with urlopen(request, timeout=TIMEOUT) as response:
                data = response.read(limit + 1)
            break
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if attempt == 0 and status in (429, 500, 502, 503, 504):
                time.sleep(1)
                continue
            hint = {403: "该服务器出口被拒绝访问", 404: "请求页面不存在或尚未收录",
                    429: "请求过于频繁，请稍后重试"}.get(status, "网站暂时返回错误，请稍后重试")
            raise PriceError(f"{stage} · HTTP {status}\n{hint}。") from None
        except URLError:
            raise PriceError(f"{stage}连接失败，请检查服务器网络、DNS 或证书。") from None
    if len(data) > limit:
        raise PriceError("数据源返回内容过大，已停止读取。")
    return data

def parse_fx(data):
    try:
        payload = json.loads(data)
        if payload.get("result") != "success" or payload.get("base_code") != "EUR":
            raise ValueError
        rates = {key: positive_decimal(value) for key, value in payload["rates"].items()}
        positive_decimal(rates["CNY"])
        if rates.get("EUR") != 1:
            raise ValueError
        stamp = float(payload["time_last_update_unix"])
        # 拒绝过期或未来异常的汇率，避免以旧汇率冒充当日换算。
        age = time.time() - stamp
        if not -3600 <= age <= 3 * 86400:
            raise ValueError
        date = datetime.fromtimestamp(stamp, TZ).date()
        return rates, date, stamp
    except (KeyError, ValueError, TypeError, OverflowError, AttributeError):
        raise PriceError("汇率数据暂不可用，请稍后重试。") from None

def fetch_fx():
    cached = CACHE.get("fx")
    if cached and time.monotonic() - cached[0] < 86400 and time.time() - cached[1][2] < 3 * 86400:
        return cached[1]
    value = parse_fx(fetch_bytes(FX_URL, 256 * 1024))
    CACHE["fx"] = (time.monotonic(), value)
    return value

BASE_URL = "https://www.findcheapsubs.com"
HELP = (
    "<b>✦ 订阅地区价格</b>\n\n"
    "<code>,dy 产品名</code>　查看购买项目\n\n"
    "<code>,dy 产品名 序号</code>　查询低价地区前 10 名\n\n"
    "支持 App 名称、App Store 链接或 App ID"
)
ALIASES = {"油管": "YouTube", "奈飞": "Netflix", "网飞": "Netflix", "gpt": "ChatGPT",
           "微软": "Microsoft 365", "office": "Microsoft 365", "迪士尼": "Disney",
           "苹果音乐": "Apple Music"}
KNOWN_APPS = {
    "youtube": ("544007664", "YouTube", "youtube"),
    "surge": ("1442620678", "Surge 5", "surge-5"),
    "netflix": ("363590051", "Netflix", "netflix"),
    "chatgpt": ("6448311069", "ChatGPT", "chatgpt"),
    "claude": ("6473753684", "Claude by Anthropic", "claude-by-anthropic"),
}
COUNTRIES.update({"AU": "澳大利亚", "CN": "中国大陆", "HK": "中国香港", "TW": "中国台湾",
                  "JP": "日本", "KR": "韩国", "ID": "印度尼西亚", "MY": "马来西亚",
                  "SG": "新加坡", "TH": "泰国", "KZ": "哈萨克斯坦", "QA": "卡塔尔",
                  "RU": "俄罗斯", "AE": "阿联酋", "SA": "沙特阿拉伯", "NZ": "新西兰"})
MAX_SEARCH_PAGES = 5


class PriceError(Exception):
    pass


class FlightParser(HTMLParser):
    """读取公开页面附带的 JSON 数据；不执行网页中的 JavaScript。"""
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.in_script = False
        self.parts = []
        self.scripts = []
        self.texts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.in_script = True
            self.parts = []

    def handle_data(self, text):
        if self.in_script:
            self.parts.append(text)
        else:
            self.texts.append(text)

    def handle_endtag(self, tag):
        if tag == "script":
            self.scripts.append("".join(self.parts))
            self.in_script = False


def find_app_data(value):
    if isinstance(value, dict):
        if isinstance(value.get("iapList"), list) and value.get("platformId"):
            return value
        for item in value.values():
            result = find_app_data(item)
            if result is not None:
                return result
    elif isinstance(value, list):
        for item in value:
            result = find_app_data(item)
            if result is not None:
                return result
    return None


def parse_detail(document, app_id):
    parser = FlightParser()
    parser.feed(document)
    chunks = []
    decoder = json.JSONDecoder()
    for script in parser.scripts:
        for match in re.finditer(r"self\.__next_f\.push\(", script):
            try:
                value, _ = decoder.raw_decode(script[match.end():])
                if isinstance(value, list) and len(value) == 2 and value[0] == 1 and isinstance(value[1], str):
                    chunks.append(value[1])
            except ValueError:
                continue
    flight = "".join(chunks)
    app = None
    for match in re.finditer(r"(?m)^[0-9a-f]+:", flight):
        try:
            value, _ = decoder.raw_decode(flight[match.end():])
            app = find_app_data(value)
            if app is not None:
                break
        except (ValueError, RecursionError):
            continue
    if not app or str(app.get("platformId", "")).removeprefix("id") != app_id:
        raise PriceError("未读取到该应用的价格数据，网站可能尚未收录或页面结构已变化。")
    # 页面日期只作为页面标注保留，不能当作每项价格的实际核验日期。
    visible = " ".join(parser.texts)
    stamp = re.search(r"Last Updated\s*-\s*([A-Za-z]{3} \d{1,2}, \d{4})", visible)
    app = dict(app, page_date=None)
    if stamp:
        try:
            months = {m: i for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}
            month, day, year = re.fullmatch(r"(\w+) (\d+), (\d+)", stamp[1]).groups()
            app["page_date"] = datetime(int(year), months[month], int(day)).date().isoformat()
        except (ValueError, KeyError):
            pass
    return app


def normal_name(value):
    return re.sub(r"[\W_]+", "", value.casefold())


def app_id_from_query(query):
    direct = re.fullmatch(r"(?:id)?(\d{6,12})", query.strip(), re.I)
    if direct:
        return direct[1]
    url = urlsplit(query)
    if url.scheme == "https" and url.hostname == "apps.apple.com":
        match = re.search(r"/id(\d{6,12})(?:/|$)", url.path)
        if match:
            return match[1]
    return None


def search_apps(query):
    query = ALIASES.get(query.casefold(), query)
    known = KNOWN_APPS.get(normal_name(query))
    if known:
        app_id, name, slug = known
        item = dict(app_id=app_id, appName=name, appFormatName=slug, platformId="id" + app_id)
        CACHE["metadata:" + app_id] = item
        return [item]
    key = "search:" + query.casefold()
    cached = CACHE.get(key)
    if cached and time.monotonic() - cached[0] < 600:
        return cached[1]
    matches = {}
    for page in range(1, MAX_SEARCH_PAGES + 1):
        params = urlencode(dict(page=page, pageSize=30, search=query))
        payload = json.loads(fetch_bytes(BASE_URL + "/api/o/apps?" + params, 2 * 1024 * 1024))
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise PriceError("网站搜索暂不可用，请稍后重试。")
        for item in payload["data"]:
            if not isinstance(item, dict):
                continue
            if item.get("platform") != "APP_STORE":
                continue
            app_id = app_id_from_query(str(item.get("platformId", "")))
            if app_id and isinstance(item.get("appName"), str):
                matches[app_id] = dict(item, app_id=app_id)
                CACHE["metadata:" + app_id] = matches[app_id]
        if page >= int(payload.get("totalPages", 1)):
            break
    rows = list(matches.values())
    wanted = normal_name(query)
    exact = [row for row in rows if normal_name(row["appName"]) == wanted]
    # Surge 应匹配 Surge 5，不误选 MarketSurge 或其他同名应用。
    versioned = [row for row in rows if re.sub(r"\d+$", "", normal_name(row["appName"])) == wanted]
    best = exact or versioned
    if len(best) == 1:
        rows = best
    CACHE[key] = (time.monotonic(), rows)
    return rows


def fetch_app(app_id):
    if not re.fullmatch(r"\d{6,12}", app_id):
        raise PriceError("无效的 App ID。")
    key = "app:" + app_id
    cached = CACHE.get(key)
    if cached and time.monotonic() - cached[0] < 300:
        return cached[1]
    metadata = CACHE.get("metadata:" + app_id, {})
    slug = metadata.get("appFormatName", "")
    if not slug:
        slug = next((value[2] for value in KNOWN_APPS.values() if value[0] == app_id), "")
    # 常见应用使用真实路径，避免 app-price 的额外重定向。
    page_name = quote(slug, safe="-") + "-price" if isinstance(slug, str) and slug else "app-price"
    document = fetch_bytes(BASE_URL + "/app/" + app_id + "/" + page_name, 6 * 1024 * 1024).decode("utf-8")
    app = parse_detail(document, app_id)
    CACHE[key] = (time.monotonic(), app)
    return app


def parse_local_amount(value, decimal):
    if decimal not in (".", ",") or not isinstance(value, str):
        raise ValueError("无效的本地价格")
    # 各地区千位与小数分隔符不同，使用来源提供的小数符号。
    if re.search(r"[-−]\s*\d", value):
        raise ValueError("负数价格")
    match = re.search(r"[0-9](?:.*[0-9])?", value)
    if not match:
        raise ValueError("未找到价格")
    # 仅检查数字区间，忽略货币名称末尾的句点（如 kr.）。
    numeric = re.sub(r"\s+", "", match[0])
    if not re.fullmatch(r"[0-9.,]+", numeric) or numeric.count(decimal) > 1:
        raise ValueError("价格格式不明")
    grouping = "," if decimal == "." else "."
    integer, _, fraction = numeric.partition(decimal)
    if grouping in fraction or (grouping in integer and not re.fullmatch(r"[0-9]{1,3}(?:" + re.escape(grouping) + r"[0-9]{3})+", integer)):
        raise ValueError("千位格式与小数元数据不符")
    return positive_decimal(numeric.replace(grouping, "").replace(decimal, "."))


def purchase_choices(app):
    choices = []
    youtube = str(app["platformId"]) == "id544007664"
    allowed_youtube = {
        "youtube premium", "youtube premium family", "youtube premium two-person",
        "premium lite", "youtube music", "youtube music family",
    }
    for item in app["iapList"]:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        if not isinstance(title, str) or not title.strip() or not isinstance(item.get("priceList"), list):
            continue
        if youtube and title.strip().casefold() not in allowed_youtube:
            continue
        if re.search(r"super\s*chat|super\s*sticker|\bcoins?\b|\bgems?\b|\bcredits?\b|\btokens?\b|\bdonation\b", title, re.I):
            continue
        buckets = {}
        for raw in item["priceList"]:
            if not isinstance(raw, dict):
                continue
            code = str(raw.get("countryCode", "")).upper()
            currency = raw.get("currencyCode", "")
            if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{2}", code) or not re.fullmatch(r"[A-Z]{3}", currency):
                continue
            try:
                amount = parse_local_amount(raw.get("price"), raw.get("decimal"))
            except (ValueError, TypeError):
                continue
            buckets.setdefault(code, {})[(currency, amount)] = dict(code=code, currency=currency, amount=amount)
        # 同项目在同一地区出现不同金额时，不能确认 SKU，排除该地区而非取最低价。
        rows = [next(iter(group.values())) for group in buckets.values() if len(group) == 1]
        if rows:
            choices.append(dict(title=title.strip()[:120], rows=rows))
    if not choices:
        raise PriceError("网站已收录该应用，但暂未提供可确认的订阅或内购价格。")
    youtube_order = {"youtube premium": 0, "youtube premium family": 1,
                     "youtube premium two-person": 2, "premium lite": 3,
                     "youtube music": 4, "youtube music family": 5}
    # 主菜单优先展示覆盖地区较多的项目，避免少数地区的翻译名称占满第一页。
    choices.sort(key=lambda item: (
        youtube_order.get(item["title"].casefold(), 99) if youtube else 0,
        -len(item["rows"]), item["title"].casefold(), item["title"],
    ))
    return choices


def display_title(app, title):
    if str(app["platformId"]) == "id1442620678":
        return {"Feature Subscription": "功能更新订阅", "Surge Pro": "Surge Pro 解锁"}.get(title, title)
    return title


def render_menu(app, choices, query, page=1):
    title = html.escape(str(app["appName"])[:90])
    # 通常直接复用用户输入；多词产品名称同样可以加末尾序号。
    if len(query) > 50 or not re.fullmatch(r"[\w +.-]+", query):
        query = str(app["platformId"])
    safe_query = html.escape(query)
    lines = [f"<b>✦ {title}</b>"]
    size = 12
    for index in range((page - 1) * size, min(page * size, len(choices))):
        item = choices[index]
        name = html.escape(display_title(app, item["title"]))
        lines.append(f'<code>,dy {safe_query} {index + 1}</code>　{name}')
    pages = (len(choices) + size - 1) // size
    if pages > 1:
        lines.append(f'项目 {page}/{pages} 页 · <code>,dy {safe_query} menu {page % pages + 1}</code>　下一页')
    return "\n\n".join(lines)


def rank_prices(rows, rates):
    ranked = []
    for original in rows:
        currency = original["currency"]
        if currency not in rates:
            raise PriceError(f"缺少 {currency} 汇率，暂时无法生成完整排名。")
        ranked.append(dict(original, cny=original["amount"] * rates["CNY"] / rates[currency]))
    return sorted(ranked, key=lambda row: (row["cny"], row["code"]))[:10]


def render_result(app, choice, rows, fx_date):
    name = html.escape(str(app["appName"])[:90])
    title = html.escape(display_title(app, choice["title"]))
    lines = [f"<b>{name} · {title}</b>", f"从低到高 · 前 {len(rows)} 名"]
    for index, row in enumerate(rows, 1):
        flag = "".join(chr(127397 + ord(char)) for char in row["code"])
        country = html.escape(COUNTRIES.get(row["code"], row["code"]))
        local = f'{row["amount"]:,.2f}'.rstrip("0").rstrip(".")
        lines.append(f'{index:02d} · {flag} <b>{country}</b>\n'
                     f'<b>¥{row["cny"]:.2f}</b>（{row["currency"]} {local}）')
    return "\n\n".join(lines)


def render_matches(rows):
    lines = ["<b>✦ 请选择应用</b>"]
    for row in rows[:6]:
        name = html.escape(row["appName"][:90])
        seller = html.escape(str(row.get("seller", ""))[:80])
        lines.append(f'<b>{name}</b>\n{seller}\n<code>,dy id{row["app_id"]}</code>')
    lines.append("可直接输入 App Store 链接以准确匹配。")
    return "\n\n".join(lines)


@listener(command="dy", outgoing=True, ignore_edited=True,
          description="查询 App 订阅及内购项目低价地区前十名。",
          parameters="[产品名 [项目序号]]")
async def subscription_prices(client, message):
    args = getattr(message, "parameter", None) or []
    args = args.split() if isinstance(args, str) else list(args)
    options = dict(parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    if not args or args[0].casefold() in ("help", "menu"):
        return await message.edit(HELP, **options)
    key = message.chat.id
    if key in RUNNING:
        return await message.edit("当前聊天已有价格查询，请稍后重试。", **options)
    RUNNING.add(key)
    try:
        page = 1
        selection = None
        explicit_menu = len(args) > 2 and args[-2].casefold() == "menu" and args[-1].isascii() and args[-1].isdigit()
        if explicit_menu:
            page = int(args[-1])
            args = args[:-2]
        query = " ".join(args).strip()
        # 已缓存的完整应用名（含数字）优先，避免将 Microsoft 365 或 Surge 5 当序号。
        full_cached = CACHE.get("search:" + ALIASES.get(query.casefold(), query).casefold())
        numeric_name = normal_name(query) in ("surge5", "microsoft365", "office365", "1password")
        if not explicit_menu and len(args) > 1 and args[-1].isascii() and args[-1].isdigit() and not numeric_name and not (full_cached and len(full_cached[1]) == 1):
            selection = int(args.pop())
            query = " ".join(args).strip()
        if not query or len(query) > 400:
            raise PriceError("请输入产品名称、App Store 链接或 App ID。")
        app_id = app_id_from_query(query)
        if not app_id:
            matches = await asyncio.to_thread(search_apps, query)
            if not matches:
                raise PriceError("该网站暂未收录此名称，或名称无法匹配。可尝试英文名或 App Store 链接。")
            if len(matches) != 1:
                return await message.edit(render_matches(matches), **options)
            app_id = matches[0]["app_id"]
        app = await asyncio.to_thread(fetch_app, app_id)
        choices = purchase_choices(app)
        pages = (len(choices) + 11) // 12
        if not 1 <= page <= pages:
            raise PriceError("菜单页码无效。")
        if selection is None:
            return await message.edit(render_menu(app, choices, query, page), **options)
        if not 1 <= selection <= len(choices):
            return await message.edit("项目序号无效，请重新选择。\n\n" + render_menu(app, choices, query), **options)
        choice = choices[selection - 1]
        fx = await asyncio.to_thread(fetch_fx)
        rows = rank_prices(choice["rows"], fx[0])
        await message.edit(render_result(app, choice, rows, fx[1]), **options)
    except PriceError as exc:
        await message.edit(html.escape(str(exc)), **options)
    except Exception as exc:
        await message.edit(f"订阅价格查询未完成 · {type(exc).__name__}，请稍后重试。", **options)
    finally:
        RUNNING.discard(key)
