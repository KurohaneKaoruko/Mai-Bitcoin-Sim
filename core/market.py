"""多交易所行情聚合器。

支持 Binance / OKX / Gate.io / Coinbase 四家公共行情接口（均无需 API key），
启动时并发测速选择最快的主源，失败自动降级到下一家，价格带 TTL 缓存。

全部用标准库实现（urllib + asyncio.to_thread），零第三方依赖。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 5.0
USER_AGENT = "mai-bitcoin-sim/0.1 (+https://github.com/KurohaneKaoruko/Mai-Bitcoin-Sim)"

# K线 interval → 各交易所粒度映射
_INTERVAL_MAP = {
    # binance: 字符串直接用; okx: bar; gate: 秒; coinbase: 秒
    "5m":  {"binance": "5m",  "okx": "5m",  "gate": 300,     "coinbase": 300},
    "15m": {"binance": "15m", "okx": "15m", "gate": 900,     "coinbase": 900},
    "1h":  {"binance": "1h",  "okx": "1H",  "gate": 3600,    "coinbase": 3600},
    "4h":  {"binance": "4h",  "okx": "4H",  "gate": 14400,   "coinbase": 21600},
    "1d":  {"binance": "1d",  "okx": "1D",  "gate": 86400,   "coinbase": 86400},
}

# 各 interval 的秒数（回放引擎用）
INTERVAL_SECONDS = {
    "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400,
}


def _http_get_json(url: str, timeout: float = HTTP_TIMEOUT) -> Any:
    """同步 GET，返回解析后的 JSON。供 asyncio.to_thread 调用。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def fetch_json(url: str, timeout: float = HTTP_TIMEOUT) -> Any:
    """异步 GET JSON。任何网络/解析错误抛 RuntimeError。"""
    try:
        return await asyncio.to_thread(_http_get_json, url, timeout)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}") from e
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"{type(e).__name__}: {e}") from e


@dataclass
class Candle:
    ts: int          # 开盘时间（秒）
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Ticker24h:
    price: float
    change_pct: float   # 24h 涨跌幅 %
    high: float
    low: float
    source: str


class ExchangeSource:
    """单一交易所行情客户端。子类实现三个接口，符号格式自行转换。"""

    name: str = "?"
    quote_note: str = "USDT"

    def symbol(self, base: str) -> str:
        raise NotImplementedError

    async def get_price(self, base: str) -> float:
        raise NotImplementedError

    async def get_ticker24h(self, base: str) -> Ticker24h:
        p = await self.get_price(base)
        return Ticker24h(price=p, change_pct=0.0, high=p, low=p, source=self.name)

    async def get_klines(self, base: str, interval: str, limit: int) -> List[Candle]:
        raise NotImplementedError


class BinanceSource(ExchangeSource):
    name = "Binance"
    _base = "https://data-api.binance.vision"
    _fapi = "https://fapi.binance.com"

    def symbol(self, base: str) -> str:
        return f"{base.upper()}USDT"

    async def get_price(self, base: str) -> float:
        data = await fetch_json(f"{self._base}/api/v3/ticker/price?symbol={self.symbol(base)}")
        return float(data["price"])

    async def get_funding(self, base: str) -> float:
        """永续合约当前资金费率（Binance USDT-M fapi）。"""
        data = await fetch_json(f"{self._fapi}/fapi/v1/premiumIndex?symbol={self.symbol(base)}")
        return float(data["lastFundingRate"])

    async def get_ticker24h(self, base: str) -> Ticker24h:
        d = await fetch_json(f"{self._base}/api/v3/ticker/24hr?symbol={self.symbol(base)}")
        return Ticker24h(
            price=float(d["lastPrice"]),
            change_pct=float(d["priceChangePercent"]),
            high=float(d["highPrice"]),
            low=float(d["lowPrice"]),
            source=self.name,
        )

    async def get_klines(self, base: str, interval: str, limit: int, end_ms: Optional[int] = None) -> List[Candle]:
        iv = _INTERVAL_MAP[interval]["binance"]
        url = f"{self._base}/api/v3/klines?symbol={self.symbol(base)}&interval={iv}&limit={limit}"
        if end_ms:
            url += f"&endTime={int(end_ms)}"
        rows = await fetch_json(url)
        return [Candle(int(r[0] / 1000), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in rows]


class OkxSource(ExchangeSource):
    name = "OKX"
    _base = "https://www.okx.com"

    def symbol(self, base: str) -> str:
        return f"{base.upper()}-USDT"

    async def get_price(self, base: str) -> float:
        data = await fetch_json(f"{self._base}/api/v5/market/ticker?instId={self.symbol(base)}")
        rows = data.get("data") or []
        if not rows:
            raise RuntimeError("OKX: symbol not found")
        return float(rows[0]["last"])

    async def get_funding(self, base: str) -> float:
        """永续合约当前资金费率（OKX USDT 永续）。"""
        data = await fetch_json(
            f"{self._base}/api/v5/public/funding-rate?instId={base.upper()}-USDT-SWAP"
        )
        rows = data.get("data") or []
        if not rows:
            raise RuntimeError("OKX: swap not found")
        return float(rows[0]["fundingRate"])

    async def get_ticker24h(self, base: str) -> Ticker24h:
        data = await fetch_json(f"{self._base}/api/v5/market/ticker?instId={self.symbol(base)}")
        rows = data.get("data") or []
        if not rows:
            raise RuntimeError("OKX: symbol not found")
        r = rows[0]
        last = float(r["last"])
        open24 = float(r.get("open24h") or last)
        return Ticker24h(
            price=last,
            change_pct=(last - open24) / open24 * 100 if open24 else 0.0,
            high=float(r.get("high24h") or last),
            low=float(r.get("low24h") or last),
            source=self.name,
        )

    async def get_klines(self, base: str, interval: str, limit: int, end_ms: Optional[int] = None) -> List[Candle]:
        bar = _INTERVAL_MAP[interval]["okx"]
        url = f"{self._base}/api/v5/market/candles?instId={self.symbol(base)}&bar={bar}&limit={min(limit, 300)}"
        if end_ms:
            url += f"&after={int(end_ms)}"  # OKX after=返回早于该时间的数据
        data = await fetch_json(url)
        rows = data.get("data") or []
        out = []
        for r in rows:  # OKX 倒序：新→旧
            out.append(Candle(int(r[0]) // 1000, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])))
        out.reverse()
        return out


class GateSource(ExchangeSource):
    name = "Gate.io"
    _base = "https://api.gateio.ws"

    def symbol(self, base: str) -> str:
        return f"{base.upper()}_USDT"

    async def get_price(self, base: str) -> float:
        rows = await fetch_json(f"{self._base}/api/v4/spot/tickers?currency_pair={self.symbol(base)}")
        if not rows:
            raise RuntimeError("Gate: symbol not found")
        return float(rows[0]["last"])

    async def get_funding(self, base: str) -> float:
        """永续合约当前资金费率（Gate USDT 永续）。"""
        d = await fetch_json(f"{self._base}/api/v4/futures/usdt/contracts/{self.symbol(base)}")
        rate = d.get("funding_rate") or d.get("funding_rate_indicative")
        if rate is None:
            raise RuntimeError("Gate: no funding rate")
        return float(rate)

    async def get_ticker24h(self, base: str) -> Ticker24h:
        rows = await fetch_json(f"{self._base}/api/v4/spot/tickers?currency_pair={self.symbol(base)}")
        if not rows:
            raise RuntimeError("Gate: symbol not found")
        r = rows[0]
        last = float(r["last"])
        return Ticker24h(
            price=last,
            change_pct=float(r.get("change_percentage") or 0.0),
            high=float(r.get("high_24h") or last),
            low=float(r.get("low_24h") or last),
            source=self.name,
        )

    async def get_klines(self, base: str, interval: str, limit: int, end_ms: Optional[int] = None) -> List[Candle]:
        sec = _INTERVAL_MAP[interval]["gate"]
        url = f"{self._base}/api/v4/spot/candlesticks?currency_pair={self.symbol(base)}&interval={sec}&limit={min(limit, 1000)}"
        if end_ms:
            url += f"&to={int(end_ms)}&to_ts=1"  # to=毫秒时间戳
        rows = await fetch_json(url)
        out = []
        for r in rows:  # gate 倒序
            out.append(Candle(int(r["t"]), float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"]), float(r["v"])))
        out.reverse()
        return out


class CoinbaseSource(ExchangeSource):
    """Coinbase Exchange 公共行情。注意：交易对是 USD 计价（非 USDT），模拟盘按 1:1 处理。"""

    name = "Coinbase"
    quote_note = "USD"
    _base = "https://api.exchange.coinbase.com"

    def symbol(self, base: str) -> str:
        return f"{base.upper()}-USD"

    async def get_price(self, base: str) -> float:
        data = await fetch_json(f"{self._base}/products/{self.symbol(base)}/ticker")
        return float(data["price"])

    async def get_ticker24h(self, base: str) -> Ticker24h:
        d = await fetch_json(f"{self._base}/products/{self.symbol(base)}/stats")
        last = float(d["last"])
        open_ = float(d["open"])
        return Ticker24h(
            price=last,
            change_pct=(last - open_) / open_ * 100 if open_ else 0.0,
            high=float(d["high"]),
            low=float(d["low"]),
            source=self.name,
        )

    async def get_klines(self, base: str, interval: str, limit: int) -> List[Candle]:
        gran = _INTERVAL_MAP[interval]["coinbase"]
        # coinbase granularity 必须是 60/300/900/3600/21600/86400
        rows = await fetch_json(f"{self._base}/products/{self.symbol(base)}/candles?granularity={gran}")
        out = []
        for r in rows:  # [time, low, high, open, close, volume]，倒序
            out.append(Candle(int(r[0]), float(r[3]), float(r[2]), float(r[1]), float(r[4]), float(r[5])))
        out.reverse()
        return out[-limit:]


class MarketAggregator:
    """多交易所聚合：测速排序 → 主源优先、失败降级。"""

    def __init__(self, price_ttl: float = 10.0, ticker_ttl: float = 30.0, kline_ttl: float = 60.0,
                 funding_ttl: float = 600.0):
        self.sources: List[ExchangeSource] = [BinanceSource(), OkxSource(), GateSource(), CoinbaseSource()]
        self._order: List[str] = [s.name for s in self.sources]
        self.price_ttl = price_ttl
        self.ticker_ttl = ticker_ttl
        self.kline_ttl = kline_ttl
        self.funding_ttl = funding_ttl
        # cache: key -> (expires_at, value)
        self._cache: Dict[str, Tuple[float, Any]] = {}
        # 各源最近延迟（毫秒），-1 = 不可用
        self.latency: Dict[str, float] = {s.name: -1.0 for s in self.sources}
        # funding 源独立可用性：spot 可达 ≠ fapi 可达，记忆上次成功的源
        self._funding_ok: Optional[str] = None
        self._lock = asyncio.Lock()

    # ── 测速 ──────────────────────────────────────────────

    async def _probe_one(self, src: ExchangeSource) -> Tuple[str, float]:
        t0 = time.monotonic()
        try:
            await src.get_price("BTC")
            return src.name, (time.monotonic() - t0) * 1000
        except Exception as e:  # noqa: BLE001
            logger.debug("行情源 %s 测速失败: %s", src.name, e)
            return src.name, -1.0

    async def measure(self) -> Dict[str, float]:
        """并发测速全部交易所，更新优先级排序。返回 {名称: 延迟ms}。"""
        results = await asyncio.gather(*(self._probe_one(s) for s in self.sources))
        ok = sorted([(n, ms) for n, ms in results if ms > 0], key=lambda x: x[1])
        failed = [(n, -1.0) for n, ms in results if ms <= 0]
        self.latency = dict(ok + failed)
        # 可用源按延迟排序在前，不可用的垫底（仍保留作冷备）
        self._order = [n for n, _ in ok] + [n for n, _ in failed]
        logger.info("行情源测速完成: %s", ", ".join(f"{n}={ms:.0f}ms" if ms > 0 else f"{n}=不可用" for n, ms in results))
        return dict(self.latency)

    def source_status(self) -> List[Tuple[str, float]]:
        return [(n, self.latency.get(n, -1.0)) for n in self._order]

    def _pick(self) -> List[ExchangeSource]:
        by_name = {s.name: s for s in self.sources}
        return [by_name[n] for n in self._order if n in by_name]

    # ── 缓存 ──────────────────────────────────────────────

    def _cache_get(self, key: str) -> Optional[Any]:
        hit = self._cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        return None

    def _cache_put(self, key: str, value: Any, ttl: float) -> None:
        self._cache[key] = (time.monotonic() + ttl, value)

    # ── 对外接口 ──────────────────────────────────────────

    async def get_price(self, base: str) -> Tuple[float, str]:
        """返回 (价格, 来源名)。"""
        base = base.upper().strip()
        key = f"p:{base}"
        hit = self._cache_get(key)
        if hit is not None:
            return hit
        errors: List[str] = []
        for src in self._pick():
            try:
                price = await src.get_price(base)
                if price <= 0:
                    raise RuntimeError("非法价格")
                self._cache_put(key, (price, src.name), self.price_ttl)
                return price, src.name
            except Exception as e:  # noqa: BLE001
                errors.append(f"{src.name}: {e}")
                continue
        raise RuntimeError("；".join(errors) or "无可用行情源")

    async def get_funding(self, base: str, default: Optional[float] = None) -> Optional[float]:
        """返回永续资金费率；数据源都失败时返回 default。

        spot 可达 ≠ fapi 可达，因此 funding 源独立选择：
        有记忆（上次成功的源）直连；否则并发发起、先成功者胜（不等慢源）。
        """
        base = base.upper().strip()
        key = f"f:{base}"
        hit = self._cache_get(key)
        if hit is not None:
            return hit
        srcs = [s for s in self._pick() if callable(getattr(s, "get_funding", None))]
        rate: Optional[float] = None

        async def _try(src) -> Tuple[str, Optional[float]]:
            try:
                return src.name, await src.get_funding(base)
            except Exception as e:  # noqa: BLE001
                logger.debug("get_funding %s via %s 失败: %s", base, src.name, e)
                return src.name, None

        cached_ok = self._funding_ok
        if cached_ok:
            by_name = {s.name: s for s in srcs}
            src = by_name.get(cached_ok)
            if src:
                _, rate = await _try(src)
                if rate is None:
                    cached_ok = None  # 上次的源也失效，退回竞速
        if rate is None and not cached_ok and srcs:
            tasks = {asyncio.create_task(_try(s)) for s in srcs}
            pending = set(tasks)
            try:
                while pending:
                    done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    for t in done:
                        name, r = t.result()
                        if r is not None:
                            self._funding_ok = name
                            rate = r
                            break
                    if rate is not None:
                        break
            finally:
                for t in pending:
                    t.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
        if rate is not None:
            self._cache_put(key, rate, self.funding_ttl)
            return rate
        if default is not None:
            self._cache_put(key, default, min(self.funding_ttl, 60.0))
        return default

    async def get_ticker24h(self, base: str) -> Ticker24h:
        base = base.upper().strip()
        key = f"t:{base}"
        hit = self._cache_get(key)
        if hit is not None:
            return hit
        errors: List[str] = []
        for src in self._pick():
            try:
                t = await src.get_ticker24h(base)
                self._cache_put(key, t, self.ticker_ttl)
                return t
            except Exception as e:  # noqa: BLE001
                errors.append(f"{src.name}: {e}")
                continue
        raise RuntimeError("；".join(errors) or "无可用行情源")

    async def get_klines(self, base: str, interval: str = "1h", limit: int = 72) -> List[Candle]:
        base = base.upper().strip()
        if interval not in _INTERVAL_MAP:
            raise RuntimeError(f"不支持的 K 线周期: {interval}（可选 {list(_INTERVAL_MAP)}）")
        key = f"k:{base}:{interval}:{limit}"
        hit = self._cache_get(key)
        if hit is not None:
            return hit
        errors: List[str] = []
        for src in self._pick():
            try:
                candles = await src.get_klines(base, interval, limit)
                if not candles:
                    raise RuntimeError("空K线")
                self._cache_put(key, candles, self.kline_ttl)
                return candles
            except Exception as e:  # noqa: BLE001
                errors.append(f"{src.name}: {e}")
                continue
        raise RuntimeError("；".join(errors) or "无可用行情源")

    async def get_klines_history(self, base: str, interval: str, end_ms: int, limit: int = 500) -> List[Candle]:
        """取 end_ms 之前的历史K线（含 end_ms 所在根），分页聚合，回放引擎用。

        逐源尝试（各源参数格式不同，基类签名已带 end_ms），Binance 上限 1000 根/次。
        """
        base = base.upper().strip()
        if interval not in _INTERVAL_MAP:
            raise RuntimeError(f"不支持的 K 线周期: {interval}")
        errors: List[str] = []
        for src in self._pick():
            collected: List[Candle] = []
            cursor = int(end_ms)
            remaining = limit
            try:
                while remaining > 0:
                    batch = min(remaining, 1000 if src.name == "Binance" else 300)
                    candles = await src.get_klines(base, interval, batch, end_ms=cursor)
                    if not candles:
                        break
                    collected = candles + collected
                    remaining -= len(candles)
                    first = candles[0].ts * 1000
                    if first >= cursor:  # 没有更早的数据了
                        break
                    cursor = first - 1
                if collected:
                    # 去重排序（按 ts）
                    seen = {c.ts for c in collected}
                    collected = sorted(collected, key=lambda c: c.ts)
                    return collected[:limit]
            except Exception as e:  # noqa: BLE001
                errors.append(f"{src.name}: {e}")
                continue
        raise RuntimeError("；".join(errors) or "无可用行情源")

    async def get_prices(self, bases: Sequence[str]) -> Dict[str, float]:
        """批量取价，失败的币种跳过。"""
        async def one(b: str) -> Tuple[str, Optional[float]]:
            try:
                p, _ = await self.get_price(b)
                return b, p
            except Exception:  # noqa: BLE001
                return b, None
        results = await asyncio.gather(*(one(b) for b in bases))
        return {b: p for b, p in results if p is not None}

    async def get_overview(self, bases: Sequence[str] = ("BTC", "ETH", "SOL", "BNB")) -> List[Tuple[str, Ticker24h]]:
        """大盘概览：主流币 24h 行情，返回 [(symbol, ticker), ...]。"""
        async def one(b: str) -> Tuple[str, Optional[Ticker24h]]:
            try:
                return b, await self.get_ticker24h(b)
            except Exception:  # noqa: BLE001
                return b, None
        got = await asyncio.gather(*(one(b) for b in bases))
        return [(b, t) for b, t in got if t is not None]


# ── 常用币种别名（中文 → 符号） ──────────────────────────

SYMBOL_ALIASES: Dict[str, str] = {
    "比特币": "BTC", "大饼": "BTC", " btc": "BTC",
    "以太坊": "ETH", "以太": "ETH", "以太狗": "ETH", "V神币": "ETH",
    "索拉纳": "SOL", "solana": "SOL",
    "狗币": "DOGE", "狗狗币": "DOGE", "狗狗": "DOGE", "旺财": "DOGE",
    "屎币": "SHIB", "柴犬": "SHIB",
    "瑞波": "XRP", "波场": "TRX", "辣条": "LTC", "莱特": "LTC",
    "币安币": "BNB", "门罗": "XMR", "艾达": "ADA", "雪崩": "AVAX",
    "铭文": "ORDI",
}


def normalize_symbol(raw: str) -> str:
    """把用户输入的币名（含中文别名、大小写、$前缀）规整为交易所符号。"""
    s = raw.strip().lstrip("$").strip()
    if not s:
        return ""
    low = " " + s.lower()
    for alias, sym in SYMBOL_ALIASES.items():
        if low == alias or s.lower() == alias.strip() or s == sym or s.upper() == sym:
            return sym
    return s.upper()
