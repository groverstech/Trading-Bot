import json
import time

import pytest

from grokbot.market.streams import MarketStreams, status_for


def msg(stream, data):
    return json.dumps({"stream": stream, "data": data})


async def test_stream_updates_prices_mark_book_and_klines(engine):
    s = MarketStreams(engine.md, "wss://x", "wss://y", lambda: ["15m"])
    now_ms = int(time.time() * 1000)
    s._handle("spot", msg("btcusdt@bookTicker", {"s": "BTCUSDT", "b": "61000.5", "a": "61000.6"}))
    t = engine.md.ticker("spot", "BTCUSDT")
    assert t.bid == 61000.5 and t.ask == 61000.6 and time.time() - t.ts < 1
    s._handle("spot", msg("btcusdt@ticker", {"E": now_ms, "s": "BTCUSDT", "c": "61000.55", "P": "1.5", "h": "62000",
                                             "l": "60000", "q": "123456789"}))
    assert t.price == 61000.55 and t.quote_volume == 123456789
    s._handle("futures", msg("ethusdt@markPrice@1s", {"E": now_ms, "s": "ETHUSDT", "p": "3001.2", "i": "3001.0",
                                                     "r": "0.0002", "T": now_ms + 3600_000}))
    f = engine.md.ticker("futures", "ETHUSDT")
    assert f.mark == 3001.2 and f.funding_rate == 0.0002
    s._handle("spot", msg("btcusdt@depth20@1000ms", {"bids": [["61000", "1"]], "asks": [["61001", "2"]]}))
    assert engine.md.book("spot", "BTCUSDT")["asks"][0] == ["61001", "2"]
    # closed kline appended to the cache without a REST call
    c = await engine.md.klines("spot", "BTCUSDT", "15m", 100)
    last_open = int(c.open_time[-1])
    nxt = last_open + 900_000
    s._handle("spot", msg("btcusdt@kline_15m", {"E": now_ms, "s": "BTCUSDT", "k": {
        "t": nxt, "T": nxt + 899_999, "i": "15m", "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "10", "q": "15",
        "x": True}}))
    c2 = engine.md._klines[("spot", "BTCUSDT", "15m")]
    assert int(c2.open_time[-1]) == nxt and c2.close[-1] == 1.5 and len(c2) == len(c)
    # a forming (not closed) kline is ignored
    s._handle("spot", msg("btcusdt@kline_15m", {"s": "BTCUSDT", "k": {
        "t": nxt + 900_000, "T": nxt + 1_799_999, "i": "15m", "o": "1", "h": "2", "l": "0.5", "c": "9", "v": "1",
        "q": "1", "x": False}}))
    assert engine.md._klines[("spot", "BTCUSDT", "15m")].close[-1] == 1.5


@pytest.mark.safety
async def test_feed_status_live_delayed_disconnected(engine):
    s = MarketStreams(engine.md, "wss://x", "wss://y", lambda: ["15m"])
    h = s.health["spot"]
    h.connected, h.last_msg = True, time.time()
    engine.md.last_update["spot"] = time.time()
    assert status_for(engine.md, "spot", 20)["state"] == "LIVE"
    h.connected = False  # stream dropped, REST still fresh
    assert status_for(engine.md, "spot", 20)["state"] == "DELAYED"
    engine.md.last_update["spot"] = time.time() - 60
    assert status_for(engine.md, "spot", 20)["state"] == "DISCONNECTED"


async def test_scanner_only_trades_promoted_strategies(engine):
    assert engine.scanner.tradable() == {}  # nothing promoted by default
    engine.cfg.update("automation", {"paper_research_mode": True}, "test")
    assert set(engine.scanner.tradable()) == {"momentum", "breakout", "trend", "volume", "volatility"}
    engine.state._set("mode", "testnet")  # research mode never applies outside paper
    assert engine.scanner.tradable() == {}
    with pytest.raises(ValueError):
        engine.cfg.update("strategies", {"trend": {"promoted": True}}, "operator")
