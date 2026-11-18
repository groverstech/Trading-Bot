"""Walk-forward strategy research with out-of-sample validation.

Design (decided before looking at results):

* Simulation mirrors what the LIVE system would trade:
  - signals on closed candles only, entry at the NEXT bar's open;
  - higher-timeframe confirmation from timeframes strictly above the entry
    timeframe (resampled causally from the base candles) and the scanner's
    ``min_score``;
  - the Sizer's acceptance gate: net reward:risk after 2x taker fees and
    slippage must be >= ``min_net_rr`` (1.5 by default) or the trade is skipped;
  - stop-loss fills at the stop or a worse gap-open, plus slippage, taker fee;
    take-profit fills at the target (maker fee for the Spot OCO limit leg,
    taker + slippage for Futures TAKE_PROFIT_MARKET);
  - if a bar touches both stop and target, the stop is assumed first;
  - Futures: approximate funding of 0.01% per 8 h (longs pay, shorts receive).
* Walk-forward: rolling train/test windows. For each fold, parameters are
  chosen ONLY from trades whose entry lies in the train window (objective:
  total net R with a minimum trade count); the chosen parameters are then
  scored on the following, unseen test window. Out-of-sample (OOS) results
  are the concatenation of test windows. If no parameter set qualifies in
  training, the strategy does not trade that fold.
* Parameter grids are deliberately small (8 combinations) to limit
  data-mining. Every OOS trade is labelled with its market regime.

PROMOTION CRITERIA (all required, per strategy x timeframe x cost scenario):
  OOS trades >= 60, OOS profit factor >= 1.20, OOS expectancy >= +0.05 R,
  positive OOS net R in >= 60% of folds that traded, positive on >= 3 of 5
  symbols, OOS max drawdown <= 10% at 0.5% risk per trade.
"""
from __future__ import annotations

import itertools
import math
import time
from dataclasses import dataclass, field

import numpy as np

from ..market.data import TF_SECONDS, Candles
from ..strategy import indicators as ind
from ..strategy.strategies import STRATEGIES, Ctx, Signal, apply_htf

PROMOTION = {"min_trades": 60, "min_pf": 1.20, "min_expectancy_r": 0.05, "min_positive_fold_ratio": 0.60,
             "min_positive_symbols": 3, "max_drawdown_pct": 10.0}

GRIDS: dict[str, dict[str, list]] = {
    "momentum": {"rsi_long": [55, 60], "atr_stop": [1.5, 2.5], "rr": [2.0, 3.0]},
    "breakout": {"lookback": [20, 55], "atr_stop": [1.2, 2.0], "rr": [2.0, 3.0]},
    "trend": {"adx_min": [20, 25], "atr_stop": [1.5, 2.5], "rr": [2.0, 3.0]},
    "volume": {"vol_mult": [2.5, 3.5], "atr_stop": [1.3, 2.0], "rr": [2.0, 3.0]},
    "volatility": {"squeeze_pct": [10, 20], "atr_stop": [1.5, 2.5], "rr": [2.0, 3.0]},
}
FIXED = {"momentum": {"rsi_len": 14}, "breakout": {"vol_mult": 1.5}, "trend": {}, "volume": {"close_pos": 0.7},
         "volatility": {"expansion": 1.3}}

# train / test window lengths in bars per timeframe
WINDOWS = {"15m": (8640, 2880), "1h": (4320, 1440), "4h": (1080, 360)}

SCENARIOS = {
    "spot": {"taker": 0.001, "maker": 0.001, "allow_short": False, "funding_8h": 0.0, "tp_maker": True},
    "futures": {"taker": 0.0005, "maker": 0.0002, "allow_short": True, "funding_8h": 0.0001, "tp_maker": False},
    # Diagnostic only (never promotable): zero fees/funding, still 3 bps slippage.
    "nocost": {"taker": 0.0, "maker": 0.0, "allow_short": True, "funding_8h": 0.0, "tp_maker": True},
}


def combos(strategy: str) -> list[dict]:
    g = GRIDS[strategy]
    out = []
    for vals in itertools.product(*g.values()):
        p = dict(zip(g.keys(), vals)) | FIXED[strategy]
        if strategy == "momentum":
            p["rsi_short"] = 100 - p["rsi_long"]
        out.append(p)
    return out


# ------------------------------------------------------------------ helpers
def htf_trend_series(c: Candles, htf: str) -> np.ndarray:
    """Trend direction of the last CLOSED higher-timeframe candle at each base bar."""
    base_ms, h_ms = TF_SECONDS[c.timeframe] * 1000, TF_SECONDS[htf] * 1000
    per = h_ms // base_ms
    grp = (c.open_time // h_ms).astype(np.int64)
    # aggregate complete groups only
    keys, starts = np.unique(grp, return_index=True)
    closes, ends = [], []
    for k, s0 in zip(keys, starts):
        e0 = s0
        while e0 + 1 < len(grp) and grp[e0 + 1] == k:
            e0 += 1
        if e0 - s0 + 1 == per:
            closes.append(c.close[e0])
            ends.append(e0)
    closes = np.array(closes)
    out = np.zeros(len(c), dtype=np.int8)
    if len(closes) < 60:
        return out
    e50, e200 = ind.ema(closes, 50), ind.ema(closes, min(200, len(closes) - 1))
    dirs = np.zeros(len(closes), dtype=np.int8)
    for j in range(55, len(closes)):
        slope = e50[j] - e50[j - 5]
        if closes[j] > e50[j] > e200[j] and slope > 0:
            dirs[j] = 1
        elif closes[j] < e50[j] < e200[j] and slope < 0:
            dirs[j] = -1
    # base bar i sees the latest HTF group whose last base bar index <= i
    ends = np.array(ends)
    idx = np.searchsorted(ends, np.arange(len(c)), side="right") - 1
    valid = idx >= 0
    out[valid] = dirs[idx[valid]]
    return out


def regimes(c: Candles) -> tuple[np.ndarray, np.ndarray]:
    adx = ind.adx(c.high, c.low, c.close, 14)
    e50 = ind.ema(c.close, 50)
    slope = np.concatenate([[np.nan] * 5, e50[5:] - e50[:-5]])
    trend = np.where(np.nan_to_num(adx) >= 25, np.where(slope > 0, "trend_up", "trend_down"), "range")
    atrp = ind.atr(c.high, c.low, c.close, 14) / c.close
    med = np.array([np.nanmedian(atrp[max(0, i - 500):i + 1]) if i >= 50 else np.nan for i in range(len(c))])
    vol = np.where(atrp > med, "high_vol", "low_vol")
    return trend, vol


@dataclass
class Trade:
    symbol: str
    entry_i: int
    exit_i: int
    entry_t: int
    direction: str
    r: float            # net result in R (risk unit incl. costs)
    reason: str
    cost_r: float       # round-trip costs expressed in R
    regime: str = ""
    vol: str = ""
    params: dict = field(default_factory=dict)


def simulate(c: Candles, strategy: str, params: dict, sc: dict, htf: dict[str, np.ndarray],
             min_score: float = 0.55, min_net_rr: float = 1.5, slip_bps: float = 3.0,
             warmup: int = 210, ctx: Ctx | None = None, labels=None) -> tuple[list[Trade], dict]:
    fn = STRATEGIES[strategy]
    ctx = ctx or Ctx(c)
    slip = slip_bps / 1e4
    taker, maker = sc["taker"], sc["maker"]
    cost_frac = 2 * taker + slip            # same cost model as the live Sizer
    bars_8h = 8 * 3600 / TF_SECONDS[c.timeframe]
    trades: list[Trade] = []
    stats = {"signals": 0, "gated_rr": 0, "gated_score": 0, "gated_short": 0}
    n, i = len(c), warmup
    while i < n - 1:
        res = fn(ctx.at(i), params)
        if not isinstance(res, Signal):
            i += 1
            continue
        stats["signals"] += 1
        if res.direction == "SHORT" and not sc["allow_short"]:
            stats["gated_short"] += 1
            i += 1
            continue
        res = apply_htf(res, {k: int(v[i]) for k, v in htf.items()})
        if res.score < min_score:
            stats["gated_score"] += 1
            i += 1
            continue
        d = 1 if res.direction == "LONG" else -1
        stop_d, tgt_d = abs(res.entry - res.stop), abs(res.target - res.entry)
        entry_ref = res.entry
        unit = stop_d + entry_ref * cost_frac
        net_rr = (tgt_d - entry_ref * cost_frac) / unit if unit > 0 else 0
        if net_rr < min_net_rr:
            stats["gated_rr"] += 1
            i += 1
            continue
        # enter next bar open, keep the strategy's absolute stop/target distances
        e = c.open[i + 1] * (1 + d * slip)
        stop, target = e - d * stop_d, e + d * tgt_d
        risk_unit = stop_d + e * cost_frac   # what the Sizer budgets per unit
        j = i + 1
        exit_px, reason = None, None
        while j < n:
            o, h, l = c.open[j], c.high[j], c.low[j]
            if d == 1:
                if l <= stop:
                    exit_px, reason = min(stop, o) * (1 - slip), "stop_loss"
                elif h >= target:
                    exit_px, reason = (target if sc["tp_maker"] else target * (1 - slip)), "take_profit"
            else:
                if h >= stop:
                    exit_px, reason = max(stop, o) * (1 + slip), "stop_loss"
                elif l <= target:
                    exit_px, reason = (target if sc["tp_maker"] else target * (1 + slip)), "take_profit"
            if exit_px is not None:
                break
            j += 1
        if exit_px is None:
            j, exit_px, reason = n - 1, c.close[n - 1], "open_at_end"
        exit_fee = maker if (reason == "take_profit" and sc["tp_maker"]) else taker
        gross = (exit_px - e) * d
        fees = e * taker + exit_px * exit_fee
        funding = -d * e * sc["funding_8h"] * ((j - i) / bars_8h)
        net = gross - fees + funding
        lab = labels[0][i] if labels is not None else ""
        vol = labels[1][i] if labels is not None else ""
        trades.append(Trade(c.symbol, i + 1, j, int(c.open_time[i + 1]), res.direction, net / risk_unit, reason,
                            (fees - funding) / risk_unit, str(lab), str(vol), params))
        i = j + 1
    return trades, stats


# ------------------------------------------------------------------ metrics
def metrics(trades: list[Trade], risk_pct: float = 0.5) -> dict:
    rs = [t.r for t in trades]
    if not rs:
        return {"trades": 0, "net_r": 0.0, "pf": None, "win_rate": None, "expectancy_r": None, "max_dd_pct": 0.0,
                "return_pct": 0.0, "avg_cost_r": None}
    wins, losses = [r for r in rs if r > 0], [r for r in rs if r <= 0]
    eq, peak, mdd = 1.0, 1.0, 0.0
    for t in sorted(trades, key=lambda t: t.entry_t):
        eq *= 1 + risk_pct / 100 * t.r
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak * 100)
    gl = -sum(losses)
    return {"trades": len(rs), "net_r": float(sum(rs)), "pf": float(sum(wins) / gl) if gl > 0 else None,
            "win_rate": len(wins) / len(rs) * 100, "expectancy_r": float(np.mean(rs)), "max_dd_pct": float(mdd),
            "return_pct": float((eq - 1) * 100), "avg_cost_r": float(np.mean([t.cost_r for t in trades]))}


def breakeven_win_rate(trades: list[Trade]) -> float | None:
    """Win rate needed to break even given the realised average win and loss."""
    w = [t.r for t in trades if t.r > 0]
    l = [-t.r for t in trades if t.r <= 0]
    if not w or not l:
        return None
    aw, al = np.mean(w), np.mean(l)
    return float(al / (aw + al) * 100)


# ---------------------------------------------------------------- walk-forward
def walk_forward(data: dict[str, Candles], tf: str, strategy: str, scenario: str,
                 min_train_trades: int = 10) -> dict:
    """``data``: symbol -> Candles (same timeframe)."""
    sc = SCENARIOS[scenario]
    train, test = WINDOWS[tf]
    confirm = [h for h in ("1h", "4h", "1d") if TF_SECONDS[h] > TF_SECONDS[tf]][:2]
    # simulate every combo once per symbol over the full history
    per_combo: dict[int, list[Trade]] = {}
    signal_stats = {"signals": 0, "gated_rr": 0, "gated_score": 0, "gated_short": 0}
    cs = combos(strategy)
    for sym, c in data.items():
        htf = {h: htf_trend_series(c, h) for h in confirm}
        labels = regimes(c)
        ctx = Ctx(c)
        for k, p in enumerate(cs):
            tr, st = simulate(c, strategy, p, sc, htf, ctx=ctx, labels=labels)
            per_combo.setdefault(k, []).extend(tr)
            if k == 0:
                for kk in signal_stats:
                    signal_stats[kk] += st[kk]
    # common time axis from the shortest series
    t0 = max(int(c.open_time[0]) for c in data.values())
    t_end = min(int(c.open_time[-1]) for c in data.values())
    bar_ms = TF_SECONDS[tf] * 1000
    folds, oos = [], []
    start = t0 + 210 * bar_ms
    while start + (train + test) * bar_ms <= t_end + bar_ms:
        tr_a, tr_b = start, start + train * bar_ms
        te_a, te_b = tr_b, tr_b + test * bar_ms
        best_k, best_score = None, -math.inf
        for k, trades in per_combo.items():
            ins = [t for t in trades if tr_a <= t.entry_t < tr_b]
            if len(ins) < min_train_trades:
                continue
            score = sum(t.r for t in ins)
            if score > best_score:
                best_k, best_score = k, score
        chosen = [t for t in per_combo[best_k] if te_a <= t.entry_t < te_b] if best_k is not None else []
        oos.extend(chosen)
        folds.append({"train": [tr_a, tr_b], "test": [te_a, te_b], "params": cs[best_k] if best_k is not None else None,
                      "train_net_r": best_score if best_k is not None else None, "oos": metrics(chosen)})
        start += test * bar_ms
    m = metrics(oos)
    by_sym = {s: metrics([t for t in oos if t.symbol == s]) for s in data}
    by_regime = {g: metrics([t for t in oos if t.regime == g]) for g in ("trend_up", "trend_down", "range")}
    by_vol = {g: metrics([t for t in oos if t.vol == g]) for g in ("high_vol", "low_vol")}
    by_dir = {g: metrics([t for t in oos if t.direction == g]) for g in ("LONG", "SHORT")}
    traded = [f for f in folds if f["oos"]["trades"] > 0]
    pos_folds = sum(1 for f in traded if f["oos"]["net_r"] > 0)
    pos_syms = sum(1 for v in by_sym.values() if v["trades"] and v["net_r"] > 0)
    checks = {
        "min_trades": m["trades"] >= PROMOTION["min_trades"],
        "min_pf": (m["pf"] or 0) >= PROMOTION["min_pf"],
        "min_expectancy_r": (m["expectancy_r"] or -1) >= PROMOTION["min_expectancy_r"],
        "positive_folds": bool(traded) and pos_folds / len(traded) >= PROMOTION["min_positive_fold_ratio"],
        "positive_symbols": pos_syms >= PROMOTION["min_positive_symbols"],
        "max_drawdown": m["max_dd_pct"] <= PROMOTION["max_drawdown_pct"],
    }
    # in-sample (all combos, full history) to expose the in-sample vs OOS gap
    best_is = max((metrics(v) for v in per_combo.values()), key=lambda x: x["net_r"], default=metrics([]))
    default_trades = per_combo[0]
    checks = {k: bool(v) for k, v in checks.items()}
    if scenario == "nocost":
        checks["diagnostic_only"] = False
    tot = sum(v["net_r"] for v in by_sym.values())
    top_sym = max(by_sym, key=lambda s: by_sym[s]["net_r"]) if by_sym else None
    return {
        "strategy": strategy, "timeframe": tf, "scenario": scenario, "symbols": list(data),
        "confirm_timeframes": confirm, "folds": folds, "oos": m, "oos_by_symbol": by_sym,
        "oos_by_regime": by_regime, "oos_by_volatility": by_vol, "oos_by_direction": by_dir,
        "positive_folds": f"{pos_folds}/{len(traded)}", "positive_symbols": pos_syms,
        "breakeven_win_rate": breakeven_win_rate(oos), "best_in_sample": best_is,
        "signal_funnel_first_combo": signal_stats, "default_combo_all_history": metrics(default_trades),
        "checks": checks, "promote": all(checks.values()),
        "concentration": {"top_symbol": top_sym, "top_symbol_net_r": by_sym[top_sym]["net_r"] if top_sym else None,
                          "net_r_excluding_top": tot - (by_sym[top_sym]["net_r"] if top_sym else 0)},
        "latest_params": next((f["params"] for f in reversed(folds) if f["params"]), None),
        "trades_per_1000_bars": m["trades"] / max(1, sum(len(c) for c in data.values())) * 1000,
    }


async def fetch_history(adapter, symbol: str, tf: str, bars: int) -> Candles:
    from ..strategy.backtest import fetch_history as fh
    return await fh(adapter, symbol, tf, bars)


def now_ts() -> float:
    return time.time()
