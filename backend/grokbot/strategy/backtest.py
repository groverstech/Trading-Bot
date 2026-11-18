"""Historical backtester using the same strategy code as live scanning.

Bias controls:
* At bar i only candles [0..i] (closed) are visible to the strategy.
* Entry is at the OPEN of bar i+1 (the next tradable price), never at the
  signal bar's close.
* If a bar touches both stop and target, the stop is assumed to fill first.
* Fees on both sides and adverse slippage are applied.
* Higher-timeframe confirmation is NOT applied (single-timeframe test), and
  results are labelled as hypothetical: past performance does not predict
  live results.
"""
from __future__ import annotations

import math

import numpy as np

from ..market.data import Candles
from .strategies import STRATEGIES, Ctx, Signal

WINDOW = 300


def run(c: Candles, strategy: str, params: dict, *, allow_short: bool, fee_rate: float, slippage_bps: float,
        risk_pct: float = 0.5, start_equity: float = 1000.0, warmup: int = 210) -> dict:
    fn = STRATEGIES[strategy]
    slip = slippage_bps / 1e4
    equity = start_equity
    curve = [{"t": int(c.open_time[min(warmup, len(c) - 1)]) if len(c) else 0, "equity": equity}]
    trades = []
    pos = None
    n = len(c)
    i = warmup
    while i < n - 1:
        if pos is None:
            lo = max(0, i + 1 - WINDOW)
            window = Candles(  # closed candles ending at bar i - nothing after it is visible
                c.symbol, c.timeframe, c.open_time[lo:i + 1], c.open[lo:i + 1], c.high[lo:i + 1], c.low[lo:i + 1],
                c.close[lo:i + 1], c.volume[lo:i + 1], c.quote_volume[lo:i + 1], c.close_time[lo:i + 1], c.fetched_at)
            res = fn(Ctx.build(window), params)
            if isinstance(res, Signal) and (allow_short or res.direction == "LONG"):
                d = 1 if res.direction == "LONG" else -1
                entry = c.open[i + 1] * (1 + d * slip)
                # keep the strategy's stop/target distances relative to the actual entry
                stop = entry - d * abs(res.entry - res.stop)
                target = entry + d * abs(res.target - res.entry)
                risk_amt = equity * risk_pct / 100
                unit = abs(entry - stop) + entry * 2 * fee_rate
                qty = risk_amt / unit if unit > 0 else 0
                if qty > 0:
                    pos = {"d": d, "entry": entry, "stop": stop, "target": target, "qty": qty, "i": i + 1,
                           "t": int(c.open_time[i + 1]), "reasons": res.reasons}
            i += 1
            continue
        # manage open position on bar i (from entry bar onwards)
        hi, lo_ = c.high[i], c.low[i]
        exit_px, reason = None, None
        if pos["d"] == 1:
            if lo_ <= pos["stop"]:
                exit_px, reason = min(pos["stop"], c.open[i]) * (1 - slip), "stop_loss"
            elif hi >= pos["target"]:
                exit_px, reason = pos["target"], "take_profit"
        else:
            if hi >= pos["stop"]:
                exit_px, reason = max(pos["stop"], c.open[i]) * (1 + slip), "stop_loss"
            elif lo_ <= pos["target"]:
                exit_px, reason = pos["target"], "take_profit"
        if exit_px is not None:
            gross = (exit_px - pos["entry"]) * pos["qty"] * pos["d"]
            fees = (pos["entry"] + exit_px) * pos["qty"] * fee_rate
            pnl = gross - fees
            equity += pnl
            trades.append({"entry_time": pos["t"], "exit_time": int(c.open_time[i]),
                           "direction": "LONG" if pos["d"] == 1 else "SHORT", "entry": pos["entry"],
                           "exit": exit_px, "pnl": pnl, "fees": fees, "reason": reason, "bars": i - pos["i"] + 1})
            curve.append({"t": int(c.open_time[i]), "equity": equity})
            pos = None
            # re-evaluate from this bar's close for a new entry
        i += 1
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    peak, mdd = start_equity, 0.0
    for pt in curve:
        peak = max(peak, pt["equity"])
        mdd = max(mdd, (peak - pt["equity"]) / peak * 100)
    gl = -sum(losses)
    return {
        "strategy": strategy, "symbol": c.symbol, "timeframe": c.timeframe, "bars": n,
        "from": int(c.open_time[0]) if n else None, "to": int(c.close_time[-1]) if n else None,
        "trades": len(trades), "win_rate": len(wins) / len(trades) * 100 if trades else None,
        "net_return_pct": (equity - start_equity) / start_equity * 100,
        "profit_factor": (sum(wins) / gl if gl > 0 else None) if trades else None,
        "max_drawdown_pct": mdd, "avg_bars_held": float(np.mean([t["bars"] for t in trades])) if trades else None,
        "fees_paid": sum(t["fees"] for t in trades), "open_position_at_end": pos is not None,
        "equity_curve": curve, "trade_list": trades[-200:],
        "assumptions": {"fee_rate": fee_rate, "slippage_bps": slippage_bps, "risk_pct": risk_pct,
                        "entry": "next bar open", "intrabar": "stop assumed before target",
                        "htf_filter": "not applied", "note": "hypothetical results; not indicative of future performance"},
    }


async def fetch_history(adapter, symbol: str, tf: str, bars: int) -> Candles:
    """Page backwards through klines (1000 per request)."""
    import time
    raw: list = []
    end = None
    while len(raw) < bars:
        lim = min(1000, bars - len(raw))
        chunk = await adapter.klines(symbol, tf, limit=lim, end_time=end)
        if not chunk:
            break
        raw = chunk + raw
        end = int(chunk[0][0]) - 1
        if len(chunk) < lim:
            break
    return Candles.from_raw(symbol, tf, raw, int(time.time() * 1000))


def safe(obj):
    """Replace NaN/inf for JSON."""
    if isinstance(obj, float):
        return None if math.isnan(obj) or math.isinf(obj) else obj
    if isinstance(obj, dict):
        return {k: safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [safe(v) for v in obj]
    return obj
