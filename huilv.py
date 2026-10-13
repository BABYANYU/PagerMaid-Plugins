"""PagerMaid-Pyro 智能汇率查询插件。

由 TeleBox rate 插件适配：
https://github.com/TeleBoxOrg/TeleBox-Plugins/blob/main/rate/rate.ts

安装后使用：,h help
"""

import contextlib
import html
import math
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import httpx
from pyrogram.enums import ParseMode

from pagermaid.enums import Message
from pagermaid.listener import listener
from pagermaid.utils import logs


TIME_ZONE = ZoneInfo("Asia/Shanghai")
HTTP_TIMEOUT = 8.0
FIAT_CACHE_SECONDS = 300
PRICE_CACHE_SECONDS = 15


# 原插件内置法币表的代码集合；加入部分现行 ISO 代码作为兼容。
FIAT_CODES = set(
    """
    usd eur gbp jpy cny cad aud chf nzd sek nok dkk isk pln czk huf ron bgn
    hrk rsd bam mkd all rub uah byn mdl try gel amd azn brl mxn ars cop pen
    clp uyu pyg bob ves gyd srd ttd jmd bbd bsd bzd crc gtq hnl nio pab dop
    htg cup sgd hkd krw inr thb myr php idr vnd lak khr mmk bnd twd mop fjd
    pgk sbd vuv top wst lkr pkr bdt npr btn mvr afn kzt uzs kgs tjs tmt mnt zar
    ils aed sar qar kwd bhd omr jod lbp syp iqd irr yer egp mad dzd tnd lyd
    sdg etb ern djf sos ngn ghs xof sll lrd gmd gnf cve kes ugx tzs rwf bif
    mzn mwk zmw zwl mga mur scr kmf xaf cdf aoa stn bwp nad szl lsl
    """.split()
)

ALIASES = {
    "rmb": "cny",
    "yuan": "cny",
    "cnh": "cny",
    "rm": "myr",
    "tmm": "tmt",
    "cvs": "cve",
    "std": "stn",
    "gqe": "xaf",
    "zwd": "zwl",
    "bitcoin": "btc",
    "ethereum": "eth",
    "tether": "usdt",
    "binancecoin": "bnb",
    "solana": "sol",
    "usd-coin": "usdc",
    "ripple": "xrp",
    "dogecoin": "doge",
    "toncoin": "ton",
    "cardano": "ada",
    "shiba-inu": "shib",
    "avalanche-2": "avax",
    "tron": "trx",
    "polkadot": "dot",
    "chainlink": "link",
    "matic-network": "matic",
    "wrapped-bitcoin": "wbtc",
    "litecoin": "ltc",
    "bitcoin-cash": "bch",
    "uniswap": "uni",
    "cosmos": "atom",
    "ethereum-classic": "etc",
    "stellar": "xlm",
    "internet-computer": "icp",
    "filecoin": "fil",
    "hedera-hashgraph": "hbar",
    "lido-dao": "ldo",
    "curve-dao-token": "crv",
    "arbitrum": "arb",
}

# 中文货币名称及常用叫法。
CURRENCY_NAME_ALIASES = {
    "人民币": "cny", "人民币元": "cny", "中国元": "cny", "元": "cny",
    "美元": "usd", "美金": "usd", "欧元": "eur", "英镑": "gbp",
    "日元": "jpy", "日币": "jpy", "加元": "cad", "加拿大元": "cad",
    "澳元": "aud", "澳币": "aud", "新西兰元": "nzd", "纽元": "nzd",
    "瑞士法郎": "chf", "港元": "hkd", "港币": "hkd",
    "澳门元": "mop", "澳门币": "mop", "新台币": "twd", "台币": "twd",
    "韩元": "krw", "韩币": "krw", "印度卢比": "inr", "卢比": "inr",
    "泰铢": "thb", "新加坡元": "sgd", "新币": "sgd",
    "马来西亚林吉特": "myr", "林吉特": "myr", "马币": "myr",
    "菲律宾比索": "php", "印尼盾": "idr", "印度尼西亚盾": "idr",
    "越南盾": "vnd", "俄罗斯卢布": "rub", "卢布": "rub",
    "土耳其里拉": "try", "里拉": "try", "阿联酋迪拉姆": "aed",
    "迪拉姆": "aed", "沙特里亚尔": "sar", "南非兰特": "zar",
    "巴西雷亚尔": "brl", "墨西哥比索": "mxn",
}

# 国家或地区名称映射到当地主要流通货币。多个国家共用同一种货币时，
# 直接映射到相同代码；具有多种法定货币的地区采用日常最常用币种。
COUNTRY_CURRENCY_ALIASES = {
    # 中国及周边
    "中国": "cny", "中华人民共和国": "cny", "中国大陆": "cny", "大陆": "cny",
    "香港": "hkd", "中国香港": "hkd", "澳门": "mop", "中国澳门": "mop",
    "台湾": "twd", "中国台湾": "twd", "日本": "jpy", "韩国": "krw",
    "蒙古": "mnt", "印度": "inr", "巴基斯坦": "pkr", "孟加拉国": "bdt",
    "斯里兰卡": "lkr", "尼泊尔": "npr", "不丹": "btn", "马尔代夫": "mvr",
    "阿富汗": "afn", "哈萨克斯坦": "kzt", "乌兹别克斯坦": "uzs",
    "吉尔吉斯斯坦": "kgs", "塔吉克斯坦": "tjs", "土库曼斯坦": "tmt",
    # 东南亚及大洋洲
    "新加坡": "sgd", "马来西亚": "myr", "泰国": "thb", "印度尼西亚": "idr",
    "印尼": "idr", "菲律宾": "php", "越南": "vnd", "老挝": "lak",
    "柬埔寨": "khr", "缅甸": "mmk", "文莱": "bnd", "东帝汶": "usd",
    "澳大利亚": "aud", "澳洲": "aud", "新西兰": "nzd", "斐济": "fjd",
    "巴布亚新几内亚": "pgk", "萨摩亚": "wst", "汤加": "top",
    # 美洲
    "美国": "usd", "美利坚合众国": "usd", "加拿大": "cad", "墨西哥": "mxn",
    "巴西": "brl", "阿根廷": "ars", "智利": "clp", "哥伦比亚": "cop",
    "秘鲁": "pen", "玻利维亚": "bob", "乌拉圭": "uyu", "巴拉圭": "pyg",
    "委内瑞拉": "ves", "厄瓜多尔": "usd", "巴拿马": "pab",
    "哥斯达黎加": "crc", "危地马拉": "gtq", "洪都拉斯": "hnl",
    "尼加拉瓜": "nio", "多米尼加": "dop", "古巴": "cup", "牙买加": "jmd",
    # 欧洲
    "英国": "gbp", "大不列颠": "gbp", "欧盟": "eur", "欧元区": "eur",
    "德国": "eur", "法国": "eur", "意大利": "eur", "西班牙": "eur",
    "葡萄牙": "eur", "荷兰": "eur", "比利时": "eur", "奥地利": "eur",
    "爱尔兰": "eur", "芬兰": "eur", "希腊": "eur", "卢森堡": "eur",
    "克罗地亚": "eur", "瑞士": "chf", "挪威": "nok", "瑞典": "sek",
    "丹麦": "dkk", "冰岛": "isk", "波兰": "pln", "捷克": "czk",
    "匈牙利": "huf", "罗马尼亚": "ron", "保加利亚": "bgn",
    "塞尔维亚": "rsd", "乌克兰": "uah", "俄罗斯": "rub", "白俄罗斯": "byn",
    "摩尔多瓦": "mdl", "土耳其": "try", "格鲁吉亚": "gel",
    "亚美尼亚": "amd", "阿塞拜疆": "azn",
    # 中东
    "阿联酋": "aed", "阿拉伯联合酋长国": "aed", "沙特": "sar",
    "沙特阿拉伯": "sar", "卡塔尔": "qar", "科威特": "kwd", "巴林": "bhd",
    "阿曼": "omr", "约旦": "jod", "以色列": "ils", "黎巴嫩": "lbp",
    "叙利亚": "syp", "伊拉克": "iqd", "伊朗": "irr", "也门": "yer",
    # 非洲
    "埃及": "egp", "南非": "zar", "尼日利亚": "ngn", "加纳": "ghs",
    "肯尼亚": "kes", "埃塞俄比亚": "etb", "坦桑尼亚": "tzs", "乌干达": "ugx",
    "卢旺达": "rwf", "摩洛哥": "mad", "阿尔及利亚": "dzd", "突尼斯": "tnd",
    "利比亚": "lyd", "苏丹": "sdg", "索马里": "sos", "安哥拉": "aoa",
    "赞比亚": "zmw", "津巴布韦": "zwl", "博茨瓦纳": "bwp",
    "莫桑比克": "mzn", "马达加斯加": "mga", "毛里求斯": "mur",
    "塞舌尔": "scr", "刚果金": "cdf", "刚果民主共和国": "cdf",
    "喀麦隆": "xaf", "塞内加尔": "xof", "科特迪瓦": "xof",
}

ALIASES.update(CURRENCY_NAME_ALIASES)
ALIASES.update(COUNTRY_CURRENCY_ALIASES)

# CoinGecko ID 仅作为 Binance 不可用时的备用。
CRYPTO_IDS = {
    "btc": "bitcoin",
    "eth": "ethereum",
    "usdt": "tether",
    "bnb": "binancecoin",
    "sol": "solana",
    "usdc": "usd-coin",
    "xrp": "ripple",
    "doge": "dogecoin",
    "ton": "toncoin",
    "ada": "cardano",
    "shib": "shiba-inu",
    "avax": "avalanche-2",
    "trx": "tron",
    "dot": "polkadot",
    "link": "chainlink",
    "matic": "matic-network",
    "pol": "matic-network",
    "wbtc": "wrapped-bitcoin",
    "ltc": "litecoin",
    "bch": "bitcoin-cash",
    "uni": "uniswap",
    "atom": "cosmos",
    "etc": "ethereum-classic",
    "xlm": "stellar",
    "okb": "okb",
    "icp": "internet-computer",
    "fil": "filecoin",
    "hbar": "hedera-hashgraph",
    "ldo": "lido-dao",
    "crv": "curve-dao-token",
    "arb": "arbitrum",
}

STABLECOINS = {"USDT", "USDC", "BUSD", "DAI", "FDUSD", "TUSD"}
BINANCE_BASES = (
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://data-api.binance.vision",
)


HELP_HTML = """<b>汇率查询</b>

<b>使用示例</b>

支持输入货币代码、中文货币名称或国家/地区名称。

<code>,h USD</code>
美元→人民币

<code>,h USD CNY</code>
美元→人民币

<code>,h CNY TRY</code>
人民币→土耳其里拉

<code>,h 印度 人民币</code>
印度卢比→人民币

<code>,h 印度 中国</code>
印度卢比→人民币

<code>,h USD CNY 1</code>
1美元 → 人民币

<code>,h 1 CNY USDT </code>
1人民币 → USDT

数量可放在两个货币代码前后任意位置"""


@dataclass(frozen=True)
class Currency:
    code: str
    symbol: str
    kind: str


@dataclass(frozen=True)
class MarketPrice:
    price: float
    updated_at: datetime


fiat_rates_cache: Dict[str, Tuple[float, Dict[str, float]]] = {}
price_cache: Dict[str, Tuple[float, float]] = {}


async def edit_plain(message: Message, text: str) -> None:
    with contextlib.suppress(Exception):
        await message.edit(text, parse_mode=ParseMode.DISABLED)


async def edit_html(message: Message, text: str) -> None:
    with contextlib.suppress(Exception):
        await message.edit(text, parse_mode=ParseMode.HTML)


def normalize_code(value: str) -> str:
    code = (value or "").strip().lower()
    return ALIASES.get(code, code)


def parse_arguments(args: List[str]) -> Tuple[str, str, float]:
    amount = 1.0
    currencies: List[str] = []
    for raw in args:
        normalized = normalize_code(raw)
        try:
            number = float(normalized)
            if math.isfinite(number):
                amount = number
                continue
        except ValueError:
            pass
        currencies.append(normalized)
    return currencies[0] if currencies else "btc", currencies[1] if len(currencies) > 1 else "cny", amount


def identify_currency(code: str) -> Currency:
    normalized = normalize_code(code)
    kind = "fiat" if normalized in FIAT_CODES else "crypto"
    return Currency(normalized, normalized.upper(), kind)


async def get_json(url: str) -> Any:
    headers = {"User-Agent": "PagerMaid-Rate/1.0"}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True, headers=headers) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()


def normalize_rates(raw: Dict[str, Any]) -> Dict[str, float]:
    result: Dict[str, float] = {}
    for key, value in raw.items():
        try:
            number = float(value)
            if math.isfinite(number) and number > 0:
                result[str(key).lower()] = number
        except (TypeError, ValueError):
            continue
    return result


async def fetch_fiat_rates(base: str) -> Dict[str, float]:
    key = base.lower()
    cached = fiat_rates_cache.get(key)
    now = time.monotonic()
    if cached and now - cached[0] < FIAT_CACHE_SECONDS:
        return cached[1]

    endpoints = (
        f"https://open.er-api.com/v6/latest/{quote_plus(key.upper())}",
        f"https://api.frankfurter.app/latest?from={quote_plus(key.upper())}",
        f"https://api.coinbase.com/v2/exchange-rates?currency={quote_plus(key.upper())}",
        f"https://cdn.jsdelivr.net/gh/fawazahmed0/currency-api@1/latest/currencies/{quote_plus(key)}.json",
        f"https://api.exchangerate.host/latest?base={quote_plus(key.upper())}",
    )
    last_error = "没有服务返回有效数据"
    for url in endpoints:
        try:
            data = await get_json(url)
            raw_rates: Optional[Dict[str, Any]] = None
            if isinstance(data, dict) and isinstance(data.get("rates"), dict):
                raw_rates = data["rates"]
            elif isinstance(data, dict) and isinstance(data.get("data"), dict) and isinstance(data["data"].get("rates"), dict):
                raw_rates = data["data"]["rates"]
            elif isinstance(data, dict) and isinstance(data.get(key), dict):
                raw_rates = data[key]
            if raw_rates:
                rates = normalize_rates(raw_rates)
                rates[key] = 1.0
                if rates:
                    fiat_rates_cache[key] = (now, rates)
                    return rates
        except Exception as exc:
            last_error = str(exc)
    raise RuntimeError(f"法币汇率服务不可用：{last_error}")


async def fetch_binance_price(pair: str) -> float:
    symbol = pair.upper()
    cached = price_cache.get(symbol)
    now = time.monotonic()
    if cached and now - cached[0] < PRICE_CACHE_SECONDS:
        return cached[1]

    last_error = "交易对不存在"
    for base_url in BINANCE_BASES:
        try:
            data = await get_json(f"{base_url}/api/v3/ticker/price?symbol={quote_plus(symbol)}")
            price = float(data["price"])
            if math.isfinite(price) and price > 0:
                price_cache[symbol] = (now, price)
                return price
        except Exception as exc:
            last_error = str(exc)
    raise RuntimeError(f"Binance 无法获取 {symbol}：{last_error}")


async def fetch_coingecko_usd(symbol: str) -> float:
    code = symbol.lower()
    if symbol.upper() in STABLECOINS:
        return 1.0
    coin_id = CRYPTO_IDS.get(code)
    if not coin_id:
        raise RuntimeError(f"CoinGecko 不认识 {symbol}")
    data = await get_json(
        "https://api.coingecko.com/api/v3/simple/price"
        f"?ids={quote_plus(coin_id)}&vs_currencies=usd"
    )
    price = float(data[coin_id]["usd"])
    if not math.isfinite(price) or price <= 0:
        raise RuntimeError(f"CoinGecko 未返回 {symbol} 的有效价格")
    return price


async def crypto_usd_price(symbol: str) -> float:
    upper = symbol.upper()
    if upper in STABLECOINS:
        return 1.0
    for bridge in ("USDT", "USDC", "FDUSD"):
        try:
            return await fetch_binance_price(f"{upper}{bridge}")
        except Exception:
            continue
    return await fetch_coingecko_usd(upper)


async def crypto_to_crypto(source: str, target: str) -> MarketPrice:
    source, target = source.upper(), target.upper()
    if source == target:
        return MarketPrice(1.0, datetime.now(TIME_ZONE))
    try:
        return MarketPrice(await fetch_binance_price(f"{source}{target}"), datetime.now(TIME_ZONE))
    except Exception:
        pass
    try:
        reverse = await fetch_binance_price(f"{target}{source}")
        return MarketPrice(1.0 / reverse, datetime.now(TIME_ZONE))
    except Exception:
        pass
    source_usd = await crypto_usd_price(source)
    target_usd = await crypto_usd_price(target)
    return MarketPrice(source_usd / target_usd, datetime.now(TIME_ZONE))


async def crypto_to_fiat(crypto: str, fiat: str) -> MarketPrice:
    crypto, fiat = crypto.upper(), fiat.upper()
    usd_price = await crypto_usd_price(crypto)
    if fiat == "USD":
        return MarketPrice(usd_price, datetime.now(TIME_ZONE))
    rates = await fetch_fiat_rates("usd")
    fiat_rate = rates.get(fiat.lower())
    if not fiat_rate:
        raise RuntimeError(f"无法获取 USD 到 {fiat} 的汇率")
    return MarketPrice(usd_price * fiat_rate, datetime.now(TIME_ZONE))


async def universal_price(source: Currency, target: Currency) -> MarketPrice:
    if source.symbol == target.symbol:
        return MarketPrice(1.0, datetime.now(TIME_ZONE))
    if source.kind == "crypto" and target.kind == "crypto":
        return await crypto_to_crypto(source.symbol, target.symbol)
    if source.kind == "crypto" and target.kind == "fiat":
        return await crypto_to_fiat(source.symbol, target.symbol)
    if source.kind == "fiat" and target.kind == "crypto":
        reverse = await crypto_to_fiat(target.symbol, source.symbol)
        return MarketPrice(1.0 / reverse.price, reverse.updated_at)
    rates = await fetch_fiat_rates(source.code)
    rate = rates.get(target.code)
    if not rate:
        raise RuntimeError(f"无法获取 {source.symbol} 到 {target.symbol} 的汇率")
    return MarketPrice(rate, datetime.now(TIME_ZONE))


def format_price(value: float) -> str:
    if value >= 1:
        return f"{value:,.2f}"
    if value >= 0.01:
        return f"{value:.4f}"
    if value >= 0.0001:
        return f"{value:.6f}"
    return f"{value:.2e}"


def format_amount(value: float) -> str:
    return f"{value:,.2f}" if abs(value) >= 1 else f"{value:.6f}"


def build_response(source: Currency, target: Currency, amount: float, market: MarketPrice) -> str:
    converted = amount * market.price
    source_symbol = source.symbol
    target_symbol = target.symbol
    rate_source = source_symbol
    rate_target = target_symbol
    rate = market.price
    if source.kind == "fiat" and target.kind == "crypto":
        crypto_symbol = source_symbol if source.kind == "crypto" else target_symbol
        fiat_symbol = target_symbol if target.kind == "fiat" else source_symbol
        rate_source = crypto_symbol
        rate_target = fiat_symbol
        rate = 1.0 / market.price
    return (
        "汇率\n\n"
        f"{format_amount(amount)} {source_symbol} ≈\n\n"
        f"{format_amount(converted)} {target_symbol}\n\n"
        f"当前汇率: 1 {rate_source} = {format_price(rate)} {rate_target}"
    )


def google_fallback(base: str, quote: str, amount: float) -> str:
    query = quote_plus(f"{amount} {base.upper()} to {quote.upper()}")
    return f"https://www.google.com/search?q={query}"


@listener(
    is_plugin=True,
    outgoing=True,
    command="h",
    description="查询加密货币和法币汇率并进行数量换算。",
    parameters="<货币1> [货币2] [数量]",
)
async def rate(message: Message) -> None:
    args = list(message.parameter or [])
    if not args or args[0].lower() in {"help", "h"}:
        await edit_html(message, HELP_HTML)
        return

    base, quote, amount = parse_arguments(args)
    if amount < 0:
        await edit_plain(message, "数量不能小于 0。")
        return
    if not base or not quote:
        await edit_html(message, HELP_HTML)
        return

    source = identify_currency(base)
    target = identify_currency(quote)
    fallback = google_fallback(base, quote, amount)
    await edit_plain(message, "正在获取最新汇率数据……")

    try:
        market = await universal_price(source, target)
        await edit_plain(message, build_response(source, target, amount, market))
    except httpx.TimeoutException:
        await edit_html(
            message,
            "❌ <b>请求超时</b>\n\n请稍后重试。"
            f'\n\n<a href="{html.escape(fallback)}">使用 Google 查询</a>',
        )
    except Exception as exc:
        logs.warning(f"[Rate] 查询失败: {exc}")
        await edit_html(
            message,
            f"❌ <b>获取汇率失败</b>\n\n{html.escape(str(exc))}"
            f'\n\n<a href="{html.escape(fallback)}">使用 Google 查询</a>',
        )
