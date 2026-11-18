"""Performance analytics computed exclusively from recorded data
(closed positions, ledger and equity snapshots) for one mode at a time.
Nothing is estimated or back-filled; empty data yields empty statistics."""
from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict

from .db import Database
from .util import D


def _day(ts: float, fmt: str = "%Y-%m-%d") -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime(fmt)


def trade_stats(rows: list[dict]) -> dict:
    pnls = [float(D(r["realized_pnl"])) for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gw, gl = sum(wins), -sum(losses)
    hold = [r["closed_at"] - r["opened_at"] for r in rows if r.get("closed_at")]
    return {
        "trades": len(pnls), "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / len(pnls) * 100 if pnls else None,
        "net_pnl": sum(pnls), "gross_profit": gw, "gross_loss": gl,
        "profit_factor": (gw / gl if gl > 0 else (None if not wins else float("inf"))) if pnls else None,
        "avg_win": gw / len(wins) if wins else None, "avg_loss": -gl / len(losses) if losses else None,
        "expectancy": sum(pnls) / len(pnls) if pnls else None,
        "largest_win": max(wins) if wins else None, "largest_loss": min(losses) if losses else None,
        "avg_holding_s": sum(hold) / len(hold) if hold else None,
        "fees": sum(float(D(r["fees"])) for r in rows),
        "slippage": sum(float(D(r["slippage"])) for r in rows),
        "funding": sum(float(D(r["funding"])) for r in rows),
    }


def max_drawdown(series: list[float]) -> tuple[float, float]:
    """(max drawdown %, current drawdown %)."""
    peak, mdd = -math.inf, 0.0
    for v in series:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak * 100)
    cur = (peak - series[-1]) / peak * 100 if series and peak > 0 else 0.0
    return mdd, cur


def period_returns(points: list[tuple[float, float]], fmt: str) -> list[dict]:
    """Return per period from the last equity value of each period."""
    last: dict[str, float] = {}
    first_val = points[0][1] if points else None
    for ts, v in points:
        last[_day(ts, fmt)] = v
    out, prev = [], first_val
    for k in sorted(last):
        v = last[k]
        out.append({"period": k, "equity": v, "return_pct": (v - prev) / prev * 100 if prev else 0.0})
        prev = v
    return out


def compute(db: Database, mode: str, starting: float) -> dict:
    closed = db.all("SELECT * FROM positions WHERE mode=? AND status='CLOSED' ORDER BY closed_at", (mode,))
    snaps = db.all("SELECT ts,spot,futures,reserve,total FROM equity_snapshots WHERE mode=? ORDER BY ts", (mode,))
    by_market = {m: trade_stats([r for r in closed if r["market"] == m]) for m in ("spot", "futures")}
    by_strategy = defaultdict(list)
    for r in closed:
        by_strategy[r["strategy"]].append(r)
    total_series = [s["total"] for s in snaps]
    mdd, cdd = max_drawdown(total_series) if total_series else (0.0, 0.0)
    mdd_m = {m: max_drawdown([s[m] for s in snaps])[0] if snaps else 0.0 for m in ("spot", "futures")}
    pts = [(s["ts"], s["total"]) for s in snaps]
    daily = period_returns(pts, "%Y-%m-%d")
    weekly = period_returns(pts, "%G-W%V")
    monthly = period_returns(pts, "%Y-%m")
    sharpe = None
    sharpe_note = "requires at least 14 daily returns"
    if len(daily) >= 15:
        rets = [d["return_pct"] / 100 for d in daily[1:]]
        mu = sum(rets) / len(rets)
        sd = (sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)) ** 0.5
        if sd > 0:
            sharpe = mu / sd * math.sqrt(365)
            sharpe_note = f"annualised from {len(rets)} daily returns (crypto trades 365 days)"
    current = total_series[-1] if total_series else starting
    stages = db.all("SELECT stage, COUNT(*) n FROM trades WHERE mode=? GROUP BY stage", (mode,))
    rejections = db.all(
        "SELECT agent, event, COUNT(*) n FROM agent_events WHERE mode=? AND event IN "
        "('rejected','risk_rejected','execution_refused','execution_failed') GROUP BY agent, event", (mode,))
    agent_counts = db.all("SELECT agent, COUNT(*) n FROM agent_events WHERE mode=? GROUP BY agent", (mode,))
    return {
        "mode": mode, "is_simulated": mode == "paper", "is_real_money": mode == "live",
        "starting_equity": starting, "current_equity": current,
        "total_return_pct": (current - starting) / starting * 100 if starting else None,
        "consolidated": trade_stats(closed), "spot": by_market["spot"], "futures": by_market["futures"],
        "max_drawdown_pct": mdd, "current_drawdown_pct": cdd,
        "max_drawdown_by_market": mdd_m,
        "daily_returns": daily[-60:], "weekly_returns": weekly[-26:], "monthly_returns": monthly[-24:],
        "sharpe": sharpe, "sharpe_note": sharpe_note,
        "by_strategy": {k: trade_stats(v) for k, v in by_strategy.items()},
        "pipeline": {r["stage"]: r["n"] for r in stages},
        "rejections": rejections, "agent_activity": {r["agent"]: r["n"] for r in agent_counts},
    }


def equity_curve(db: Database, mode: str, limit: int = 2000) -> list[dict]:
    rows = db.all("SELECT ts,reason,spot,futures,reserve,total,realized,unrealized FROM equity_snapshots "
                  "WHERE mode=? ORDER BY ts DESC LIMIT ?", (mode, limit))
    return rows[::-1]
