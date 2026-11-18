"""AGENT 4 - EXECUTOR.

Executes risk-approved trades and manages open positions.

Safety properties:
* Refuses any trade without a valid, unexpired approval token from the
  central risk engine whose fields match the stored sizing.
* Stage transition RISK_APPROVED -> EXECUTING is a compare-and-set, and the
  entry order's client id is deterministic and a PRIMARY KEY, so a trade can
  be submitted at most once - across concurrent workers and restarts.
* Ambiguous submissions are reconciled by client id; never re-sent.
* Exchange positions receive exchange-side stop-loss/take-profit. If those
  cannot be placed, the position is closed immediately.
"""
from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal

from ..config_store import ConfigStore
from ..db import Database, dumps, loads
from ..market.data import MarketData
from ..portfolio import Portfolio
from ..risk import RiskEngine
from ..state import SystemState
from ..util import D, ZERO, client_order_id, now
from .base import Agent
from .sizer import liquidation_price

log = logging.getLogger("grokbot.executor")


class ExecutorAgent(Agent):
    name = "executor"
    title = "EXECUTOR"

    def __init__(self, logger, db: Database, md: MarketData, cfg: ConfigStore, pf: Portfolio,
                 risk: RiskEngine, state: SystemState, brokers, on_change):
        super().__init__(logger)
        self.db, self.md, self.cfg, self.pf, self.risk, self.state = db, md, cfg, pf, risk, state
        self.brokers = brokers            # callable(mode) -> broker
        self.on_change = on_change        # callable(mode, reason) after fills/closes
        self.metrics = {"orders": 0, "filled": 0, "rejected": 0, "partial": 0, "closed": 0, "failed": 0}
        self._funding_seen: dict[str, tuple[int, float]] = {}
        self._last_exchange_check: dict[str, float] = {}
        self._closing: set[str] = set()

    # ----------------------------------------------------------- helpers
    def _cas(self, trade_id: str, frm: tuple[str, ...], to: str, **fields) -> bool:
        sets = ", ".join(f"{k}=?" for k in fields)
        sql = f"UPDATE trades SET stage=?, status=?, updated_at=?{', ' + sets if sets else ''} " \
              f"WHERE id=? AND stage IN ({','.join('?' * len(frm))})"
        status = {"OPEN": "open", "CLOSED": "closed", "EXECUTING": "executing"}.get(to, to.lower())
        cur = self.db.execute(sql, (to, status, now(), *fields.values(), trade_id, *frm))
        return cur.rowcount == 1

    def _order_row(self, cid, trade, side, qty, purpose, otype="MARKET"):
        t = now()
        self.db.execute(
            "INSERT INTO orders(client_id,trade_id,mode,market,symbol,side,type,purpose,qty,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, trade["id"] if "id" in trade else trade["trade_id"], trade["mode"], trade["market"], trade["symbol"],
             side, otype, purpose, str(qty), "SUBMITTING", t, t))

    def _update_order(self, cid, res, status=None):
        self.db.execute(
            "UPDATE orders SET status=?, exchange_id=?, filled_qty=?, avg_price=?, fee_usdt=?, updated_at=?, error=?, raw=? "
            "WHERE client_id=?",
            (status or res.status, res.exchange_id, str(res.filled_qty), str(res.avg_price), str(res.fee_usdt), now(),
             res.error, dumps(res.raw)[:4000], cid))

    # ------------------------------------------------------------ entry
    async def execute(self, trade: dict) -> dict:
        mode = trade["mode"]
        sizing = trade.get("sizing")
        sizing = sizing if isinstance(sizing, dict) else loads(sizing)
        if trade.get("stage") != "RISK_APPROVED" or not sizing or not sizing.get("approved"):
            self.log("execution_refused", f"Executor refused {trade['symbol']}: trade is not risk-approved "
                     f"(stage {trade.get('stage')})", trade["id"], level="error")
            return {"ok": False, "reason": "trade is not risk-approved"}
        ok, why = self.risk.verify_token(trade.get("approval_token"), trade["id"], mode, sizing["qty"], sizing["stop"])
        if not ok:
            self.metrics["rejected"] += 1
            self._cas(trade["id"], ("RISK_APPROVED",), "FAILED", reject_reason=f"executor: {why}")
            self.log("execution_refused", f"Executor refused {trade['symbol']}: {why}", trade["id"], level="error")
            return {"ok": False, "reason": why}
        if mode == "live" and not self.state.live_authorized():
            self._cas(trade["id"], ("RISK_APPROVED",), "FAILED", reject_reason="live trading not authorized")
            return {"ok": False, "reason": "live trading not authorized"}
        if self.state.halted:
            self._cas(trade["id"], ("RISK_APPROVED",), "CANCELLED", reject_reason="trading halted")
            return {"ok": False, "reason": "trading halted"}
        if not self._cas(trade["id"], ("RISK_APPROVED",), "EXECUTING"):
            return {"ok": False, "reason": "trade already being executed or no longer approved"}
        market, symbol, direction = trade["market"], trade["symbol"], trade["direction"]
        side = "BUY" if direction == "LONG" else "SELL"
        qty = D(sizing["qty"])
        broker = self.brokers(mode)
        self.busy(f"Executing {side} {qty} {symbol} ({market}, {mode})")
        cid = client_order_id(trade["id"], "E")
        try:
            if market == "futures":
                await broker.prepare_futures(symbol, int(sizing["leverage"]), self.cfg.risk.margin_mode)
            self._order_row(cid, trade, side, qty, "entry")
        except Exception as e:  # noqa: BLE001
            self._cas(trade["id"], ("EXECUTING",), "FAILED", reject_reason=f"pre-trade setup failed: {e}")
            self.metrics["failed"] += 1
            self.log("execution_failed", f"Executor setup failed for {symbol}: {e}", trade["id"], level="error")
            self.fail(e)
            return {"ok": False, "reason": str(e)}
        self.metrics["orders"] += 1
        self.log("order_submitted", f"Executor submitted {side} MARKET {qty} {symbol} ({market}, {mode})",
                 trade["id"], {"client_id": cid, "qty": str(qty)})
        try:
            res = await broker.market_order(market, symbol, side, qty, cid)
        except Exception as e:  # noqa: BLE001
            from ..exchange.broker import ExecResult
            res = ExecResult("UNKNOWN", error=str(e))
        return await self._apply_entry(trade, sizing, cid, res, broker)

    async def _apply_entry(self, trade: dict, sizing: dict, cid: str, res, broker) -> dict:
        mode, market, symbol, direction = trade["mode"], trade["market"], trade["symbol"], trade["direction"]
        side = "BUY" if direction == "LONG" else "SELL"
        if res.status == "UNKNOWN":
            self._update_order(cid, res)
            inc = self.state.incident("order_state_unknown", "high", {"trade_id": trade["id"], "client_id": cid, "error": res.error})
            self.log("order_unknown", f"Order {cid} state UNKNOWN - will reconcile, never resubmit (incident #{inc})",
                     trade["id"], level="error")
            self.fail(f"order {cid} state unknown")
            return {"ok": False, "reason": "order state unknown"}
        if not res.ok:
            self._update_order(cid, res, "REJECTED" if res.status != "NOT_FOUND" else "NOT_FOUND")
            self._cas(trade["id"], ("EXECUTING",), "FAILED", reject_reason=f"order rejected: {res.error}")
            self.metrics["rejected"] += 1
            self.log("execution_failed", f"Order rejected for {symbol}: {res.error}", trade["id"], level="error")
            self.done(f"REJECTED {symbol}: {res.error}")
            return {"ok": False, "reason": res.error}
        qty = res.net_base_qty if res.net_base_qty is not None else res.filled_qty
        entry = res.avg_price
        lev = int(sizing["leverage"]) if market == "futures" else 1
        margin = qty * entry / D(lev) if market == "futures" else qty * entry
        liq = liquidation_price(entry, lev, direction) if market == "futures" else None
        slip = res.slippage_usdt(side)
        if res.status == "PARTIALLY_FILLED":
            self.metrics["partial"] += 1
        with self.db.tx():
            self._update_order(cid, res)
            self.db.execute(
                "INSERT INTO fills(client_id,trade_id,mode,ts,qty,price,fee_usdt,ref_price,slippage_usdt,exchange_trade_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (cid, trade["id"], mode, now(), str(res.filled_qty), str(entry), str(res.fee_usdt), str(res.ref_price),
                 str(slip), res.exchange_id))
            self.db.execute(
                "INSERT INTO positions(trade_id,mode,market,symbol,direction,strategy,status,qty,entry_price,stop_price,"
                "target_price,leverage,margin,liq_price,opened_at,fees,slippage) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (trade["id"], mode, market, symbol, direction, trade["strategy"], "OPEN", str(qty), str(entry),
                 sizing["stop"], sizing["target"], lev, str(margin if market == "futures" else ZERO),
                 str(liq) if liq else None, now(), str(res.fee_usdt), str(slip)))
            if market == "spot":
                self.pf.post(mode, "spot", "trade", -(res.filled_qty * entry), trade["id"], f"buy {symbol}")
            self.pf.post(mode, market, "fee", -res.fee_usdt, trade["id"], "entry fee")
            self._cas(trade["id"], ("EXECUTING",), "OPEN", execution=dumps({
                "client_id": cid, "status": res.status, "filled_qty": str(res.filled_qty), "net_qty": str(qty),
                "avg_price": str(entry), "fee_usdt": str(res.fee_usdt), "slippage_usdt": str(slip),
                "ref_price": str(res.ref_price), "exchange_id": res.exchange_id, "broker": broker.kind}))
        self.metrics["filled"] += 1
        self.log("order_filled",
                 f"Executor filled {side} {res.filled_qty} {symbol} @ {entry:.6g} "
                 f"(fee {res.fee_usdt:.4f} USDT, slippage {slip:.4f} USDT{', PARTIAL' if res.status == 'PARTIALLY_FILLED' else ''})",
                 trade["id"], {"avg_price": str(entry), "qty": str(res.filled_qty), "fee": str(res.fee_usdt),
                               "slippage": str(slip), "status": res.status})
        pos = self.db.one("SELECT * FROM positions WHERE trade_id=?", (trade["id"],))
        # Guard: if fill slipped through the stop, exit immediately.
        stop = D(pos["stop_price"])
        if (direction == "LONG" and entry <= stop) or (direction == "SHORT" and entry >= stop):
            await self.close_position(pos, "entry_beyond_stop")
            return {"ok": True, "closed_immediately": True}
        try:
            prot = await broker.place_protection(pos)
            self.log("protection_placed", f"Stop-loss {pos['stop_price']} / take-profit {pos['target_price']} active "
                     f"({prot.get('type')})", trade["id"], prot)
        except Exception as e:  # noqa: BLE001
            inc = self.state.incident("protection_failed", "critical", {"trade_id": trade["id"], "error": str(e)})
            self.log("protection_failed", f"Could not place protective orders ({e}); closing position (incident #{inc})",
                     trade["id"], level="error")
            await self.close_position(pos, "protection_failed")
        self.done(f"FILLED {symbol} {direction} @ {entry:.6g}")
        self.on_change(mode, f"entry {symbol}")
        return {"ok": True}

    # ------------------------------------------------------------- exit
    async def close_position(self, pos: dict, reason: str, actor: str = "executor") -> dict:
        tid = pos["trade_id"]
        if tid in self._closing:
            return {"ok": False, "reason": "already closing"}
        cur = self.db.execute("UPDATE positions SET status='CLOSING' WHERE trade_id=? AND status='OPEN'", (tid,))
        if cur.rowcount != 1:
            return {"ok": False, "reason": "position not open"}
        self._closing.add(tid)
        try:
            broker = self.brokers(pos["mode"])
            market, symbol = pos["market"], pos["symbol"]
            side = "SELL" if pos["direction"] == "LONG" else "BUY"
            qty = D(pos["qty"])
            self.busy(f"Closing {symbol} ({reason})")
            await broker.cancel_protection(pos)
            n = int(self.db.scalar("SELECT COUNT(*) FROM orders WHERE trade_id=? AND purpose='exit'", (tid,)) or 0)
            cid = client_order_id(tid, "X", n)
            self._order_row(cid, pos, side, qty, "exit")
            res = await broker.market_order(market, symbol, side, qty, cid, reduce_only=True)
            if not res.ok:
                self._update_order(cid, res)
                self.db.execute("UPDATE positions SET status='OPEN' WHERE trade_id=?", (tid,))
                inc = self.state.incident("exit_failed", "critical", {"trade_id": tid, "reason": reason, "error": res.error})
                self.log("exit_failed", f"Exit order for {symbol} failed: {res.error} (incident #{inc})", tid, level="error")
                if broker.kind != "paper":
                    try:
                        await broker.place_protection(pos)
                    except Exception as e:  # noqa: BLE001
                        self.log("protection_failed", f"Re-protecting {symbol} failed: {e}", tid, level="error")
                self.fail(f"exit failed {symbol}")
                return {"ok": False, "reason": res.error}
            self._update_order(cid, res)
            self._apply_exit(pos, res.filled_qty, res.avg_price, res.fee_usdt, reason, res.slippage_usdt(side), cid, res)
            return {"ok": True}
        finally:
            self._closing.discard(tid)

    def _apply_exit(self, pos: dict, qty: Decimal, price: Decimal, fee: Decimal, reason: str,
                    slip: Decimal = ZERO, cid: str | None = None, res=None) -> None:
        tid, mode, market = pos["trade_id"], pos["mode"], pos["market"]
        d = 1 if pos["direction"] == "LONG" else -1
        entry, held = D(pos["entry_price"]), D(pos["qty"])
        qty = min(qty, held)
        gross = (price - entry) * qty * d
        remaining = held - qty
        rules = self.md.rules[market].get(pos["symbol"])
        dust = rules is None or remaining < max(rules.min_qty, rules.step_size)
        with self.db.tx():
            if cid:
                self.db.execute(
                    "INSERT INTO fills(client_id,trade_id,mode,ts,qty,price,fee_usdt,ref_price,slippage_usdt,exchange_trade_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (cid, tid, mode, now(), str(qty), str(price), str(fee),
                     str(res.ref_price) if res else None, str(slip), res.exchange_id if res else None))
            if market == "spot":
                self.pf.post(mode, "spot", "trade", qty * price, tid, f"sell {pos['symbol']} ({reason})")
            else:
                self.pf.post(mode, "futures", "pnl", gross, tid, f"close {pos['symbol']} ({reason})")
            self.pf.post(mode, market, "fee", -fee, tid, "exit fee")
            g = D(pos["gross_pnl"]) + gross
            fees = D(pos["fees"]) + fee
            funding = D(pos["funding"])
            sl = D(pos["slippage"]) + slip
            margin = D(pos["margin"]) * (remaining / held) if held > 0 else ZERO
            if dust:
                realized = g - fees + funding
                self.db.execute(
                    "UPDATE positions SET status='CLOSED', qty=?, closed_at=?, exit_price=?, exit_reason=?, gross_pnl=?, "
                    "fees=?, slippage=?, realized_pnl=?, margin='0' WHERE trade_id=?",
                    (str(held), now(), str(price), reason, str(g), str(fees), str(sl), str(realized), tid))
                self.db.execute("UPDATE trades SET stage='CLOSED', status='closed', updated_at=? WHERE id=?", (now(), tid))
            else:
                self.db.execute(
                    "UPDATE positions SET status='OPEN', qty=?, gross_pnl=?, fees=?, slippage=?, margin=? WHERE trade_id=?",
                    (str(remaining), str(g), str(fees), str(sl), str(margin), tid))
        if dust:
            self.metrics["closed"] += 1
            realized = g - fees + funding
            self.log("position_closed",
                     f"Closed {pos['symbol']} {pos['direction']} @ {price:.6g} ({reason.replace('_', ' ')}): "
                     f"net P&L {realized:+.4f} USDT (fees {fees:.4f}, funding {funding:+.4f})",
                     tid, {"exit_price": str(price), "gross": str(g), "fees": str(fees), "funding": str(funding),
                           "realized": str(realized), "reason": reason},
                     "info" if realized >= 0 else "warning")
            self.done(f"CLOSED {pos['symbol']} {realized:+.2f} USDT ({reason})")
        else:
            self.log("partial_exit", f"Partial exit {qty} {pos['symbol']} @ {price:.6g}; {remaining} remaining", tid)
        self.on_change(mode, f"exit {pos['symbol']}")

    # ---------------------------------------------------------- monitor
    async def monitor(self) -> None:
        """Called every ~2 s. Checks stops/targets/liquidation for paper
        positions and reconciles exchange positions."""
        positions = self.db.all("SELECT * FROM positions WHERE status='OPEN'")
        if not positions:
            if self.status != "error":
                self.activity = "No open positions"
            return
        self.activity = f"Monitoring {len(positions)} open position{'s' * (len(positions) != 1)}"
        stale_limit = self.cfg.risk.stale_data_seconds
        for pos in positions:
            try:
                if pos["mode"] == "paper":
                    await self._monitor_paper(pos, stale_limit)
                else:
                    await self._monitor_exchange(pos)
            except Exception as e:  # noqa: BLE001
                log.exception("monitor %s", pos["trade_id"])
                self.fail(e)

    async def _monitor_paper(self, pos: dict, stale_limit: float) -> None:
        market, symbol = pos["market"], pos["symbol"]
        t = self.md.ticker(market, symbol)
        if t is None or self.md.age(market, symbol) > stale_limit:
            return  # never act on stale data; protective logic resumes when data is fresh
        if market == "futures":
            self._paper_funding(pos, t)
        px = D(t.mark) if market == "futures" and t.mark else D(t.price)
        stop, target = D(pos["stop_price"]), D(pos["target_price"])
        long = pos["direction"] == "LONG"
        if market == "futures" and pos["liq_price"]:
            liq = D(pos["liq_price"])
            if (long and px <= liq) or (not long and px >= liq):
                await self._paper_liquidate(pos, liq)
                return
        if (long and px <= stop) or (not long and px >= stop):
            await self.close_position(pos, "stop_loss")
        elif (long and px >= target) or (not long and px <= target):
            await self.close_position(pos, "take_profit")

    async def _paper_liquidate(self, pos: dict, liq: Decimal) -> None:
        cur = self.db.execute("UPDATE positions SET status='CLOSING' WHERE trade_id=? AND status='OPEN'", (pos["trade_id"],))
        if cur.rowcount == 1:
            # Isolated liquidation: position margin is lost (approximated at liquidation price).
            self._apply_exit(pos, D(pos["qty"]), liq, ZERO, "liquidation")
            self.state.incident("liquidation", "critical", {"trade_id": pos["trade_id"], "symbol": pos["symbol"]})

    def _paper_funding(self, pos: dict, t) -> None:
        sym = pos["symbol"]
        prev = self._funding_seen.get(sym)
        self._funding_seen[sym] = (t.next_funding, t.funding_rate)
        if not prev or not t.next_funding or t.next_funding <= prev[0]:
            return
        settled_ms, rate = prev
        settled = settled_ms / 1000
        if pos["opened_at"] >= settled or (pos["last_funding_ts"] and pos["last_funding_ts"] >= settled):
            return
        d = 1 if pos["direction"] == "LONG" else -1
        notional = D(pos["qty"]) * D(t.mark or t.price)
        amount = -D(d) * notional * D(rate)  # longs pay positive funding
        with self.db.tx():
            self.pf.post(pos["mode"], "futures", "funding", amount, pos["trade_id"], f"funding {rate:.6f}")
            self.db.execute("UPDATE positions SET funding=?, last_funding_ts=? WHERE trade_id=?",
                            (str(D(pos["funding"]) + amount), settled, pos["trade_id"]))
        self.log("funding", f"Funding {sym}: {amount:+.4f} USDT (rate {rate * 100:.4f}%)", pos["trade_id"],
                 {"rate": rate, "amount": str(amount)})

    async def _monitor_exchange(self, pos: dict) -> None:
        tid = pos["trade_id"]
        if time.time() - self._last_exchange_check.get(tid, 0) < 10:
            return
        self._last_exchange_check[tid] = time.time()
        broker = self.brokers(pos["mode"])
        if broker is None:
            return
        if pos["market"] == "futures" and time.time() - (pos["last_funding_ts"] or 0) > 600:
            total = await broker.funding_paid(pos)
            delta = total - D(pos["funding"])
            if delta != 0:
                with self.db.tx():
                    self.pf.post(pos["mode"], "futures", "funding", delta, tid, "funding (exchange income)")
                    self.db.execute("UPDATE positions SET funding=?, last_funding_ts=? WHERE trade_id=?",
                                    (str(total), time.time(), tid))
                pos = self.db.one("SELECT * FROM positions WHERE trade_id=?", (tid,))
            else:
                self.db.execute("UPDATE positions SET last_funding_ts=? WHERE trade_id=?", (time.time(), tid))
        ex = await broker.check_exit(pos)
        if ex.closed:
            cur = self.db.execute("UPDATE positions SET status='CLOSING' WHERE trade_id=? AND status='OPEN'", (tid,))
            if cur.rowcount == 1:
                if ex.qty <= 0:
                    ex.qty, ex.exit_price = D(pos["qty"]), self.pf.mark_price(pos) or D(pos["entry_price"])
                self._apply_exit(pos, ex.qty, ex.exit_price, ex.fee_usdt, ex.reason)
                self.log("reconciled_exit", f"Exchange reports {pos['symbol']} closed ({ex.reason})", tid)

    # --------------------------------------------------------- recovery
    async def recover(self) -> list[str]:
        """Startup reconciliation. Returns human-readable notes."""
        notes = []
        for t in self.db.all("SELECT * FROM trades WHERE stage='EXECUTING'"):
            cid = client_order_id(t["id"], "E")
            order = self.db.one("SELECT * FROM orders WHERE client_id=?", (cid,))
            if order is None:
                self._cas(t["id"], ("EXECUTING",), "ABANDONED", reject_reason="interrupted before order submission")
                notes.append(f"{t['id']}: abandoned (never submitted)")
                continue
            if t["mode"] == "paper":
                # Paper fills are committed atomically with the position, so an
                # EXECUTING paper trade was never filled.
                self.db.execute("UPDATE orders SET status='ABANDONED' WHERE client_id=?", (cid,))
                self._cas(t["id"], ("EXECUTING",), "ABANDONED", reject_reason="interrupted before simulated fill")
                notes.append(f"{t['id']}: paper order abandoned")
                continue
            broker = self.brokers(t["mode"])
            res = await broker.query_order(t["market"], t["symbol"], cid)
            if res.status == "NOT_FOUND":
                self.db.execute("UPDATE orders SET status='NOT_FOUND' WHERE client_id=?", (cid,))
                self._cas(t["id"], ("EXECUTING",), "ABANDONED", reject_reason="order not found on exchange after restart")
                notes.append(f"{t['id']}: not on exchange, abandoned (not resubmitted)")
            elif res.ok:
                t["sizing"] = loads(t["sizing"])
                await self._apply_entry(t, t["sizing"], cid, res, broker)
                notes.append(f"{t['id']}: filled order recovered into position")
            else:
                notes.append(f"{t['id']}: still {res.status}; manual check required")
                self.state.incident("reconcile_pending", "high", {"trade_id": t["id"], "status": res.status})
        n = self.db.execute("UPDATE positions SET status='OPEN' WHERE status='CLOSING'").rowcount
        if n:
            notes.append(f"{n} position(s) were mid-close at shutdown; restored to OPEN for re-evaluation")
        for note in notes:
            self.log("recovery", note, level="warning")
        return notes
