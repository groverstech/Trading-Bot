"""Market data service: genuine Binance market data for Spot and USDⓈ-M Futures.

* Tickers / best bid-ask / futures mark price & funding are polled on short
  intervals. Each market tracks ``last_update`` so the risk engine can block
  trading on stale data.
* Klines are cached and only CLOSED candles are ever returned - the still
  forming candle is dropped - so strategies cannot peek at unfinished data.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from decimal import Decimal

import numpy as np

from ..exchange.filters import SymbolRules
from ..exchange.futures import FuturesAdapter
from ..exchange.spot import SpotAdapter
from ..util import D

log = logging.getLogger("grokbot.market")

TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


@dataclass
class Candles:
    symbol: str
    timeframe: str
    open_time: np.ndarray   # ms
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    quote_volume: np.ndarray
    close_time: np.ndarray  # ms
    fetched_at: float

    def __len__(self) -> int:
        return len(self.close)

    def slice(self, end: int) -> "Candles":
        """Candles [0, end) - used by the backtester to avoid look-ahead."""
        return Candles(self.symbol, self.timeframe, self.open_time[:end], self.open[:end], self.high[:end],
                       self.low[:end], self.close[:end], self.volume[:end], self.quote_volume[:end],
                       self.close_time[:end], self.fetched_at)

    @classmethod
    def from_raw(cls, symbol: str, tf: str, raw: list[list], now_ms: int | None = None) -> "Candles":
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        rows = [r for r in raw if int(r[6]) < now_ms]  # closed candles only
        a = np.array([[float(x) for x in r[:8]] for r in rows]) if rows else np.zeros((0, 8))
        return cls(symbol, tf, a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4], a[:, 5], a[:, 7], a[:, 6], time.time())


@dataclass
class Ticker:
    symbol: str
    price: float
    change_pct: float = 0.0
    high: float = 0.0
    low: float = 0.0
    quote_volume: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    mark: float = 0.0
    index: float = 0.0
    funding_rate: float = 0.0
    next_funding: int = 0
    ts: float = 0.0

    @property
    def spread_bps(self) -> float:
        if self.bid <= 0 or self.ask <= 0:
            return float("inf")
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / mid * 1e4

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__} | {"spread_bps": round(self.spread_bps, 2)}


class MarketData:
    def __init__(self, spot: SpotAdapter, futures: FuturesAdapter, futures_is_testnet: bool = False):
        self.spot = spot
        self.futures = futures
        self.futures_is_testnet = futures_is_testnet
        self.adapters = {"spot": spot, "futures": futures}
        self.tickers: dict[str, dict[str, Ticker]] = {"spot": {}, "futures": {}}
        self.rules: dict[str, dict[str, SymbolRules]] = {"spot": {}, "futures": {}}
        self.last_update: dict[str, float] = {"spot": 0.0, "futures": 0.0}
        self.symbols: dict[str, list[str]] = {"spot": [], "futures": []}
        self._klines: dict[tuple, Candles] = {}
        self._kline_locks: dict[tuple, asyncio.Lock] = {}
        self._oi: dict[str, tuple[float, float]] = {}
        self.errors: dict[str, str] = {"spot": "", "futures": ""}
        self._rules_loaded_at = 0.0

    def set_symbols(self, spot: list[str], futures: list[str]) -> None:
        self.symbols = {"spot": list(dict.fromkeys(spot)), "futures": list(dict.fromkeys(futures))}

    # ------------------------------------------------------------- refresh
    async def load_rules(self) -> None:
        for m, ad in self.adapters.items():
            if not self.symbols[m]:
                continue
            try:
                self.rules[m] = await ad.exchange_info(self.symbols[m])
            except Exception as e:  # noqa: BLE001 - report and keep going
                self.errors[m] = f"exchangeInfo: {e}"
                log.warning("exchangeInfo %s failed: %s", m, e)
        self._rules_loaded_at = time.time()

    async def refresh_spot(self) -> None:
        syms = self.symbols["spot"]
        if not syms:
            return
        t24, books = await asyncio.gather(self.spot.ticker_24hr(syms), self.spot.book_ticker(syms))
        bk = {b["symbol"]: b for b in books}
        ts = time.time()
        for t in t24:
            s = t["symbol"]
            b = bk.get(s, {})
            self.tickers["spot"][s] = Ticker(
                s, float(t["lastPrice"]), float(t["priceChangePercent"]), float(t["highPrice"]),
                float(t["lowPrice"]), float(t["quoteVolume"]), float(b.get("bidPrice", 0)),
                float(b.get("askPrice", 0)), ts=ts)
        self.last_update["spot"] = ts
        self.errors["spot"] = ""

    async def refresh_futures(self, include_24h: bool) -> None:
        syms = set(self.symbols["futures"])
        if not syms:
            return
        calls = [self.futures.premium_index_all(), self.futures.book_ticker_all()]
        if include_24h:
            calls.append(self.futures.ticker_24hr_all())
        res = await asyncio.gather(*calls)
        prem = {p["symbol"]: p for p in res[0] if p["symbol"] in syms}
        books = {b["symbol"]: b for b in res[1] if b["symbol"] in syms}
        t24 = {t["symbol"]: t for t in res[2] if t["symbol"] in syms} if include_24h else {}
        ts = time.time()
        for s in syms:
            p, b = prem.get(s), books.get(s)
            if not p or not b:
                continue
            old = self.tickers["futures"].get(s)
            t = t24.get(s)
            bid, ask = float(b["bidPrice"]), float(b["askPrice"])
            self.tickers["futures"][s] = Ticker(
                s, price=(bid + ask) / 2 if bid and ask else float(p["markPrice"]),
                change_pct=float(t["priceChangePercent"]) if t else (old.change_pct if old else 0.0),
                high=float(t["highPrice"]) if t else (old.high if old else 0.0),
                low=float(t["lowPrice"]) if t else (old.low if old else 0.0),
                quote_volume=float(t["quoteVolume"]) if t else (old.quote_volume if old else 0.0),
                bid=bid, ask=ask, mark=float(p["markPrice"]), index=float(p["indexPrice"]),
                funding_rate=float(p.get("lastFundingRate") or 0), next_funding=int(p.get("nextFundingTime") or 0),
                ts=ts)
        self.last_update["futures"] = ts
        self.errors["futures"] = ""

    async def run(self, stop: asyncio.Event, interval: float = 3.0) -> None:
        """Background polling loop."""
        n = 0
        while not stop.is_set():
            if time.time() - self._rules_loaded_at > 3600 or any(
                    s not in self.rules[m] for m in self.rules for s in self.symbols[m]):
                if time.time() - self._rules_loaded_at > 30:
                    await self.load_rules()
            for m, coro in (("spot", self.refresh_spot()), ("futures", self.refresh_futures(n % 5 == 0))):
                try:
                    await coro
                except Exception as e:  # noqa: BLE001
                    self.errors[m] = str(e)[:300]
                    log.warning("market refresh %s failed: %s", m, e)
            n += 1
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------- queries
    def ticker(self, market: str, symbol: str) -> Ticker | None:
        return self.tickers[market].get(symbol)

    def price(self, market: str, symbol: str) -> float | None:
        t = self.ticker(market, symbol)
        return t.price if t else None

    def mark(self, market: str, symbol: str) -> float | None:
        t = self.ticker(market, symbol)
        if not t:
            return None
        return t.mark if market == "futures" and t.mark else t.price

    def age(self, market: str, symbol: str | None = None) -> float:
        if symbol:
            t = self.ticker(market, symbol)
            return time.time() - t.ts if t else float("inf")
        lu = self.last_update[market]
        return time.time() - lu if lu else float("inf")

    def is_stale(self, market: str, max_age: float, symbol: str | None = None) -> bool:
        return self.age(market, symbol) > max_age

    async def klines(self, market: str, symbol: str, tf: str, limit: int = 300) -> Candles:
        key = (market, symbol, tf)
        lock = self._kline_locks.setdefault(key, asyncio.Lock())
        async with lock:
            c = self._klines.get(key)
            now_ms = int(time.time() * 1000)
            # Refresh once the next candle has closed (plus 2 s settle time).
            if c is None or len(c) < min(limit, 50) or now_ms > c.close_time[-1] + TF_SECONDS[tf] * 1000 + 2000:
                raw = await self.adapters[market].klines(symbol, tf, limit=limit + 1)
                c = Candles.from_raw(symbol, tf, raw, now_ms)
                self._klines[key] = c
            return c

    async def depth(self, market: str, symbol: str, limit: int = 100) -> dict:
        return await self.adapters[market].depth(symbol, limit)

    async def open_interest(self, symbol: str) -> float | None:
        """Open interest (contracts); cached 60 s."""
        cached = self._oi.get(symbol)
        if cached and time.time() - cached[1] < 60:
            return cached[0]
        try:
            v = float((await self.futures.open_interest(symbol))["openInterest"])
        except Exception:  # noqa: BLE001 - optional data
            return None
        self._oi[symbol] = (v, time.time())
        return v

    def snapshot(self) -> dict:
        return {
            m: {
                "tickers": {s: t.as_dict() for s, t in self.tickers[m].items()},
                "age": round(self.age(m), 1) if self.last_update[m] else None,
                "error": self.errors[m],
                "source": (self.spot.data.base_url if m == "spot" else self.futures.data.base_url),
                "testnet_data": m == "futures" and self.futures_is_testnet,
            } for m in ("spot", "futures")
        }


def walk_book(levels: list[list[str]], qty: Decimal) -> tuple[Decimal, Decimal] | None:
    """Average fill price for ``qty`` consumed from book ``levels``.

    Returns (avg_price, filled_qty) or None if the book is empty. If the book
    is too thin, filled_qty < qty.
    """
    remaining, cost, filled = qty, Decimal(0), Decimal(0)
    for p, q in levels:
        p, q = D(p), D(q)
        take = min(q, remaining)
        cost += take * p
        filled += take
        remaining -= take
        if remaining <= 0:
            break
    if filled == 0:
        return None
    return cost / filled, filled


def depth_within(levels: list[list[str]], ref: float, band_pct: float, side: str) -> float:
    """Quote notional available within ``band_pct`` % of ``ref`` on one side."""
    total = 0.0
    for p, q in levels:
        p, q = float(p), float(q)
        if side == "ask" and p > ref * (1 + band_pct / 100):
            break
        if side == "bid" and p < ref * (1 - band_pct / 100):
            break
        total += p * q
    return total
