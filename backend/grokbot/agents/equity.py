"""AGENT 6 - EQUITY.

Keeps portfolio equity current: snapshots after every execution/settlement
and periodically, maintains the equity curve, computes performance metrics
from recorded transactions, and watches the daily-loss and drawdown limits.
A breach triggers the emergency halt via the orchestrator.
"""
from __future__ import annotations

import time

from .. import analytics
from ..db import Database
from ..portfolio import CapitalNotInitialised, Portfolio
from ..risk import RiskEngine
from .base import Agent


class EquityAgent(Agent):
    name = "equity"
    title = "EQUITY"

    def __init__(self, logger, db: Database, pf: Portfolio, risk: RiskEngine, on_breach):
        super().__init__(logger)
        self.db, self.pf, self.risk = db, pf, risk
        self.on_breach = on_breach  # async callable(mode, kind, detail)
        self._last_periodic: dict[str, float] = {}
        self.metrics = {}

    def update(self, mode: str, reason: str) -> dict | None:
        try:
            self.pf.ensure_initialised(mode)
        except CapitalNotInitialised as e:
            self.fail(e)
            return None
        self.busy(f"Updating equity ({reason})")
        self.pf.snapshot(mode, reason)
        s = self.pf.summary(mode)
        a = analytics.compute(self.db, mode, s["starting_usdt"])
        c = a["consolidated"]
        self.metrics = {
            "mode": mode, "equity": round(s["total_equity"], 4), "pnl": round(s["total_pnl"], 4),
            "win_rate": None if c["win_rate"] is None else round(c["win_rate"], 1),
            "profit_factor": None if c["profit_factor"] in (None, float("inf")) else round(c["profit_factor"], 2),
            "max_dd_pct": round(a["max_drawdown_pct"], 2), "trades": c["trades"],
        }
        if reason != "periodic":
            self.log("equity_updated", f"Equity updated after {reason}: {s['total_equity']:.2f} USDT "
                     f"(P&L {s['total_pnl']:+.2f})", data={"summary": {k: s[k] for k in (
                         "total_equity", "realized_pnl", "unrealized_pnl")}})
        self.done(f"Equity {s['total_equity']:.2f} USDT ({s['total_pnl']:+.2f})", activity="Tracking portfolio")
        return s

    async def periodic(self, mode: str, every: float = 60.0) -> None:
        if time.time() - self._last_periodic.get(mode, 0) >= every:
            self._last_periodic[mode] = time.time()
            self.update(mode, "periodic")
        if not self.pf.capital_info(mode):
            return
        b = self.risk.breach_status(mode)
        self.metrics["daily_pnl_pct"] = round(b["daily_pnl_pct"], 3)
        self.metrics["drawdown_pct"] = round(b["drawdown_pct"], 3)
        if b["daily_loss_breached"]:
            await self.on_breach(mode, "daily_loss_limit", b)
        elif b["drawdown_breached"]:
            await self.on_breach(mode, "max_drawdown", b)
