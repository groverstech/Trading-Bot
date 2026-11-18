"""Order execution back-ends used by the Executor agent.

* ``PaperBroker``   - never contacts a trading endpoint. Market orders are
  filled against a fresh snapshot of the REAL order book (walking levels), plus
  configurable extra slippage and fees. Thin books produce partial fills.
* ``ExchangeBroker`` - signed Binance orders on testnet or live. Ambiguous
  submissions (timeouts) are reconciled by deterministic client order id and
  are never blindly resubmitted.

Protective orders: paper positions are protected by the Executor's monitor;
exchange positions get exchange-side protection (Spot OCO via
/api/v3/orderList/oco, Futures STOP_MARKET + TAKE_PROFIT_MARKET via the Algo
Service) so they stay protected even if this application goes down.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal

from ..config_store import ConfigStore
from ..market.data import MarketData, walk_book
from ..util import D, ZERO, fmt
from .client import BinanceError, UnknownOrderState
from .futures import FuturesAdapter
from .spot import SpotAdapter

log = logging.getLogger("grokbot.broker")


def loads_safe(s) -> dict:
    import json
    try:
        return json.loads(s) if s else {}
    except (TypeError, ValueError):
        return {}


@dataclass
class ExecResult:
    status: str                    # FILLED | PARTIALLY_FILLED | REJECTED | NOT_FOUND | NEW | UNKNOWN
    filled_qty: Decimal = ZERO
    avg_price: Decimal = ZERO
    fee_usdt: Decimal = ZERO
    ref_price: Decimal = ZERO      # mid price at submission, for slippage accounting
    exchange_id: str | None = None
    error: str | None = None
    net_base_qty: Decimal | None = None  # spot: quantity actually received after base-asset fees
    realized_pnl: Decimal | None = None  # futures exchange-reported realized pnl (exits)
    raw: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in ("FILLED", "PARTIALLY_FILLED") and self.filled_qty > 0

    @property
    def terminal(self) -> bool:
        """No further fills can occur on this order."""
        return self.status in ("FILLED", "CANCELED", "EXPIRED", "REJECTED", "NOT_FOUND", "EXPIRED_IN_MATCH")

    def slippage_usdt(self, side: str) -> Decimal:
        if self.ref_price <= 0 or self.filled_qty <= 0:
            return ZERO
        diff = (self.avg_price - self.ref_price) if side == "BUY" else (self.ref_price - self.avg_price)
        return diff * self.filled_qty


@dataclass
class ExitInfo:
    closed: bool
    reason: str = ""
    exit_price: Decimal = ZERO
    qty: Decimal = ZERO
    fee_usdt: Decimal = ZERO
    realized_pnl: Decimal | None = None


class PaperBroker:
    kind = "paper"

    def __init__(self, md: MarketData, cfg: ConfigStore):
        self.md, self.cfg = md, cfg
        self.orders: dict[str, dict] = {}

    async def prepare_futures(self, symbol: str, leverage: int, margin_mode: str) -> None:
        return None

    async def market_order(self, market: str, symbol: str, side: str, qty: Decimal, client_id: str,
                           reduce_only: bool = False) -> ExecResult:
        book = self.md.book(market, symbol)
        if book is None or walk_book(book["asks"] if side == "BUY" else book["bids"], qty) is None or \
                walk_book(book["asks"] if side == "BUY" else book["bids"], qty)[1] < qty:
            book = await self.md.depth(market, symbol, 100)
        levels = book["asks"] if side == "BUY" else book["bids"]
        if not book["bids"] or not book["asks"]:
            return ExecResult("REJECTED", error="empty order book")
        mid = (D(book["bids"][0][0]) + D(book["asks"][0][0])) / 2
        walked = walk_book(levels, qty)
        if walked is None:
            return ExecResult("REJECTED", error="no liquidity")
        avg, filled = walked
        extra = D(self.cfg.fees.paper_extra_slippage_bps) / D(10_000)
        avg = avg * (1 + extra) if side == "BUY" else avg * (1 - extra)
        rules = self.md.rules[market].get(symbol)
        if rules:
            filled = rules.round_qty(filled)
        if filled <= 0:
            return ExecResult("REJECTED", error="insufficient book depth for minimum quantity")
        fee_rate = D(self.cfg.fees.spot_taker if market == "spot" else self.cfg.fees.futures_taker)
        fee = filled * avg * fee_rate
        status = "FILLED" if filled >= qty else "PARTIALLY_FILLED"
        return ExecResult(status, filled, avg, fee, mid, exchange_id=f"PAPER-{client_id}",
                          net_base_qty=filled, raw={"simulated": True, "book_levels": len(levels)})

    # ---- simulated resting limit orders (in memory; fills are persisted by the Executor as they occur)
    async def _book(self, market: str, symbol: str) -> dict:
        return self.md.book(market, symbol) or await self.md.depth(market, symbol, 100)

    async def limit_order(self, market: str, symbol: str, side: str, qty: Decimal, price: Decimal, client_id: str,
                          post_only: bool = True) -> ExecResult:
        if client_id in self.orders:
            return ExecResult("REJECTED", error="duplicate client order id")
        book = await self._book(market, symbol)
        if not book["bids"] or not book["asks"]:
            return ExecResult("REJECTED", error="empty order book")
        best_bid, best_ask = D(book["bids"][0][0]), D(book["asks"][0][0])
        marketable = price >= best_ask if side == "BUY" else price <= best_bid
        mid = (best_bid + best_ask) / 2
        o = {"market": market, "symbol": symbol, "side": side, "qty": qty, "price": price, "filled": ZERO,
             "cost": ZERO, "fee": ZERO, "status": "NEW", "ts": time.time(), "ref": mid}
        if marketable:
            if post_only:
                return ExecResult("EXPIRED", error="post-only order would immediately match (rejected as taker)",
                                  ref_price=mid)
            levels = book["asks"] if side == "BUY" else book["bids"]
            take = [[p, q] for p, q in levels if (D(p) <= price if side == "BUY" else D(p) >= price)]
            w = walk_book(take, qty)
            if w:
                avg, filled = w
                rules = self.md.rules[market].get(symbol)
                filled = rules.round_qty(filled, market_order=False) if rules else filled
                fee_rate = D(self.cfg.fees.spot_taker if market == "spot" else self.cfg.fees.futures_taker)
                o["filled"], o["cost"], o["fee"] = filled, filled * avg, filled * avg * fee_rate
        o["status"] = "FILLED" if o["filled"] >= qty else ("PARTIALLY_FILLED" if o["filled"] > 0 else "NEW")
        self.orders[client_id] = o
        return self._result(client_id)

    def _result(self, cid: str) -> ExecResult:
        o = self.orders[cid]
        avg = o["cost"] / o["filled"] if o["filled"] > 0 else ZERO
        return ExecResult(o["status"], o["filled"], avg, o["fee"], o["ref"], exchange_id=f"PAPER-{cid}",
                          net_base_qty=o["filled"], raw={"simulated": True, "limit": str(o["price"])})

    async def poll_order(self, market: str, symbol: str, client_id: str) -> ExecResult:
        """Advance a resting simulated order against the current book.

        Conservative fill model: a resting BUY at P fills only when asks trade
        THROUGH it (best ask < P); the filled quantity is capped by the ask
        liquidity priced below P. Queue position is not modelled."""
        o = self.orders.get(client_id)
        if o is None:
            return ExecResult("NOT_FOUND")
        if o["status"] in ("NEW", "PARTIALLY_FILLED"):
            book = await self._book(market, symbol)
            side, price = o["side"], o["price"]
            crossing = [q for p, q in (book["asks"] if side == "BUY" else book["bids"])
                        if (D(p) < price if side == "BUY" else D(p) > price)]
            avail = sum((D(q) for q in crossing), ZERO)
            rem = o["qty"] - o["filled"]
            take = min(rem, avail)
            rules = self.md.rules[market].get(symbol)
            if rules:
                take = rules.round_qty(take, market_order=False)
            if take > 0:
                maker = D(self.cfg.fees.spot_maker if market == "spot" else self.cfg.fees.futures_maker)
                o["filled"] += take
                o["cost"] += take * price
                o["fee"] += take * price * maker
                o["status"] = "FILLED" if o["filled"] >= o["qty"] else "PARTIALLY_FILLED"
        return self._result(client_id)

    async def cancel_order(self, market: str, symbol: str, client_id: str) -> ExecResult:
        o = self.orders.get(client_id)
        if o is None:
            return ExecResult("NOT_FOUND")
        await self.poll_order(market, symbol, client_id)  # fills up to the cancel instant
        if o["status"] in ("NEW", "PARTIALLY_FILLED"):
            o["status"] = "CANCELED"
        return self._result(client_id)

    async def query_order(self, market: str, symbol: str, client_id: str) -> ExecResult:
        if client_id in self.orders:
            return self._result(client_id)
        # Paper orders do not survive a restart; fills were persisted when they happened.
        return ExecResult("NOT_FOUND")

    async def place_protection(self, pos: dict) -> dict:
        return {"type": "local-monitor"}

    async def cancel_protection(self, pos: dict) -> None:
        return None

    async def check_exit(self, pos: dict) -> ExitInfo:
        return ExitInfo(False)  # paper exits are triggered by the Executor's price monitor


class ExchangeBroker:
    """Testnet or live Binance execution."""

    def __init__(self, env: str, spot: SpotAdapter, futures: FuturesAdapter, md: MarketData):
        assert env in ("testnet", "live")
        self.kind = env
        self.spot, self.futures, self.md = spot, futures, md

    def _ad(self, market: str):
        return self.spot if market == "spot" else self.futures

    def _to_usdt(self, amount: Decimal, asset: str, symbol: str, price: Decimal) -> Decimal:
        if asset in ("USDT", ""):
            return amount
        rules = self.md.rules["spot"].get(symbol) or self.md.rules["futures"].get(symbol)
        if rules and asset == rules.base:
            return amount * price
        p = self.md.price("spot", f"{asset}USDT")
        return amount * D(p) if p else ZERO

    async def prepare_futures(self, symbol: str, leverage: int, margin_mode: str) -> None:
        if await self.futures.position_mode():
            raise RuntimeError("Futures account is in Hedge Mode; switch to One-way Mode")
        await self.futures.set_margin_type(symbol, margin_mode)
        await self.futures.set_leverage(symbol, leverage)

    async def market_order(self, market: str, symbol: str, side: str, qty: Decimal, client_id: str,
                           reduce_only: bool = False) -> ExecResult:
        t = self.md.ticker(market, symbol)
        ref = D((t.bid + t.ask) / 2) if t and t.bid and t.ask else ZERO
        params = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": fmt(qty),
                  "newClientOrderId": client_id}
        if market == "futures" and reduce_only:
            params["reduceOnly"] = "true"
        try:
            raw = await self._ad(market).new_order(**params)
        except UnknownOrderState as e:
            log.warning("order %s state unknown after submit (%s); reconciling", client_id, e)
            res = await self._reconcile(market, symbol, client_id)
            res.ref_price = ref
            return res
        except BinanceError as e:
            return ExecResult("REJECTED", error=f"{e.code}: {e.msg}", ref_price=ref)
        res = await self._parse(market, symbol, raw)
        res.ref_price = ref
        return res

    async def limit_order(self, market: str, symbol: str, side: str, qty: Decimal, price: Decimal, client_id: str,
                          post_only: bool = True) -> ExecResult:
        t = self.md.ticker(market, symbol)
        ref = D((t.bid + t.ask) / 2) if t and t.bid and t.ask else ZERO
        if market == "spot":
            params = {"symbol": symbol, "side": side, "type": "LIMIT_MAKER" if post_only else "LIMIT",
                      "quantity": fmt(qty), "price": fmt(price), "newClientOrderId": client_id}
            if not post_only:
                params["timeInForce"] = "GTC"
        else:
            params = {"symbol": symbol, "side": side, "type": "LIMIT", "timeInForce": "GTX" if post_only else "GTC",
                      "quantity": fmt(qty), "price": fmt(price), "newClientOrderId": client_id}
        try:
            raw = await self._ad(market).new_order(**params)
        except UnknownOrderState:
            res = await self._reconcile(market, symbol, client_id)
            res.ref_price = ref
            return res
        except BinanceError as e:
            # Spot LIMIT_MAKER that would cross is rejected with -2010.
            status = "EXPIRED" if (post_only and e.code == -2010 and "immediately match" in e.msg) else "REJECTED"
            return ExecResult(status, error=f"{e.code}: {e.msg}", ref_price=ref)
        res = await self._parse(market, symbol, raw)
        res.ref_price = ref
        return res

    async def poll_order(self, market: str, symbol: str, client_id: str) -> ExecResult:
        return await self.query_order(market, symbol, client_id)

    async def cancel_order(self, market: str, symbol: str, client_id: str) -> ExecResult:
        try:
            await self._ad(market).cancel_order(symbol, client_id)
        except BinanceError as e:
            if e.code not in (-2011, -2013):  # unknown order: already final
                return ExecResult("UNKNOWN", error=f"cancel failed: {e.code}: {e.msg}")
        # The cancel response can race with fills: always re-query the final state.
        return await self.query_order(market, symbol, client_id)

    async def _reconcile(self, market: str, symbol: str, client_id: str) -> ExecResult:
        for attempt in range(5):
            await asyncio.sleep(1 + attempt)
            r = await self.query_order(market, symbol, client_id)
            if r.status != "UNKNOWN":
                return r
        return ExecResult("UNKNOWN", error="order state could not be determined; manual check required")

    async def query_order(self, market: str, symbol: str, client_id: str) -> ExecResult:
        try:
            raw = await self._ad(market).query_order(symbol, client_id)
        except BinanceError as e:
            if e.code == -2013:  # Order does not exist
                return ExecResult("NOT_FOUND")
            return ExecResult("UNKNOWN", error=str(e))
        return await self._parse(market, symbol, raw)

    async def _parse(self, market: str, symbol: str, raw: dict) -> ExecResult:
        status = raw.get("status", "UNKNOWN")
        filled = D(raw.get("executedQty", "0"))
        oid = str(raw.get("orderId", ""))
        if market == "spot":
            quote = D(raw.get("cummulativeQuoteQty", "0"))
            avg = quote / filled if filled > 0 else ZERO
            fills = raw.get("fills")
            if fills is None and filled > 0:
                fills = await self.spot.my_trades(symbol, order_id=int(oid))
            fee, base_fee = ZERO, ZERO
            rules = self.md.rules["spot"].get(symbol)
            for f in fills or []:
                c, a = D(f.get("commission", "0")), f.get("commissionAsset", "")
                fee += self._to_usdt(c, a, symbol, avg)
                if rules and a == rules.base:
                    base_fee += c
            net = filled - base_fee
            if rules:
                net = rules.round_qty(net, market_order=False)
            if status == "EXPIRED" and filled > 0 and raw.get("type") == "MARKET":
                status = "PARTIALLY_FILLED"
            return ExecResult(status, filled, avg, fee, exchange_id=oid, net_base_qty=net, raw=raw)
        avg = D(raw.get("avgPrice", "0"))
        fee, rpnl = ZERO, ZERO
        if filled > 0:
            trades = await self.futures.user_trades(symbol, order_id=int(oid))
            for tr in trades:
                fee += self._to_usdt(D(tr["commission"]), tr.get("commissionAsset", "USDT"), symbol, avg)
                rpnl += D(tr.get("realizedPnl", "0"))
            if avg <= 0 and trades:
                q = sum(D(tr["qty"]) for tr in trades)
                avg = sum(D(tr["qty"]) * D(tr["price"]) for tr in trades) / q
        if status == "EXPIRED" and filled > 0 and raw.get("type") == "MARKET":
            status = "PARTIALLY_FILLED"
        return ExecResult(status, filled, avg, fee, exchange_id=oid, net_base_qty=filled,
                          realized_pnl=rpnl, raw=raw)

    # ---------------------------------------------------------- protection
    async def place_protection(self, pos: dict) -> dict:
        market, symbol, cid = pos["market"], pos["symbol"], pos["trade_id"]
        rules = self.md.rules[market][symbol]
        stop, target = D(pos["stop_price"]), D(pos["target_price"])
        if market == "spot":
            qty = rules.round_qty(D(pos["qty"]), market_order=False)
            stop_limit = rules.floor_price(stop * D("0.995"))
            prev = loads_safe(pos.get("protection"))
            version = int(prev.get("version", 0)) + 1  # OCO resized after partial fills gets a new list id
            lid = f"{cid}-P{version}"
            await self.spot.oco_sell(symbol, fmt(qty), fmt(rules.round_price(target)),
                                     fmt(rules.round_price(stop)), fmt(stop_limit), lid)
            return {"type": "spot-oco", "list_client_id": lid, "version": version}
        close_side = "SELL" if pos["direction"] == "LONG" else "BUY"
        await self.futures.new_conditional(symbol, close_side, "STOP_MARKET", fmt(rules.round_price(stop)), f"{cid}-SL")
        await self.futures.new_conditional(symbol, close_side, "TAKE_PROFIT_MARKET",
                                           fmt(rules.round_price(target)), f"{cid}-TP")
        return {"type": "futures-algo", "sl": f"{cid}-SL", "tp": f"{cid}-TP"}

    async def cancel_protection(self, pos: dict) -> None:
        cid = pos["trade_id"]
        try:
            if pos["market"] == "spot":
                lid = loads_safe(pos.get("protection")).get("list_client_id", f"{cid}-P1")
                await self.spot.cancel_order_list(pos["symbol"], lid)
            else:
                for suffix in ("SL", "TP"):
                    try:
                        await self.futures.cancel_conditional(f"{cid}-{suffix}")
                    except BinanceError:
                        pass
        except BinanceError as e:
            log.info("cancel protection %s: %s", cid, e)

    async def check_exit(self, pos: dict) -> ExitInfo:
        """Detect whether exchange-side protection closed the position."""
        market, symbol = pos["market"], pos["symbol"]
        start_ms = int(pos["opened_at"] * 1000)
        if market == "spot":
            lid = loads_safe(pos.get("protection")).get("list_client_id", f"{pos['trade_id']}-P1")
            lst = await self.spot.query_order_list(lid)
            if lst.get("listOrderStatus") != "ALL_DONE":
                return ExitInfo(False)
            for o in lst.get("orders", []):
                od = await self.spot.query_order(symbol, o["clientOrderId"])
                if od.get("status") == "FILLED":
                    r = await self._parse("spot", symbol, od)
                    reason = "take_profit" if od.get("type") == "LIMIT_MAKER" else "stop_loss"
                    return ExitInfo(True, reason, r.avg_price, r.filled_qty, r.fee_usdt)
            return ExitInfo(True, "protection_cancelled_externally")
        risk = await self.futures.position_risk(symbol)
        amt = sum(D(p.get("positionAmt", "0")) for p in risk)
        if amt != 0:
            return ExitInfo(False)
        close_side = "SELL" if pos["direction"] == "LONG" else "BUY"
        trades = [t for t in await self.futures.user_trades(symbol, start_time=start_ms) if t["side"] == close_side]
        await self.cancel_protection(pos)
        if not trades:
            return ExitInfo(True, "closed_externally")
        q = sum(D(t["qty"]) for t in trades)
        px = sum(D(t["qty"]) * D(t["price"]) for t in trades) / q
        fee = sum(self._to_usdt(D(t["commission"]), t.get("commissionAsset", "USDT"), symbol, px) for t in trades)
        rpnl = sum(D(t.get("realizedPnl", "0")) for t in trades)
        stop = D(pos["stop_price"])
        reason = "stop_loss" if abs(px - stop) < abs(px - D(pos["target_price"])) else "take_profit"
        return ExitInfo(True, reason, px, q, fee, rpnl)

    async def funding_paid(self, pos: dict) -> Decimal:
        rows = await self.futures.income(pos["symbol"], "FUNDING_FEE", int(pos["opened_at"] * 1000))
        return sum((D(r["income"]) for r in rows), ZERO)

    async def balances(self) -> dict:
        out: dict = {}
        try:
            acct = await self.spot.account()
            out["spot"] = {b["asset"]: {"free": b["free"], "locked": b["locked"]} for b in acct.get("balances", [])}
            out["spot_can_trade"] = acct.get("canTrade")
        except Exception as e:  # noqa: BLE001
            out["spot_error"] = str(e)
        try:
            out["futures"] = {b["asset"]: {"balance": b["balance"], "available": b["availableBalance"]}
                              for b in await self.futures.balance()}
        except Exception as e:  # noqa: BLE001
            out["futures_error"] = str(e)
        out["ts"] = time.time()
        return out
