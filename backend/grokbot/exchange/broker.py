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

    async def prepare_futures(self, symbol: str, leverage: int, margin_mode: str) -> None:
        return None

    async def market_order(self, market: str, symbol: str, side: str, qty: Decimal, client_id: str,
                           reduce_only: bool = False) -> ExecResult:
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

    async def query_order(self, market: str, symbol: str, client_id: str) -> ExecResult:
        # Paper orders are written atomically with their fills; nothing to recover remotely.
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
            if status == "EXPIRED" and filled > 0:
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
        if status == "EXPIRED" and filled > 0:
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
            await self.spot.oco_sell(symbol, fmt(qty), fmt(rules.round_price(target)),
                                     fmt(rules.round_price(stop)), fmt(stop_limit), f"{cid}-P")
            return {"type": "spot-oco", "list_client_id": f"{cid}-P"}
        close_side = "SELL" if pos["direction"] == "LONG" else "BUY"
        await self.futures.new_conditional(symbol, close_side, "STOP_MARKET", fmt(rules.round_price(stop)), f"{cid}-SL")
        await self.futures.new_conditional(symbol, close_side, "TAKE_PROFIT_MARKET",
                                           fmt(rules.round_price(target)), f"{cid}-TP")
        return {"type": "futures-algo", "sl": f"{cid}-SL", "tp": f"{cid}-TP"}

    async def cancel_protection(self, pos: dict) -> None:
        cid = pos["trade_id"]
        try:
            if pos["market"] == "spot":
                await self.spot.cancel_order_list(pos["symbol"], f"{cid}-P")
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
            lst = await self.spot.query_order_list(f"{pos['trade_id']}-P")
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
