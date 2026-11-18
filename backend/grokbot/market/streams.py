"""Binance WebSocket market-data and user-data streams.

Market streams (combined-stream endpoint ``/stream?streams=a/b/c``):
  Spot    : <s>@bookTicker, <s>@ticker, <s>@kline_<tf>, <s>@depth20@1000ms
  Futures : <s>@bookTicker, <s>@markPrice@1s, <s>@ticker, <s>@kline_<tf>, <s>@depth20@500ms

Streams update ``MarketData`` in place. REST remains the source of truth for
reconciliation and recovery: when a stream is unhealthy, ``MarketData.run``
falls back to fast REST polling automatically.

Futures user-data stream (testnet/live only): listenKey from
POST /fapi/v1/listenKey, kept alive with PUT every 30 min; ORDER_TRADE_UPDATE,
ACCOUNT_UPDATE and ALGO_UPDATE events trigger an immediate REST
reconciliation of the affected position (events are never trusted alone).
Spot user data is reconciled by REST polling only (Binance has moved Spot
user-data delivery to its WebSocket API, which is not implemented here).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

import websockets

from .data import TF_SECONDS, MarketData, Ticker

log = logging.getLogger("grokbot.streams")


@dataclass
class StreamHealth:
    name: str
    url: str = ""
    connected: bool = False
    last_msg: float = 0.0
    connected_at: float = 0.0
    messages: int = 0
    reconnects: int = 0
    last_error: str = ""
    lag_ms: float = 0.0
    streams: int = 0
    by_type: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"name": self.name, "url": self.url, "connected": self.connected,
                "last_msg_age": round(time.time() - self.last_msg, 2) if self.last_msg else None,
                "messages": self.messages, "reconnects": self.reconnects, "last_error": self.last_error,
                "lag_ms": round(self.lag_ms, 1), "streams": self.streams}

    def healthy(self, max_silence: float = 5.0) -> bool:
        return self.connected and self.last_msg > 0 and time.time() - self.last_msg <= max_silence


class MarketStreams:
    def __init__(self, md: MarketData, spot_ws: str, futures_ws: str, timeframes_fn):
        self.md = md
        self.base = {"spot": spot_ws.rstrip("/"), "futures": futures_ws.rstrip("/")}
        self.timeframes_fn = timeframes_fn  # () -> list[str] of timeframes to stream
        self.health = {m: StreamHealth(m) for m in ("spot", "futures")}
        self.books: dict[str, dict[str, dict]] = {"spot": {}, "futures": {}}
        self._restart = {m: asyncio.Event() for m in ("spot", "futures")}
        md.streams = self

    def stream_names(self, market: str) -> list[str]:
        out = []
        tfs = sorted(set(self.timeframes_fn()))
        for sym in self.md.symbols[market]:
            s = sym.lower()
            out += [f"{s}@bookTicker", f"{s}@ticker"]
            out += [f"{s}@kline_{tf}" for tf in tfs]
            if market == "spot":
                out.append(f"{s}@depth20@1000ms")
            else:
                out += [f"{s}@markPrice@1s", f"{s}@depth20@500ms"]
        return out

    def resubscribe(self) -> None:
        """Symbols/timeframes changed: reconnect with the new stream list."""
        for e in self._restart.values():
            e.set()

    async def run(self, stop: asyncio.Event) -> None:
        await asyncio.gather(*(self._run_market(m, stop) for m in ("spot", "futures")))

    async def _run_market(self, market: str, stop: asyncio.Event) -> None:
        h = self.health[market]
        backoff = 1.0
        while not stop.is_set():
            names = self.stream_names(market)
            if not names:
                await asyncio.sleep(2)
                continue
            url = f"{self.base[market]}/stream?streams={'/'.join(names)}"
            h.url, h.streams = self.base[market], len(names)
            self._restart[market].clear()
            try:
                async with websockets.connect(url, open_timeout=10, ping_interval=20, ping_timeout=20,
                                              max_size=2**22, close_timeout=2) as ws:
                    h.connected, h.connected_at, h.last_error = True, time.time(), ""
                    backoff = 1.0
                    log.info("%s stream connected (%d streams)", market, len(names))
                    while not stop.is_set() and not self._restart[market].is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=10)
                        except asyncio.TimeoutError:
                            raise ConnectionError("no data for 10s")
                        self._handle(market, raw)
            except Exception as e:  # noqa: BLE001 - reconnect on anything
                h.last_error = f"{type(e).__name__}: {str(e)[:160]}"
                log.warning("%s stream error: %s", market, h.last_error)
            h.connected = False
            if stop.is_set():
                break
            if not self._restart[market].is_set():
                h.reconnects += 1
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    # ------------------------------------------------------------- handlers
    def _handle(self, market: str, raw: str) -> None:
        h = self.health[market]
        now = time.time()
        h.last_msg = now
        h.messages += 1
        msg = json.loads(raw)
        stream, d = msg.get("stream", ""), msg.get("data", {})
        kind = stream.split("@", 1)[1] if "@" in stream else "?"
        h.by_type[kind] = h.by_type.get(kind, 0) + 1
        if "E" in d:
            h.lag_ms = h.lag_ms * 0.9 + max(0.0, now * 1000 - d["E"]) * 0.1
        sym = (d.get("s") or stream.split("@")[0]).upper()
        md = self.md
        t = md.tickers[market].get(sym)
        if kind == "bookTicker":
            bid, ask = float(d["b"]), float(d["a"])
            if t is None:
                t = md.tickers[market][sym] = Ticker(sym, (bid + ask) / 2)
            t.bid, t.ask, t.ts = bid, ask, now
            if market == "futures":
                t.price = (bid + ask) / 2
        elif kind == "ticker":
            if t is None:
                t = md.tickers[market][sym] = Ticker(sym, float(d["c"]))
            if market == "spot":
                t.price = float(d["c"])
            t.change_pct, t.high, t.low, t.quote_volume = float(d["P"]), float(d["h"]), float(d["l"]), float(d["q"])
            t.ts = now
        elif kind.startswith("markPrice"):
            if t is None:
                t = md.tickers[market][sym] = Ticker(sym, float(d["p"]))
            t.mark, t.index = float(d["p"]), float(d.get("i") or 0)
            t.funding_rate, t.next_funding = float(d.get("r") or 0), int(d.get("T") or 0)
            t.ts = now
        elif kind.startswith("kline_"):
            k = d["k"]
            if k.get("x"):
                md.on_closed_kline(market, sym, k["i"], [k["t"], k["o"], k["h"], k["l"], k["c"], k["v"], k["T"], k["q"]])
        elif kind.startswith("depth"):
            self.books[market][sym] = {"bids": d.get("bids") or d.get("b", []), "asks": d.get("asks") or d.get("a", []),
                                       "ts": now}
        md.last_update[market] = now

    def book(self, market: str, symbol: str, max_age: float = 3.0) -> dict | None:
        b = self.books[market].get(symbol)
        if b and time.time() - b["ts"] <= max_age:
            return b
        return None


class FuturesUserStream:
    """Futures user-data stream for testnet/live (requires API key)."""

    def __init__(self, client, ws_base: str, on_event):
        self.client = client          # BinanceClient with credentials (futures)
        self.ws_base = ws_base.rstrip("/")
        self.on_event = on_event      # callable(event dict)
        self.health = StreamHealth("futures-user")
        self.events: list[dict] = []

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                lk = (await self.client.request("POST", "/fapi/v1/listenKey", weight=1, retries=0,
                                                signed=False, api_key_only=True))["listenKey"]
                self.health.url = self.ws_base
                async with websockets.connect(f"{self.ws_base}/ws/{lk}", open_timeout=10, ping_interval=20) as ws:
                    self.health.connected, self.health.connected_at = True, time.time()
                    backoff, last_keepalive = 1.0, time.time()
                    while not stop.is_set():
                        if time.time() - last_keepalive > 1800:
                            await self.client.request("PUT", "/fapi/v1/listenKey", weight=1, api_key_only=True)
                            last_keepalive = time.time()
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=60)
                        except asyncio.TimeoutError:
                            continue
                        ev = json.loads(raw)
                        self.health.last_msg, self.health.messages = time.time(), self.health.messages + 1
                        if ev.get("e") == "listenKeyExpired":
                            break
                        self.events = ([{k: ev.get(k) for k in ("e", "E")} | {"o": ev.get("o", {}).get("X")}] + self.events)[:50]
                        self.on_event(ev)
            except Exception as e:  # noqa: BLE001
                self.health.last_error = f"{type(e).__name__}: {str(e)[:160]}"
            self.health.connected = False
            if stop.is_set():
                break
            self.health.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)


def status_for(md: MarketData, market: str, stale_after: float) -> dict:
    """LIVE: stream connected and fresh. DELAYED: data fresh but via REST
    fallback or a lagging stream. DISCONNECTED: data older than stale_after."""
    streams: MarketStreams | None = getattr(md, "streams", None)
    age = md.age(market)
    h = streams.health[market] if streams else None
    if age > stale_after:
        state = "DISCONNECTED"
    elif h and h.healthy(5.0) and h.lag_ms < 2000:
        state = "LIVE"
    else:
        state = "DELAYED"
    return {"state": state, "age": round(age, 1) if age != float("inf") else None,
            "source": "websocket" if state == "LIVE" else "rest",
            "stream": h.as_dict() if h else None}


__all__ = ["MarketStreams", "FuturesUserStream", "status_for", "TF_SECONDS"]
