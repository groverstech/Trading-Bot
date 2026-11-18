"""Strategy engine: five rule-based, explainable strategies.

Every strategy evaluates CLOSED candles only and returns either a ``Signal``
(with entry, stop, target, score and human-readable reasons) or a
``NoTrade`` explaining why conditions were not met. Strategies never place
orders; they only propose. Risk and sizing are decided downstream.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from ..market.data import Candles
from . import indicators as ind


@dataclass
class Signal:
    strategy: str
    direction: str            # LONG | SHORT
    entry: float
    stop: float
    target: float
    score: float
    reasons: list[str]
    indicators: dict = field(default_factory=dict)

    @property
    def reward_risk(self) -> float:
        risk = abs(self.entry - self.stop)
        return abs(self.target - self.entry) / risk if risk > 0 else 0.0

    def as_dict(self) -> dict:
        return {"strategy": self.strategy, "direction": self.direction, "entry": self.entry,
                "stop": self.stop, "target": self.target, "score": round(self.score, 3),
                "reward_risk": round(self.reward_risk, 2), "reasons": self.reasons,
                "indicators": self.indicators}


@dataclass
class NoTrade:
    strategy: str
    reason: str


def _f(x) -> float:
    return float(x) if x is not None and not math.isnan(x) else float("nan")


class Ctx:
    """Indicator context for evaluating strategies at one bar.

    Indicators are computed ONCE over the full candle series (all are causal,
    see test_indicators_are_causal_no_lookahead) and exposed as views ending
    at ``end``. ``at(i)`` gives the context as it was when bar i closed,
    sharing the cache, which makes walk-forward backtests fast and keeps them
    free of look-ahead. Parameter-dependent series (e.g. RSI length) are
    cached by key.
    """

    def __init__(self, c: Candles, end: int | None = None, cache: dict | None = None):
        self.full = c
        self.end = len(c) if end is None else end
        self.cache = cache if cache is not None else {}

    @classmethod
    def build(cls, c: Candles) -> "Ctx":
        return cls(c)

    def at(self, i: int) -> "Ctx":
        return Ctx(self.full, i + 1, self.cache)

    def _series(self, key, fn) -> np.ndarray:
        arr = self.cache.get(key)
        if arr is None:
            arr = self.cache[key] = fn(self.full)
        return arr[: self.end]

    @property
    def c(self) -> Candles:
        return self.full if self.end == len(self.full) else self.full.slice(self.end)

    @property
    def close(self): return self.full.close[: self.end]
    @property
    def atr(self): return self._series("atr14", lambda c: ind.atr(c.high, c.low, c.close, 14))
    @property
    def atr50(self): return self._series("atr50", lambda c: ind.atr(c.high, c.low, c.close, 50))
    @property
    def ema20(self): return self._series("ema20", lambda c: ind.ema(c.close, 20))
    @property
    def ema50(self): return self._series("ema50", lambda c: ind.ema(c.close, 50))
    @property
    def ema200(self): return self._series("ema200", lambda c: ind.ema(c.close, min(200, max(len(c) - 1, 1))))
    @property
    def rsi(self): return self.rsi_n(14)
    @property
    def macd_hist(self): return self._series("macd", lambda c: ind.macd(c.close)[2])
    @property
    def adx(self): return self._series("adx", lambda c: ind.adx(c.high, c.low, c.close, 14))
    @property
    def vwap(self): return self._series("vwap", lambda c: ind.vwap_session(c.open_time, c.high, c.low, c.close, c.volume))
    @property
    def vol_ma(self): return self._series("volma", lambda c: ind.sma(c.volume, 20))
    @property
    def bbw(self): return self._series("bbw", lambda c: ind.bb_width(c.close, 20))

    def rsi_n(self, n: int):
        return self._series(f"rsi{n}", lambda c: ind.rsi(c.close, n))

    def snapshot(self) -> dict:
        i = -1
        vm = self.vol_ma
        return {"close": _f(self.close[i]), "atr": _f(self.atr[i]), "ema20": _f(self.ema20[i]),
                "ema50": _f(self.ema50[i]), "ema200": _f(self.ema200[i]), "rsi": _f(self.rsi[i]),
                "macd_hist": _f(self.macd_hist[i]), "adx": _f(self.adx[i]), "vwap": _f(self.vwap[i]),
                "vol_ratio": _f(self.full.volume[self.end - 1] / vm[i]) if vm[i] else float("nan")}


def _bracket(name: str, direction: str, entry: float, atr: float, p: dict, score: float,
             reasons: list[str], ctx: Ctx) -> Signal:
    d = 1 if direction == "LONG" else -1
    stop = entry - d * p.get("atr_stop", 1.5) * atr
    target = entry + d * p.get("rr", 2.0) * abs(entry - stop)
    return Signal(name, direction, entry, stop, target, max(0.0, min(1.0, score)), reasons, ctx.snapshot())


def momentum(ctx: Ctx, p: dict) -> Signal | NoTrade:
    rs = ctx.rsi_n(int(p.get("rsi_len", 14)))
    r, r1, h, h1 = rs[-1], rs[-2], ctx.macd_hist[-1], ctx.macd_hist[-2]
    px, e50, a = ctx.close[-1], ctx.ema50[-1], ctx.atr[-1]
    if np.isnan([r, r1, h, h1, e50, a]).any():
        return NoTrade("momentum", "insufficient history")
    lo, hi = p.get("rsi_short", 45), p.get("rsi_long", 55)
    if r1 < hi <= r and h > 0 and h > h1 and px > e50:
        s = 0.5 + min((r - hi) / 30, 0.15) + min(h / a, 0.15)
        return _bracket("momentum", "LONG", px, a, p, s,
                        [f"RSI crossed above {hi:g} ({r1:.1f}→{r:.1f})", "MACD histogram positive and rising",
                         "price above EMA50"], ctx)
    if r1 > lo >= r and h < 0 and h < h1 and px < e50:
        s = 0.5 + min((lo - r) / 30, 0.15) + min(-h / a, 0.15)
        return _bracket("momentum", "SHORT", px, a, p, s,
                        [f"RSI crossed below {lo:g} ({r1:.1f}→{r:.1f})", "MACD histogram negative and falling",
                         "price below EMA50"], ctx)
    return NoTrade("momentum", f"no RSI/MACD momentum shift (RSI {r:.1f})")


def breakout(ctx: Ctx, p: dict) -> Signal | NoTrade:
    n = int(p.get("lookback", 20))
    c = ctx.c
    if len(c) < n + 2 or np.isnan(ctx.atr[-1]) or np.isnan(ctx.vol_ma[-2]):
        return NoTrade("breakout", "insufficient history")
    prior_high, prior_low = c.high[-n - 1:-1].max(), c.low[-n - 1:-1].min()
    px, a = c.close[-1], ctx.atr[-1]
    vr = c.volume[-1] / ctx.vol_ma[-2] if ctx.vol_ma[-2] else 0
    need = p.get("vol_mult", 1.5)
    if px > prior_high and vr >= need:
        s = 0.55 + min((px - prior_high) / a * 0.1, 0.15) + min((vr - need) * 0.05, 0.1)
        return _bracket("breakout", "LONG", px, a, p, s,
                        [f"close {px:.6g} broke {n}-bar high {prior_high:.6g}", f"volume {vr:.1f}x average"], ctx)
    if px < prior_low and vr >= need:
        s = 0.55 + min((prior_low - px) / a * 0.1, 0.15) + min((vr - need) * 0.05, 0.1)
        return _bracket("breakout", "SHORT", px, a, p, s,
                        [f"close {px:.6g} broke {n}-bar low {prior_low:.6g}", f"volume {vr:.1f}x average"], ctx)
    return NoTrade("breakout", f"inside {n}-bar range or volume {vr:.1f}x < {need:g}x")


def trend(ctx: Ctx, p: dict) -> Signal | NoTrade:
    c = ctx.c
    e20, e50, e200, adx, a = ctx.ema20[-1], ctx.ema50[-1], ctx.ema200[-1], ctx.adx[-1], ctx.atr[-1]
    if len(c) < 60 or np.isnan([e20, e50, e200, adx, a]).any():
        return NoTrade("trend", "insufficient history")
    need = p.get("adx_min", 20)
    if adx < need:
        return NoTrade("trend", f"ADX {adx:.1f} below {need:g}: no trend")
    px = c.close[-1]
    # Pullback: the previous bar touched EMA20 and this bar closed back in trend direction.
    if e20 > e50 > e200 and c.low[-2] <= ctx.ema20[-2] and px > e20 and px > c.open[-1]:
        return _bracket("trend", "LONG", px, a, p, 0.5 + min((adx - need) / 50, 0.25),
                        ["EMA20 > EMA50 > EMA200 uptrend", f"ADX {adx:.1f}", "pullback to EMA20 rejected"], ctx)
    if e20 < e50 < e200 and c.high[-2] >= ctx.ema20[-2] and px < e20 and px < c.open[-1]:
        return _bracket("trend", "SHORT", px, a, p, 0.5 + min((adx - need) / 50, 0.25),
                        ["EMA20 < EMA50 < EMA200 downtrend", f"ADX {adx:.1f}", "pullback to EMA20 rejected"], ctx)
    return NoTrade("trend", "trend present but no pullback entry")


def volume(ctx: Ctx, p: dict) -> Signal | NoTrade:
    c = ctx.c
    if len(c) < 25 or np.isnan(ctx.vol_ma[-2]) or np.isnan(ctx.atr[-1]):
        return NoTrade("volume", "insufficient history")
    vr = c.volume[-1] / ctx.vol_ma[-2] if ctx.vol_ma[-2] else 0
    need = p.get("vol_mult", 2.5)
    rng = c.high[-1] - c.low[-1]
    if vr < need or rng <= 0:
        return NoTrade("volume", f"volume {vr:.1f}x < {need:g}x")
    pos = (c.close[-1] - c.low[-1]) / rng
    cp, px, a, vw = p.get("close_pos", 0.7), c.close[-1], ctx.atr[-1], ctx.vwap[-1]
    if pos >= cp and px > vw:
        return _bracket("volume", "LONG", px, a, p, 0.5 + min((vr - need) * 0.05, 0.2) + (pos - cp) * 0.3,
                        [f"volume surge {vr:.1f}x", f"closed at {100 * pos:.0f}% of bar range", "above VWAP"], ctx)
    if pos <= 1 - cp and px < vw:
        return _bracket("volume", "SHORT", px, a, p, 0.5 + min((vr - need) * 0.05, 0.2) + (1 - cp - pos) * 0.3,
                        [f"volume surge {vr:.1f}x", f"closed at {100 * pos:.0f}% of bar range", "below VWAP"], ctx)
    return NoTrade("volume", f"volume surge {vr:.1f}x without directional close")


def volatility(ctx: Ctx, p: dict) -> Signal | NoTrade:
    c = ctx.c
    if len(c) < 120:
        return NoTrade("volatility", "insufficient history")
    bw = ctx.bbw
    hist = bw[-101:-1]
    hist = hist[~np.isnan(hist)]
    if len(hist) < 50:
        return NoTrade("volatility", "insufficient history")
    a14, a50 = ctx.atr[-1], ctx.atr50[-1]
    pct = (hist < bw[-2]).mean() * 100  # squeeze percentile of the previous bar
    exp = a14 / a50 if a50 else 0
    need_sq, need_exp = p.get("squeeze_pct", 20), p.get("expansion", 1.3)
    if pct > need_sq:
        return NoTrade("volatility", f"no squeeze (band width at {pct:.0f}th percentile)")
    if exp < need_exp and (c.high[-1] - c.low[-1]) < need_exp * a14:
        return NoTrade("volatility", f"squeeze without expansion (ATR ratio {exp:.2f})")
    px, e20 = c.close[-1], ctx.ema20[-1]
    direction = "LONG" if px > e20 and px > c.open[-1] else "SHORT" if px < e20 and px < c.open[-1] else None
    if not direction:
        return NoTrade("volatility", "expansion bar has no clear direction")
    return _bracket("volatility", direction, px, a14, p, 0.55 + min((need_sq - pct) / 100, 0.15),
                    [f"volatility squeeze ({pct:.0f}th pct band width)", f"expansion bar (ATR ratio {exp:.2f})",
                     f"close {'above' if direction == 'LONG' else 'below'} EMA20"], ctx)


STRATEGIES = {
    "momentum": momentum,
    "breakout": breakout,
    "trend": trend,
    "volume": volume,
    "volatility": volatility,
}

STRATEGY_INFO = {
    "momentum": "RSI crossing 55/45 with MACD histogram confirmation and EMA50 filter",
    "breakout": "Close beyond N-bar high/low on above-average volume",
    "trend": "EMA20/50/200 alignment with ADX filter; enters on pullback to EMA20",
    "volume": "Volume surge with directional close relative to session VWAP",
    "volatility": "Bollinger squeeze followed by ATR expansion bar",
}


def apply_htf(sig: Signal, htf: dict[str, int]) -> Signal:
    """Adjust score using higher-timeframe trend alignment."""
    d = 1 if sig.direction == "LONG" else -1
    for tf, t in htf.items():
        if t == d:
            sig.score = min(1.0, sig.score + 0.08)
            sig.reasons.append(f"{tf} trend aligned")
        elif t == -d:
            sig.score = max(0.0, sig.score - 0.15)
            sig.reasons.append(f"{tf} trend opposes")
    return sig


def evaluate(c: Candles, enabled: dict[str, dict], htf: dict[str, int] | None = None,
             allow_short: bool = True) -> tuple[list[Signal], list[NoTrade]]:
    if len(c) < 30:
        return [], [NoTrade(n, "insufficient history") for n in enabled]
    ctx = Ctx.build(c)
    sigs, nots = [], []
    for name, params in enabled.items():
        res = STRATEGIES[name](ctx, params)
        if isinstance(res, Signal):
            if res.direction == "SHORT" and not allow_short:
                nots.append(NoTrade(name, "short signal ignored: spot market is long-only"))
                continue
            sigs.append(apply_htf(res, htf or {}))
        else:
            nots.append(res)
    return sigs, nots
