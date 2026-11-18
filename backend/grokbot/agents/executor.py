"""AGENT 4 - EXECUTOR.

Executes risk-approved trades and manages open positions.

Entry types (config ``execution.entry_order_type``):
* market - one taker order; the position is opened from its fill.
* limit  - a (post-only by default) limit order rests on the book. The trade
  is in stage WORKING while the order is live. Fills are applied
  INCREMENTALLY: the first fill creates the position and every further fill
  grows it, so stop-loss / take-profit protection always covers exactly the
  quantity actually held. The order expires after ``limit_expiry_seconds``;
  it may be repriced up to ``max_reprices`` times, never further than
  ``max_chase_bps`` from the signal entry. Every reprice is
  cancel -> confirm final fills -> place remaining quantity with a new
  deterministic client id, so an order can never be over-filled or duplicated.

Safety properties:
* Refuses any trade without a valid, unexpired approval token from the
  central risk engine whose fields match the stored sizing.
* RISK_APPROVED -> EXECUTING is a compare-and-set; client order ids are
  deterministic PRIMARY KEYs, so a trade is submitted at most once.
* Ambiguous submissions are reconciled by client id; never re-sent.
* Exchange positions receive exchange-side stop-loss/take-profit. If those
  cannot be placed, the position is closed immediately.
* Before a position is closed, any still-working entry order is cancelled
  first, so nothing can be added to a position that is being exited.
"""
from __future__ import annotations

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
LIVE_ORDER = ("SUBMITTING", "NEW", "PARTIALLY_FILLED", "UNKNOWN")


class ExecutorAgent(Agent):
    name = "executor"
    title = "EXECUTOR"

    def __init__(self, logger, db: Database, md: MarketData, cfg: ConfigStore, pf: Portfolio,
                 risk: RiskEngine, state: SystemState, brokers, on_change):
        super().__init__(logger)
        self.db, self.md, self.cfg, self.pf, self.risk, self.state = db, md, cfg, pf, risk, state
        self.brokers = brokers            # callable(mode) -> broker
        self.on_change = on_change        # callable(mode, reason) after fills/closes
        self.metrics = {"orders": 0, "filled": 0, "rejected": 0, "partial": 0, "closed": 0, "failed": 0,
                        "limit_orders": 0, "repriced": 0, "expired": 0, "cancelled": 0}
        self._funding_seen: dict[str, tuple[int, float]] = {}
        self._last_exchange_check: dict[str, float] = {}
        self._closing: set[str] = set()

    # ----------------------------------------------------------- helpers
    def _cas(self, trade_id: str, frm: tuple[str, ...], to: str, **fields) -> bool:
        sets = ", ".join(f"{k}=?" for k in fields)
        sql = f"UPDATE trades SET stage=?, status=?, updated_at=?{', ' + sets if sets else ''} " \
              f"WHERE id=? AND stage IN ({','.join('?' * len(frm))})"
        status = {"OPEN": "open", "CLOSED": "closed", "EXECUTING": "executing", "WORKING": "working"}.get(to, to.lower())
        cur = self.db.execute(sql, (to, status, now(), *fields.values(), trade_id, *frm))
        return cur.rowcount == 1

    def _order_row(self, cid, trade, side, qty, purpose, otype="MARKET", price=None):
        t = now()
        self.db.execute(
            "INSERT INTO orders(client_id,trade_id,mode,market,symbol,side,type,purpose,qty,price,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, trade["id"] if "id" in trade else trade["trade_id"], trade["mode"], trade["market"], trade["symbol"],
             side, otype, purpose, str(qty), str(price) if price is not None else None, "SUBMITTING", t, t))

    def _update_order(self, cid, res, status=None):
        self.db.execute(
            "UPDATE orders SET status=?, exchange_id=COALESCE(?, exchange_id), filled_qty=?, avg_price=?, fee_usdt=?, "
            "updated_at=?, error=?, raw=? WHERE client_id=?",
            (status or res.status, res.exchange_id, str(res.filled_qty), str(res.avg_price), str(res.fee_usdt), now(),
             res.error, dumps(res.raw)[:4000], cid))

    def _recorded(self, cid: str) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        """Fills already applied for an order: (qty, quote cost, fee, net base qty)."""
        rows = self.db.all("SELECT qty, price, fee_usdt, net_qty FROM fills WHERE client_id=?", (cid,))
        q = sum((D(r["qty"]) for r in rows), ZERO)
        c = sum((D(r["qty"]) * D(r["price"]) for r in rows), ZERO)
        f = sum((D(r["fee_usdt"]) for r in rows), ZERO)
        n = sum((D(r["net_qty"] or r["qty"]) for r in rows), ZERO)
        return q, c, f, n

    def _sizing(self, trade: dict) -> dict:
        s = trade.get("sizing")
        return s if isinstance(s, dict) else (loads(s) or {})

    # ------------------------------------------------------------ entry
    async def execute(self, trade: dict) -> dict:
        mode = trade["mode"]
        sizing = self._sizing(trade)
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
        ex = self.cfg.execution
        limit = ex.entry_order_type == "limit"
        cid = client_order_id(trade["id"], "E")
        price = None
        try:
            if market == "futures":
                await broker.prepare_futures(symbol, int(sizing["leverage"]), self.cfg.risk.margin_mode)
            if limit:
                price = self._limit_price(market, symbol, side, D(sizing["entry"]))
                if price is None:
                    raise RuntimeError("no fresh book to price the limit order")
            self._order_row(cid, trade, side, qty, "entry", "LIMIT" if limit else "MARKET", price)
        except Exception as e:  # noqa: BLE001
            self._cas(trade["id"], ("EXECUTING",), "FAILED", reject_reason=f"pre-trade setup failed: {e}")
            self.metrics["failed"] += 1
            self.log("execution_failed", f"Executor setup failed for {symbol}: {e}", trade["id"], level="error")
            self.fail(e)
            return {"ok": False, "reason": str(e)}
        self.metrics["orders"] += 1
        from ..exchange.broker import ExecResult
        if not limit:
            self.busy(f"Executing {side} {qty} {symbol} ({market}, {mode})")
            self.log("order_submitted", f"Executor submitted {side} MARKET {qty} {symbol} ({market}, {mode})",
                     trade["id"], {"client_id": cid, "qty": str(qty)})
            try:
                res = await broker.market_order(market, symbol, side, qty, cid)
            except Exception as e:  # noqa: BLE001
                res = ExecResult("UNKNOWN", error=str(e))
            return await self._handle_entry_result(trade, sizing, cid, res, broker, final=True)
        self.metrics["limit_orders"] += 1
        self.busy(f"Placing limit {side} {qty} {symbol} @ {price}")
        try:
            res = await broker.limit_order(market, symbol, side, qty, price, cid, post_only=ex.post_only)
        except Exception as e:  # noqa: BLE001
            res = ExecResult("UNKNOWN", error=str(e))
        if res.status in ("NEW", "PARTIALLY_FILLED", "FILLED"):
            self.log("order_submitted", f"Executor placed {'post-only ' if ex.post_only else ''}LIMIT {side} {qty} "
                     f"{symbol} @ {price} ({market}, {mode}); expires in {ex.limit_expiry_seconds}s",
                     trade["id"], {"client_id": cid, "qty": str(qty), "price": str(price), "status": res.status})
            self._cas(trade["id"], ("EXECUTING",), "WORKING", execution=dumps({
                "order_type": "limit", "working_cid": cid, "price": str(price), "placed_at": now(),
                "first_placed_at": now(), "reprices": 0, "target_qty": str(qty), "broker": broker.kind}))
            self._update_order(cid, res)
            return await self._apply_order_progress(self._trade(trade["id"]), cid, res, broker)
        if res.status == "EXPIRED":  # post-only would have crossed the spread
            self._update_order(cid, res)
            self._cas(trade["id"], ("EXECUTING",), "WORKING", execution=dumps({
                "order_type": "limit", "working_cid": cid, "price": str(price), "placed_at": 0,
                "first_placed_at": now(), "reprices": 0, "target_qty": str(qty), "broker": broker.kind}))
            self.log("limit_crossed", f"Post-only limit for {symbol} would cross the spread; will reprice",
                     trade["id"], {"error": res.error})
            return {"ok": True, "working": True}
        return await self._handle_entry_result(trade, sizing, cid, res, broker, final=True)

    def _limit_price(self, market: str, symbol: str, side: str, signal_entry: Decimal) -> Decimal | None:
        """Passive price: join best bid (buy) / best ask (sell), offset further
        by ``limit_offset_bps``; never worse than the chase cap around the
        signal entry."""
        t = self.md.ticker(market, symbol)
        rules = self.md.rules[market].get(symbol)
        if t is None or not t.bid or not t.ask or rules is None:
            return None
        ex = self.cfg.execution
        off = D(ex.limit_offset_bps) / D(10_000)
        cap = D(ex.max_chase_bps) / D(10_000)
        if side == "BUY":
            p = min(D(t.bid) * (1 - off), signal_entry * (1 + cap))
            return rules.floor_price(p)
        p = max(D(t.ask) * (1 + off), signal_entry * (1 - cap))
        return rules.floor_price(p) + (rules.tick_size if rules.floor_price(p) < p else ZERO)

    def _trade(self, tid: str) -> dict:
        t = self.db.one("SELECT * FROM trades WHERE id=?", (tid,))
        t["sizing"] = loads(t["sizing"])
        t["execution_d"] = loads(t["execution"]) or {}
        return t

    async def _handle_entry_result(self, trade, sizing, cid, res, broker, final: bool) -> dict:
        """Market-order (or immediate) entry result handling."""
        symbol = trade["symbol"]
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
        self._update_order(cid, res)
        if res.status == "PARTIALLY_FILLED":
            self.metrics["partial"] += 1
        await self._apply_fill_increment(trade, sizing, cid, res, broker)
        self._cas(trade["id"], ("EXECUTING", "WORKING"), "OPEN", execution=dumps({
            "order_type": "market", "client_id": cid, "status": res.status, "filled_qty": str(res.filled_qty),
            "avg_price": str(res.avg_price), "fee_usdt": str(res.fee_usdt), "ref_price": str(res.ref_price),
            "exchange_id": res.exchange_id, "broker": broker.kind}))
        pos = self.db.one("SELECT * FROM positions WHERE trade_id=?", (trade["id"],))
        if pos and pos["status"] == "OPEN":
            await self._post_fill_checks(pos, broker)
        self.done(f"FILLED {symbol} {trade['direction']} @ {res.avg_price:.6g}")
        self.on_change(trade["mode"], f"entry {symbol}")
        return {"ok": True}

    async def _apply_fill_increment(self, trade: dict, sizing: dict, cid: str, res, broker) -> Decimal:
        """Apply the part of ``res`` (cumulative order state) not yet recorded.
        Creates the position on first fill, grows it on later fills, and
        resizes exchange-side protection. Returns the new quantity applied."""
        mode, market, symbol, direction = trade["mode"], trade["market"], trade["symbol"], trade["direction"]
        side = "BUY" if direction == "LONG" else "SELL"
        rq, rc, rf, rn = self._recorded(cid)
        new_q = res.filled_qty - rq
        if new_q <= 0:
            return ZERO
        new_cost = res.filled_qty * res.avg_price - rc
        new_px = new_cost / new_q
        new_fee = max(ZERO, res.fee_usdt - rf)
        cum_net = res.net_base_qty if res.net_base_qty is not None else res.filled_qty
        new_net = max(ZERO, cum_net - rn)
        lev = int(sizing["leverage"]) if market == "futures" else 1
        slip = ZERO
        if res.ref_price > 0:
            slip = ((new_px - res.ref_price) if side == "BUY" else (res.ref_price - new_px)) * new_q
        pos = self.db.one("SELECT * FROM positions WHERE trade_id=?", (trade["id"],))
        with self.db.tx():
            self.db.execute(
                "INSERT INTO fills(client_id,trade_id,mode,ts,qty,price,fee_usdt,ref_price,slippage_usdt,exchange_trade_id,net_qty) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (cid, trade["id"], mode, now(), str(new_q), str(new_px), str(new_fee), str(res.ref_price), str(slip),
                 f"{res.exchange_id}:{res.filled_qty}", str(new_net)))
            if pos is None:
                margin = new_net * new_px / D(lev) if market == "futures" else ZERO
                liq = liquidation_price(new_px, lev, direction) if market == "futures" else None
                self.db.execute(
                    "INSERT INTO positions(trade_id,mode,market,symbol,direction,strategy,status,qty,entry_price,stop_price,"
                    "target_price,leverage,margin,liq_price,opened_at,fees,slippage) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (trade["id"], mode, market, symbol, direction, trade["strategy"], "OPEN", str(new_net), str(new_px),
                     sizing["stop"], sizing["target"], lev, str(margin), str(liq) if liq else None, now(),
                     str(new_fee), str(slip)))
            else:
                held, entry = D(pos["qty"]), D(pos["entry_price"])
                tot = held + new_net
                avg = (held * entry + new_net * new_px) / tot if tot > 0 else new_px
                margin = tot * avg / D(lev) if market == "futures" else ZERO
                liq = liquidation_price(avg, lev, direction) if market == "futures" else None
                self.db.execute(
                    "UPDATE positions SET qty=?, entry_price=?, margin=?, liq_price=?, fees=?, slippage=? WHERE trade_id=?",
                    (str(tot), str(avg), str(margin), str(liq) if liq else None, str(D(pos["fees"]) + new_fee),
                     str(D(pos["slippage"]) + slip), trade["id"]))
            if market == "spot":
                self.pf.post(mode, "spot", "trade", -(new_q * new_px), trade["id"], f"buy {symbol}")
            self.pf.post(mode, market, "fee", -new_fee, trade["id"], "entry fee")
        self.metrics["filled"] += 1
        target = D(sizing["qty"])
        part = f", {res.filled_qty}/{target} of order" if res.filled_qty < target else ""
        self.log("order_filled",
                 f"Executor filled {side} {new_q} {symbol} @ {new_px:.6g} (fee {new_fee:.4f} USDT, slippage "
                 f"{slip:.4f} USDT{part})",
                 trade["id"], {"avg_price": str(new_px), "qty": str(new_q), "cum_qty": str(res.filled_qty),
                               "fee": str(new_fee), "slippage": str(slip), "status": res.status, "client_id": cid})
        pos = self.db.one("SELECT * FROM positions WHERE trade_id=?", (trade["id"],))
        if pos and pos["status"] == "OPEN":
            await self._protect(pos, broker)
        return new_q

    async def _protect(self, pos: dict, broker) -> None:
        """(Re)place exchange-side protection for the CURRENT position size."""
        prot = loads(pos.get("protection")) or {}
        if broker.kind == "paper":
            if not prot:
                self.db.execute("UPDATE positions SET protection=? WHERE trade_id=?",
                                (dumps({"type": "local-monitor"}), pos["trade_id"]))
            return
        try:
            if pos["market"] == "futures":
                if prot.get("type") == "futures-algo":
                    return  # closePosition=true orders already cover any size
            else:
                if prot.get("qty") == pos["qty"]:
                    return
                if prot:
                    await broker.cancel_protection(pos)
                rules = self.md.rules["spot"].get(pos["symbol"])
                if rules and D(pos["qty"]) * D(pos["stop_price"]) < rules.min_notional:
                    self.log("protection_pending", f"{pos['symbol']} filled qty {pos['qty']} below exchange minimum for "
                             "an OCO; protected by the monitor until more fills arrive", pos["trade_id"], level="warning")
                    return
            info = await broker.place_protection(pos)
            info["qty"] = pos["qty"]
            self.db.execute("UPDATE positions SET protection=? WHERE trade_id=?", (dumps(info), pos["trade_id"]))
            self.log("protection_placed", f"Stop-loss {pos['stop_price']} / take-profit {pos['target_price']} active for "
                     f"{pos['qty']} ({info.get('type')})", pos["trade_id"], info)
        except Exception as e:  # noqa: BLE001
            inc = self.state.incident("protection_failed", "critical", {"trade_id": pos["trade_id"], "error": str(e)})
            self.log("protection_failed", f"Could not place protective orders ({e}); closing position (incident #{inc})",
                     pos["trade_id"], level="error")
            await self.close_position(pos, "protection_failed")

    async def _post_fill_checks(self, pos: dict, broker) -> None:
        stop, entry = D(pos["stop_price"]), D(pos["entry_price"])
        if (pos["direction"] == "LONG" and entry <= stop) or (pos["direction"] == "SHORT" and entry >= stop):
            await self.close_position(pos, "entry_beyond_stop")

    # -------------------------------------------------- working limit orders
    async def _apply_order_progress(self, trade: dict, cid: str, res, broker) -> dict:
        if res.filled_qty > 0:
            await self._apply_fill_increment(trade, trade["sizing"], cid, res, broker)
            if res.status == "PARTIALLY_FILLED":
                self.metrics["partial"] += 1
        self._update_order(cid, res)
        if res.status == "FILLED" and self._target_reached(trade):
            self._finish_working(trade, "filled")
        return {"ok": True, "working": True}

    def _filled_total(self, trade_id: str) -> Decimal:
        return D(self.db.scalar(
            "SELECT COALESCE(SUM(CAST(f.qty AS REAL)),0) FROM fills f JOIN orders o ON o.client_id=f.client_id "
            "WHERE f.trade_id=? AND o.purpose='entry'", (trade_id,)))

    def _target_reached(self, trade: dict) -> bool:
        return self._filled_total(trade["id"]) >= D(trade["sizing"]["qty"]) * D("0.999999")

    def _finish_working(self, trade: dict, how: str) -> None:
        filled = self._filled_total(trade["id"])
        exe = trade.get("execution_d") or {}
        exe.update({"finished": how, "filled_qty": str(filled), "finished_at": now()})
        if filled > 0:
            self._cas(trade["id"], ("WORKING",), "OPEN", execution=dumps(exe))
            pos = self.db.one("SELECT qty, entry_price FROM positions WHERE trade_id=?", (trade["id"],))
            self.log("entry_complete", f"Limit entry {how}: {filled}/{trade['sizing']['qty']} {trade['symbol']} filled"
                     + (f", avg {D(pos['entry_price']):.6g}" if pos else ""), trade["id"], exe)
            self.on_change(trade["mode"], f"entry {trade['symbol']}")
        else:
            self._cas(trade["id"], ("WORKING",), "EXPIRED" if how == "expired" else "CANCELLED",
                      execution=dumps(exe), reject_reason=f"limit entry {how} unfilled")
            self.log("entry_unfilled", f"Limit entry for {trade['symbol']} {how} with no fill", trade["id"], exe)

    async def manage_working(self) -> None:
        for t in self.db.all("SELECT id FROM trades WHERE stage='WORKING'"):
            trade = self._trade(t["id"])
            try:
                await self._manage_one(trade)
            except Exception as e:  # noqa: BLE001
                log.exception("working order %s", trade["id"])
                self.fail(e)

    async def _manage_one(self, trade: dict) -> None:
        broker = self.brokers(trade["mode"])
        exe = trade["execution_d"]
        cid = exe["working_cid"]
        market, symbol = trade["market"], trade["symbol"]
        ex = self.cfg.execution
        order = self.db.one("SELECT * FROM orders WHERE client_id=?", (cid,))
        if order and order["status"] in LIVE_ORDER:
            res = await broker.poll_order(market, symbol, cid)
            if res.status == "UNKNOWN":
                return
            await self._apply_order_progress(trade, cid, res, broker)
            trade = self._trade(trade["id"])
            if trade["stage"] != "WORKING":
                return
            if res.terminal:
                self._update_order(cid, res)
        order = self.db.one("SELECT * FROM orders WHERE client_id=?", (cid,))
        age_total = now() - exe["first_placed_at"]
        if self.state.halted or age_total >= ex.limit_expiry_seconds:
            await self.cancel_working(trade, "halted" if self.state.halted else "expired")
            return
        # Reprice when the order is dead (crossed post-only) or stale and the market moved away.
        side = "BUY" if trade["direction"] == "LONG" else "SELL"
        t = self.md.ticker(market, symbol)
        if t is None or exe["reprices"] >= ex.max_reprices + (1 if exe["placed_at"] == 0 else 0):
            if order["status"] not in LIVE_ORDER:
                await self.cancel_working(trade, "expired")
            return
        dead = order["status"] not in LIVE_ORDER
        moved = (side == "BUY" and D(t.bid) > D(exe["price"])) or (side == "SELL" and D(t.ask) < D(exe["price"]))
        if not dead and not (moved and now() - exe["placed_at"] >= ex.reprice_after_seconds):
            return
        new_price = self._limit_price(market, symbol, side, D(trade["sizing"]["entry"]))
        if new_price is None or (not dead and new_price == D(exe["price"])):
            return
        if not dead:
            res = await broker.cancel_order(market, symbol, cid)
            if res.status == "UNKNOWN":
                return  # cannot confirm the cancel: never place a second order
            await self._apply_order_progress(trade, cid, res, broker)
            self._update_order(cid, res, res.status if res.status != "NEW" else "CANCELED")
            trade = self._trade(trade["id"])
            if trade["stage"] != "WORKING":
                return
        remaining = D(trade["sizing"]["qty"]) - self._filled_total(trade["id"])
        rules = self.md.rules[market][symbol]
        remaining = rules.round_qty(remaining, market_order=False)
        if remaining <= 0 or rules.validate(remaining, new_price, market_order=False):
            self._finish_working(trade, "completed (remainder below exchange minimum)")
            return
        n = exe["reprices"] + 1
        new_cid = client_order_id(trade["id"], "E", n)
        self._order_row(new_cid, trade, side, remaining, "entry", "LIMIT", new_price)
        res = await broker.limit_order(market, symbol, side, remaining, new_price, new_cid, post_only=ex.post_only)
        self._update_order(new_cid, res)
        exe.update({"working_cid": new_cid, "price": str(new_price), "placed_at": now() if res.status != "EXPIRED" else 0,
                    "reprices": n})
        self.db.execute("UPDATE trades SET execution=?, updated_at=? WHERE id=?", (dumps(exe), now(), trade["id"]))
        self.metrics["repriced"] += 1
        self.log("order_repriced", f"Repriced {symbol} limit to {new_price} for remaining {remaining} "
                 f"(attempt {n}/{ex.max_reprices}, status {res.status})", trade["id"],
                 {"client_id": new_cid, "price": str(new_price), "qty": str(remaining), "status": res.status})
        if res.status in ("NEW", "PARTIALLY_FILLED", "FILLED"):
            await self._apply_order_progress(self._trade(trade["id"]), new_cid, res, broker)

    async def cancel_working(self, trade: dict, reason: str) -> None:
        """Cancel a working entry order, apply any final fills, then finish the trade."""
        broker = self.brokers(trade["mode"])
        exe = trade.get("execution_d") or loads(trade.get("execution")) or {}
        cid = exe.get("working_cid")
        order = self.db.one("SELECT * FROM orders WHERE client_id=?", (cid,)) if cid else None
        if order and order["status"] in LIVE_ORDER:
            res = await broker.cancel_order(trade["market"], trade["symbol"], cid)
            if res.status == "UNKNOWN":
                self.state.incident("cancel_unconfirmed", "high", {"trade_id": trade["id"], "client_id": cid})
                self.log("cancel_unconfirmed", f"Could not confirm cancel of {cid}; will retry", trade["id"], level="error")
                return
            if res.status != "NOT_FOUND":
                await self._apply_order_progress(trade, cid, res, broker)
            self._update_order(cid, res, "CANCELED" if res.status in ("NEW", "PARTIALLY_FILLED") else res.status)
        trade = self._trade(trade["id"])
        if trade["stage"] == "WORKING":
            self.metrics["expired" if reason == "expired" else "cancelled"] += 1
            self._finish_working(trade, reason)

    # ------------------------------------------------------------- exit
    async def close_position(self, pos: dict, reason: str, actor: str = "executor") -> dict:
        tid = pos["trade_id"]
        if tid in self._closing:
            return {"ok": False, "reason": "already closing"}
        t = self.db.one("SELECT stage FROM trades WHERE id=?", (tid,))
        if t and t["stage"] == "WORKING":
            # Stop the entry first so nothing is added to a position being exited.
            await self.cancel_working(self._trade(tid), f"cancelled before exit ({reason})")
            pos = self.db.one("SELECT * FROM positions WHERE trade_id=?", (tid,)) or pos
        cur = self.db.execute("UPDATE positions SET status='CLOSING' WHERE trade_id=? AND status='OPEN'", (tid,))
        if cur.rowcount != 1:
            return {"ok": False, "reason": "position not open"}
        self._closing.add(tid)
        try:
            pos = self.db.one("SELECT * FROM positions WHERE trade_id=?", (tid,))
            broker = self.brokers(pos["mode"])
            market, symbol = pos["market"], pos["symbol"]
            side = "SELL" if pos["direction"] == "LONG" else "BUY"
            qty = D(pos["qty"])
            self.busy(f"Closing {symbol} ({reason})")
            if loads(pos.get("protection")):
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
                        info = await broker.place_protection(pos)
                        self.db.execute("UPDATE positions SET protection=? WHERE trade_id=?",
                                        (dumps({**info, "qty": pos["qty"]}), tid))
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
                           "realized": str(realized), "reason": reason, "entry_price": str(entry), "qty": str(held)},
                     "info" if realized >= 0 else "warning")
            self.done(f"CLOSED {pos['symbol']} {realized:+.2f} USDT ({reason})")
        else:
            self.log("partial_exit", f"Partial exit {qty} {pos['symbol']} @ {price:.6g}; {remaining} remaining", tid)
        self.on_change(mode, f"exit {pos['symbol']}")

    # ---------------------------------------------------------- monitor
    def nudge(self, symbol: str | None = None) -> None:
        """Called on user-stream events: reconcile affected positions now."""
        for tid in list(self._last_exchange_check):
            self._last_exchange_check[tid] = 0

    async def monitor(self) -> None:
        """Called every ~2 s: working orders, stops/targets/liquidation for
        paper positions, and reconciliation of exchange positions."""
        await self.manage_working()
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
        # Same trigger as exchange-side protection: futures MARK_PRICE, spot last/mid.
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
        if not loads(pos.get("protection")):
            return  # protection not active yet (e.g. spot partial below minimum): nothing to reconcile
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
            elif res.filled_qty > 0 or res.status in ("NEW", "PARTIALLY_FILLED"):
                t["sizing"] = loads(t["sizing"])
                if res.status in ("NEW", "PARTIALLY_FILLED"):
                    res = await broker.cancel_order(t["market"], t["symbol"], cid)
                if res.filled_qty > 0:
                    await self._handle_entry_result(t, t["sizing"], cid, res, broker, final=True)
                    notes.append(f"{t['id']}: filled quantity {res.filled_qty} recovered into position")
                else:
                    self._cas(t["id"], ("EXECUTING",), "CANCELLED", reject_reason="unfilled order cancelled on restart")
                    notes.append(f"{t['id']}: unfilled order cancelled on restart")
            else:
                notes.append(f"{t['id']}: still {res.status}; manual check required")
                self.state.incident("reconcile_pending", "high", {"trade_id": t["id"], "status": res.status})
        # Working limit entries: time elapsed while down is unknown -> cancel, keep what filled.
        for row in self.db.all("SELECT id FROM trades WHERE stage='WORKING'"):
            trade = self._trade(row["id"])
            await self.cancel_working(trade, "cancelled on restart")
            trade = self._trade(row["id"])
            notes.append(f"{row['id']}: working limit entry cancelled on restart -> {trade['stage']}")
        n = self.db.execute("UPDATE positions SET status='OPEN' WHERE status='CLOSING'").rowcount
        if n:
            notes.append(f"{n} position(s) were mid-close at shutdown; restored to OPEN for re-evaluation")
        # Exchange positions whose protection is missing get it re-placed.
        for pos in self.db.all("SELECT * FROM positions WHERE status='OPEN' AND mode<>'paper'"):
            if not loads(pos.get("protection")):
                await self._protect(pos, self.brokers(pos["mode"]))
                notes.append(f"{pos['trade_id']}: protection re-placed")
        for note in notes:
            self.log("recovery", note, level="warning")
        return notes
