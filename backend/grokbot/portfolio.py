"""Portfolio accounting.

Balances are never stored as mutable numbers; they are derived from the
append-only ``ledger`` plus open ``positions`` marked to market. Every mode
(paper / testnet / live) has a completely separate ledger, so simulated and
real results are never mixed.

Accounts per mode: ``spot``, ``futures``, ``reserve``.

Ledger conventions (USDT):
  spot     entry  : -qty*price (kind=trade), -fee (kind=fee)
  spot     exit   : +qty*price (kind=trade), -fee (kind=fee)
  futures  entry  : -fee                       (margin is a reservation, not a cash flow)
  futures  exit   : +gross pnl (kind=pnl), -fee
  futures  funding: +/- amount (kind=funding)

Identity: total_equity = starting_capital + realized_pnl + unrealized_pnl,
where unrealized_pnl of an open position is mark-to-market gross P&L minus
fees already paid on it plus funding already received/paid on it.
Futures leverage never increases equity or available capital: it only
reduces the margin reserved for a given notional.
"""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

from .config_store import ConfigStore
from .db import Database
from .market.data import MarketData
from .market.fx import FxService
from .util import D, ZERO, now

ACCOUNTS = ("spot", "futures", "reserve")


class CapitalNotInitialised(Exception):
    pass


class Portfolio:
    def __init__(self, db: Database, md: MarketData, cfg: ConfigStore, fx: FxService):
        self.db, self.md, self.cfg, self.fx = db, md, cfg, fx

    # ------------------------------------------------------------ capital
    def capital_info(self, mode: str) -> dict | None:
        raw = self.db.meta_get(f"capital_init:{mode}")
        return json.loads(raw) if raw else None

    def ensure_initialised(self, mode: str) -> dict:
        info = self.capital_info(mode)
        if info:
            return info
        rate = self.fx.rate
        if not rate:
            raise CapitalNotInitialised("USD/INR rate unavailable; cannot convert configured ₹ capital to USDT")
        cap = self.cfg.capital
        usdt = D(cap.starting_inr) / D(rate)
        info = {"starting_inr": cap.starting_inr, "usd_inr": rate, "starting_usdt": float(usdt),
                "ts": now(), "note": "configured capital (not an exchange deposit)"}
        with self.db.tx():
            for acct, pct in (("spot", cap.spot_pct), ("futures", cap.futures_pct), ("reserve", cap.reserve_pct)):
                amt = usdt * D(pct) / 100
                self._post(mode, acct, "allocation", amt, None, f"initial allocation {pct:g}%")
            self.db.meta_set(f"capital_init:{mode}", json.dumps(info))
        return info

    def starting_usdt(self, mode: str) -> Decimal:
        info = self.capital_info(mode)
        return D(info["starting_usdt"]) if info else ZERO

    def reallocate(self, mode: str) -> list[dict]:
        """Move available cash between accounts to match the configured
        percentages of starting capital. Never touches margin or holdings."""
        start = self.starting_usdt(mode)
        if start <= 0:
            return []
        cap = self.cfg.capital
        targets = {"spot": start * D(cap.spot_pct) / 100, "futures": start * D(cap.futures_pct) / 100,
                   "reserve": start * D(cap.reserve_pct) / 100}
        current = {a: D(self.db.scalar(
            "SELECT COALESCE(SUM(CAST(amount AS REAL)),0) FROM ledger WHERE mode=? AND account=? AND kind='allocation'",
            (mode, a))) for a in ACCOUNTS}
        moves = {a: targets[a] - current[a] for a in ACCOUNTS}
        for a, delta in moves.items():
            if delta < 0 and self.available(mode, a) + delta < -D("0.01"):
                raise ValueError(f"cannot reduce {a} allocation by {-delta:.2f} USDT: only "
                                 f"{self.available(mode, a):.2f} USDT is free (rest is in positions/margin)")
        out = []
        with self.db.tx():
            for a, delta in moves.items():
                if abs(delta) > D("0.000001"):
                    self._post(mode, a, "allocation", delta, None, "reallocation")
                    out.append({"account": a, "amount": float(delta)})
        return out

    # ------------------------------------------------------------- ledger
    def _post(self, mode: str, account: str, kind: str, amount: Decimal, trade_id: str | None, note: str = "") -> None:
        assert account in ACCOUNTS
        self.db.execute(
            "INSERT INTO ledger(ts,mode,account,trade_id,kind,amount,note) VALUES(?,?,?,?,?,?,?)",
            (now(), mode, account, trade_id, kind, str(amount), note))

    def post(self, mode: str, account: str, kind: str, amount: Decimal, trade_id: str | None, note: str = "") -> None:
        self._post(mode, account, kind, D(amount), trade_id, note)

    def cash(self, mode: str, account: str) -> Decimal:
        rows = self.db.all("SELECT amount FROM ledger WHERE mode=? AND account=?", (mode, account))
        return sum((D(r["amount"]) for r in rows), ZERO)

    # ---------------------------------------------------------- positions
    def open_positions(self, mode: str, market: str | None = None) -> list[dict]:
        sql = "SELECT * FROM positions WHERE mode=? AND status='OPEN'"
        args: list = [mode]
        if market:
            sql += " AND market=?"
            args.append(market)
        return self.db.all(sql, args)

    def mark_price(self, pos: dict) -> Decimal | None:
        m = self.md.mark(pos["market"], pos["symbol"])
        return D(m) if m else None

    def position_unrealized(self, pos: dict, mark: Decimal | None = None) -> dict:
        mark = mark if mark is not None else self.mark_price(pos)
        qty, entry = D(pos["qty"]), D(pos["entry_price"])
        if mark is None:
            mark = entry
        d = 1 if pos["direction"] == "LONG" else -1
        gross = (mark - entry) * qty * d
        net = gross - D(pos["fees"]) + D(pos["funding"])
        notional = mark * qty
        return {"mark": mark, "gross": gross, "net": net, "notional": notional,
                "pnl_pct": float(gross / (entry * qty) * 100 * (pos["leverage"] if pos["market"] == "futures" else 1))
                if entry * qty else 0.0}

    def margin_used(self, mode: str) -> Decimal:
        return sum((D(p["margin"]) for p in self.open_positions(mode, "futures")), ZERO)

    def available(self, mode: str, account: str) -> Decimal:
        """Cash that can fund a NEW position in ``account``."""
        c = self.cash(mode, account)
        if account == "futures":
            c -= self.margin_used(mode)
            # Unrealized losses on isolated positions are bounded by their own margin,
            # but cross-margin losses would reduce availability.
            if self.cfg.risk.margin_mode == "CROSSED":
                c += min(ZERO, sum((self.position_unrealized(p)["gross"] for p in self.open_positions(mode, "futures")), ZERO))
        return c

    # ------------------------------------------------------------- equity
    def summary(self, mode: str) -> dict:
        info = self.capital_info(mode)
        start = self.starting_usdt(mode)
        spot_cash, fut_cash, reserve = (self.cash(mode, a) for a in ACCOUNTS)
        spot_pos, fut_pos = self.open_positions(mode, "spot"), self.open_positions(mode, "futures")
        spot_hold = ZERO
        spot_unreal = fut_unreal = ZERO
        reserved_in_positions = ZERO
        for p in spot_pos:
            u = self.position_unrealized(p)
            spot_hold += u["notional"]
            spot_unreal += u["net"]
            reserved_in_positions += D(p["qty"]) * D(p["entry_price"])
        for p in fut_pos:
            fut_unreal += self.position_unrealized(p)["net"]
        fut_gross_unreal = sum((self.position_unrealized(p)["gross"] for p in fut_pos), ZERO)
        margin = self.margin_used(mode)
        spot_eq = spot_cash + spot_hold
        fut_eq = fut_cash + fut_gross_unreal
        total = spot_eq + fut_eq + reserve
        closed = self.db.all("SELECT market, realized_pnl FROM positions WHERE mode=? AND status='CLOSED'", (mode,))
        realized = {"spot": ZERO, "futures": ZERO}
        for r in closed:
            realized[r["market"]] += D(r["realized_pnl"])
        alloc = {a: D(self.db.scalar(
            "SELECT COALESCE(SUM(CAST(amount AS REAL)),0) FROM ledger WHERE mode=? AND account=? AND kind='allocation'",
            (mode, a))) for a in ACCOUNTS}
        fees = D(self.db.scalar(
            "SELECT COALESCE(SUM(CAST(amount AS REAL)),0) FROM ledger WHERE mode=? AND kind='fee'", (mode,)))
        funding = D(self.db.scalar(
            "SELECT COALESCE(SUM(CAST(amount AS REAL)),0) FROM ledger WHERE mode=? AND kind='funding'", (mode,)))
        rate = self.fx.rate
        f = float
        return {
            "mode": mode, "initialised": info is not None, "capital": info,
            "starting_usdt": f(start), "total_equity": f(total), "total_pnl": f(total - start) if info else 0.0,
            "realized_pnl": f(realized["spot"] + realized["futures"]),
            "unrealized_pnl": f(spot_unreal + fut_unreal),
            "fees_paid": f(-fees), "funding_net": f(funding),
            "spot": {"allocated": f(alloc["spot"]), "cash": f(spot_cash), "holdings": f(spot_hold),
                     "equity": f(spot_eq), "available": f(spot_cash), "reserved": f(reserved_in_positions),
                     "realized": f(realized["spot"]), "unrealized": f(spot_unreal), "positions": len(spot_pos)},
            "futures": {"allocated": f(alloc["futures"]), "wallet": f(fut_cash), "margin": f(margin),
                        "equity": f(fut_eq), "available": f(self.available(mode, "futures")),
                        "realized": f(realized["futures"]), "unrealized": f(fut_unreal), "positions": len(fut_pos)},
            "reserve": {"allocated": f(alloc["reserve"]), "balance": f(reserve)},
            "allocated_total": f(alloc["spot"] + alloc["futures"]),
            "available_total": f(spot_cash + self.available(mode, "futures")),
            "reserved_total": f(reserved_in_positions + margin),
            "fx": {"usd_inr": rate, **{k: v for k, v in self.fx.state.items() if k != "rate"}},
            "inr": {"total_equity": f(total) * rate if rate else None,
                    "total_pnl": f(total - start) * rate if rate and info else None},
        }

    # ----------------------------------------------------- daily / drawdown
    def day_key(self) -> str:
        return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    def risk_state(self, mode: str, total_equity: float | None = None) -> dict:
        """Daily P&L vs UTC day start and drawdown vs peak equity."""
        if total_equity is None:
            total_equity = self.summary(mode)["total_equity"]
        key = f"day_start:{mode}:{self.day_key()}"
        ds = self.db.meta_get(key)
        if ds is None:
            self.db.meta_set(key, str(total_equity))
            ds = str(total_equity)
        day_start = float(ds)
        peak_key = f"peak_equity:{mode}"
        peak = float(self.db.meta_get(peak_key) or 0)
        if total_equity > peak:
            peak = total_equity
            self.db.meta_set(peak_key, str(peak))
        daily_pnl = total_equity - day_start
        return {
            "day_start_equity": day_start, "daily_pnl": daily_pnl,
            "daily_pnl_pct": daily_pnl / day_start * 100 if day_start else 0.0,
            "peak_equity": peak, "drawdown_pct": (peak - total_equity) / peak * 100 if peak else 0.0,
        }

    def snapshot(self, mode: str, reason: str) -> None:
        if not self.capital_info(mode):
            return
        s = self.summary(mode)
        self.db.execute(
            "INSERT INTO equity_snapshots(ts,mode,reason,spot,futures,reserve,total,realized,unrealized) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (now(), mode, reason, s["spot"]["equity"], s["futures"]["equity"], s["reserve"]["balance"],
             s["total_equity"], s["realized_pnl"], s["unrealized_pnl"]))
