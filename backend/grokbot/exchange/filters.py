"""Exchange symbol rules (precision, minimum size, minimum notional).

Spot and USDⓈ-M Futures publish different filter shapes in exchangeInfo:
* Spot:    PRICE_FILTER, LOT_SIZE, MARKET_LOT_SIZE, NOTIONAL (minNotional,
           applyMinToMarket) or legacy MIN_NOTIONAL.
* Futures: PRICE_FILTER, LOT_SIZE, MARKET_LOT_SIZE, MIN_NOTIONAL (field
           ``notional``), PERCENT_PRICE.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..util import D, ZERO, floor_step, round_step


@dataclass(frozen=True)
class SymbolRules:
    symbol: str
    market: str
    status: str
    base: str
    quote: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    market_step: Decimal
    market_min_qty: Decimal
    market_max_qty: Decimal
    min_notional: Decimal
    order_types: tuple[str, ...] = ()
    contract_type: str = ""

    @property
    def trading(self) -> bool:
        return self.status == "TRADING"

    def round_price(self, p: Decimal) -> Decimal:
        return round_step(D(p), self.tick_size)

    def floor_price(self, p: Decimal) -> Decimal:
        return floor_step(D(p), self.tick_size)

    def round_qty(self, q: Decimal, market_order: bool = True) -> Decimal:
        step = self.market_step if (market_order and self.market_step > 0) else self.step_size
        return floor_step(D(q), step)

    def validate(self, qty: Decimal, price: Decimal, market_order: bool = True) -> list[str]:
        errs: list[str] = []
        if not self.trading:
            errs.append(f"{self.symbol} status is {self.status}, not TRADING")
        lo = self.market_min_qty if market_order and self.market_min_qty > 0 else self.min_qty
        hi = self.market_max_qty if market_order and self.market_max_qty > 0 else self.max_qty
        if qty < lo:
            errs.append(f"quantity {qty} below minimum {lo}")
        if hi > 0 and qty > hi:
            errs.append(f"quantity {qty} above maximum {hi}")
        step = self.market_step if market_order and self.market_step > 0 else self.step_size
        if step > 0 and (qty / step) % 1 != 0:
            errs.append(f"quantity {qty} not a multiple of step {step}")
        if qty * price < self.min_notional:
            errs.append(f"notional {qty * price:.4f} below minimum {self.min_notional}")
        return errs


def _f(filters: list[dict], ftype: str) -> dict:
    return next((f for f in filters if f.get("filterType") == ftype), {})


def parse_symbol(info: dict, market: str) -> SymbolRules:
    fl = info.get("filters", [])
    price, lot, mlot = _f(fl, "PRICE_FILTER"), _f(fl, "LOT_SIZE"), _f(fl, "MARKET_LOT_SIZE")
    if market == "spot":
        notional = _f(fl, "NOTIONAL") or _f(fl, "MIN_NOTIONAL")
        min_notional = D(notional.get("minNotional", "0"))
    else:
        min_notional = D(_f(fl, "MIN_NOTIONAL").get("notional", "0"))
    return SymbolRules(
        symbol=info["symbol"], market=market, status=info.get("status", "UNKNOWN"),
        base=info.get("baseAsset", ""), quote=info.get("quoteAsset", ""),
        tick_size=D(price.get("tickSize", "0")), step_size=D(lot.get("stepSize", "0")),
        min_qty=D(lot.get("minQty", "0")), max_qty=D(lot.get("maxQty", "0")),
        market_step=D(mlot.get("stepSize", "0")) or ZERO,
        market_min_qty=D(mlot.get("minQty", "0")), market_max_qty=D(mlot.get("maxQty", "0")),
        min_notional=min_notional, order_types=tuple(info.get("orderTypes", [])),
        contract_type=info.get("contractType", ""),
    )
