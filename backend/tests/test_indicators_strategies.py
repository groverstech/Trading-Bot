import time

import numpy as np
import pytest

from fake_exchange import synthetic_klines
from grokbot.market.data import Candles
from grokbot.strategy import backtest as bt
from grokbot.strategy import indicators as ind
from grokbot.strategy.strategies import Ctx, breakout, evaluate


def test_ema_sma_rsi_basic():
    x = np.arange(1, 31, dtype=float)
    assert ind.sma(x, 5)[4] == pytest.approx(3.0)
    assert np.isnan(ind.ema(x, 10)[8]) and ind.ema(x, 10)[9] == pytest.approx(5.5)
    r = ind.rsi(x, 14)
    assert r[-1] == pytest.approx(100.0)  # monotonic rise -> RSI 100
    r2 = ind.rsi(x[::-1].copy(), 14)
    assert r2[-1] == pytest.approx(0.0)


@pytest.mark.safety
def test_indicators_are_causal_no_lookahead():
    rng = np.random.default_rng(1)
    close = 100 + np.cumsum(rng.normal(0, 1, 400))
    high, low = close + 1, close - 1
    vol = rng.uniform(1, 2, 400)
    t = np.arange(400) * 60_000.0
    full = {"ema": ind.ema(close, 20), "rsi": ind.rsi(close), "atr": ind.atr(high, low, close),
            "adx": ind.adx(high, low, close), "macd": ind.macd(close)[2], "vwap": ind.vwap_session(t, high, low, close, vol)}
    k = 250
    part = {"ema": ind.ema(close[:k], 20), "rsi": ind.rsi(close[:k]), "atr": ind.atr(high[:k], low[:k], close[:k]),
            "adx": ind.adx(high[:k], low[:k], close[:k]), "macd": ind.macd(close[:k])[2],
            "vwap": ind.vwap_session(t[:k], high[:k], low[:k], close[:k], vol[:k])}
    for name in full:
        np.testing.assert_allclose(full[name][:k], part[name], equal_nan=True, err_msg=name)


@pytest.mark.safety
def test_forming_candle_is_dropped():
    now_ms = int(time.time() * 1000)
    raw = synthetic_klines(50, 100, 0, 60_000, now_ms // 60_000 * 60_000 + 60_000)
    c = Candles.from_raw("X", "1m", raw, now_ms)
    assert len(c) == 49 and c.close_time[-1] < now_ms


def _candles(raw):
    return Candles.from_raw("BTCUSDT", "15m", raw, 10**15)


def test_breakout_long_and_spot_short_suppressed():
    raw = synthetic_klines(120, 100, 0.0, 900_000, 10**12, vol=0.001)
    # last bar: strong close above the 20-bar high on 3x volume
    last = raw[-1]
    hi = max(float(r[2]) for r in raw[-21:-1])
    last[4] = f"{hi * 1.01:.2f}"
    last[2] = f"{hi * 1.012:.2f}"
    last[5] = "400"
    c = _candles(raw)
    sig = breakout(Ctx.build(c), {"lookback": 20, "vol_mult": 1.5, "atr_stop": 1.2, "rr": 2})
    assert sig.direction == "LONG" and sig.stop < sig.entry < sig.target
    assert sig.reward_risk == pytest.approx(2.0)
    # mirror -> SHORT, which must be ignored for spot
    raw2 = synthetic_klines(120, 100, 0.0, 900_000, 10**12, vol=0.001)
    lo = min(float(r[3]) for r in raw2[-21:-1])
    raw2[-1][4], raw2[-1][3], raw2[-1][5] = f"{lo * 0.99:.2f}", f"{lo * 0.988:.2f}", "400"
    sigs, nots = evaluate(_candles(raw2), {"breakout": {"lookback": 20, "vol_mult": 1.5}}, allow_short=False)
    assert not sigs and "long-only" in nots[0].reason


def test_flat_market_yields_no_trade_decisions():
    raw = [[i * 900_000, "100", "100.1", "99.9", "100", "10", i * 900_000 + 899_999, "1000", 1, 0, 0, 0] for i in range(300)]
    sigs, nots = evaluate(_candles(raw), {n: {} for n in ("momentum", "breakout", "trend", "volume", "volatility")})
    assert sigs == [] and len(nots) == 5 and all(n.reason for n in nots)


def test_backtest_enters_next_bar_open_and_charges_fees():
    raw = synthetic_klines(600, 100, 0.0005, 3_600_000, 10**12, vol=0.01)
    c = Candles.from_raw("BTCUSDT", "1h", raw, 10**15)
    r = bt.run(c, "momentum", {"rsi_long": 55, "rsi_short": 45, "atr_stop": 1.5, "rr": 2}, allow_short=True,
               fee_rate=0.001, slippage_bps=5)
    assert r["assumptions"]["entry"] == "next bar open"
    for t in r["trade_list"]:
        assert t["fees"] > 0
        assert t["entry_time"] > 0
