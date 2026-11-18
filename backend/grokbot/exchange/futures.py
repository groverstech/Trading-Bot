"""Binance USDⓈ-M Futures adapter (REST, /fapi).

Differences from Spot that matter here:
* Positions, leverage and margin type are per symbol; the bot requires
  one-way position mode (positionSide BOTH).
* Since 2025-12-09 conditional orders (STOP_MARKET, TAKE_PROFIT_MARKET, ...)
  must be placed through the Algo Service: POST /fapi/v1/algoOrder with
  ``algoType=CONDITIONAL`` and ``triggerPrice``. /fapi/v1/order rejects them
  with -4120 STOP_ORDER_SWITCH_ALGO.
* Funding payments arrive as income of type FUNDING_FEE.
"""
from __future__ import annotations

from .client import BinanceClient, BinanceError
from .filters import SymbolRules, parse_symbol


class FuturesAdapter:
    market = "futures"

    def __init__(self, data: BinanceClient, trade: BinanceClient | None = None):
        self.data = data
        self.trade = trade

    # ---------------------------------------------------------------- public
    async def server_time(self) -> int:
        return int((await self.data.request("GET", "/fapi/v1/time"))["serverTime"])

    async def exchange_info(self, symbols: list[str]) -> dict[str, SymbolRules]:
        data = await self.data.request("GET", "/fapi/v1/exchangeInfo", weight=1)
        wanted = set(symbols)
        return {s["symbol"]: parse_symbol(s, "futures") for s in data["symbols"] if s["symbol"] in wanted}

    async def klines(self, symbol: str, interval: str, limit: int = 300,
                     start_time: int | None = None, end_time: int | None = None) -> list[list]:
        w = 1 if limit < 100 else 2 if limit < 500 else 5
        return await self.data.request("GET", "/fapi/v1/klines", {
            "symbol": symbol, "interval": interval, "limit": limit,
            "startTime": start_time, "endTime": end_time}, weight=w)

    async def depth(self, symbol: str, limit: int = 100) -> dict:
        w = 5 if limit <= 100 else 10
        return await self.data.request("GET", "/fapi/v1/depth", {"symbol": symbol, "limit": limit}, weight=w)

    async def ticker_24hr_all(self) -> list[dict]:
        return await self.data.request("GET", "/fapi/v1/ticker/24hr", weight=40)

    async def book_ticker_all(self) -> list[dict]:
        return await self.data.request("GET", "/fapi/v1/ticker/bookTicker", weight=5)

    async def premium_index_all(self) -> list[dict]:
        """Mark price, index price, last funding rate and next funding time."""
        return await self.data.request("GET", "/fapi/v1/premiumIndex", weight=10)

    async def open_interest(self, symbol: str) -> dict:
        return await self.data.request("GET", "/fapi/v1/openInterest", {"symbol": symbol}, weight=1)

    async def funding_info(self) -> list[dict]:
        """Per-symbol funding interval overrides (fundingIntervalHours)."""
        return await self.data.request("GET", "/fapi/v1/fundingInfo", weight=1)

    # ---------------------------------------------------------------- signed
    def _t(self) -> BinanceClient:
        if self.trade is None or not self.trade.has_credentials:
            raise RuntimeError("Futures trading client not configured (no API credentials for this environment)")
        return self.trade

    async def account(self) -> dict:
        return await self._t().request("GET", "/fapi/v3/account", signed=True, weight=5)

    async def balance(self) -> list[dict]:
        return await self._t().request("GET", "/fapi/v3/balance", signed=True, weight=5)

    async def position_risk(self, symbol: str | None = None) -> list[dict]:
        return await self._t().request("GET", "/fapi/v3/positionRisk", {"symbol": symbol}, signed=True, weight=5)

    async def position_mode(self) -> bool:
        """True if hedge mode (dual side) is enabled."""
        r = await self._t().request("GET", "/fapi/v1/positionSide/dual", signed=True, weight=30)
        return bool(r.get("dualSidePosition"))

    async def commission(self, symbol: str) -> dict:
        return await self._t().request("GET", "/fapi/v1/commissionRate", {"symbol": symbol}, signed=True, weight=20)

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        return await self._t().request("POST", "/fapi/v1/leverage",
                                       {"symbol": symbol, "leverage": leverage}, signed=True, weight=1)

    async def set_margin_type(self, symbol: str, margin_type: str) -> None:
        try:
            await self._t().request("POST", "/fapi/v1/marginType",
                                    {"symbol": symbol, "marginType": margin_type}, signed=True, weight=1)
        except BinanceError as e:
            if e.code != -4046:  # "No need to change margin type."
                raise

    async def new_order(self, **params) -> dict:
        params.setdefault("newOrderRespType", "RESULT")
        return await self._t().request("POST", "/fapi/v1/order", params, signed=True, weight=1, order=True)

    async def query_order(self, symbol: str, client_id: str) -> dict:
        return await self._t().request("GET", "/fapi/v1/order",
                                       {"symbol": symbol, "origClientOrderId": client_id}, signed=True, weight=1)

    async def cancel_order(self, symbol: str, client_id: str) -> dict:
        return await self._t().request("DELETE", "/fapi/v1/order",
                                       {"symbol": symbol, "origClientOrderId": client_id}, signed=True, weight=1)

    async def open_orders(self, symbol: str | None = None) -> list[dict]:
        return await self._t().request("GET", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True,
                                       weight=1 if symbol else 40)

    async def user_trades(self, symbol: str, start_time: int | None = None, order_id: int | None = None) -> list[dict]:
        return await self._t().request("GET", "/fapi/v1/userTrades", {
            "symbol": symbol, "startTime": start_time, "orderId": order_id}, signed=True, weight=5)

    async def income(self, symbol: str, income_type: str = "FUNDING_FEE", start_time: int | None = None) -> list[dict]:
        return await self._t().request("GET", "/fapi/v1/income", {
            "symbol": symbol, "incomeType": income_type, "startTime": start_time, "limit": 1000},
            signed=True, weight=30)

    # -- Algo Service (conditional orders) ------------------------------------
    async def new_conditional(self, symbol: str, side: str, order_type: str, trigger_price: str,
                              client_algo_id: str, working_type: str = "MARK_PRICE") -> dict:
        assert order_type in ("STOP_MARKET", "TAKE_PROFIT_MARKET")
        return await self._t().request("POST", "/fapi/v1/algoOrder", {
            "algoType": "CONDITIONAL", "symbol": symbol, "side": side, "type": order_type,
            "triggerPrice": trigger_price, "closePosition": "true", "workingType": working_type,
            "priceProtect": "true", "clientAlgoId": client_algo_id, "newOrderRespType": "ACK",
        }, signed=True, weight=1, order=True)

    async def query_conditional(self, client_algo_id: str) -> dict:
        return await self._t().request("GET", "/fapi/v1/algoOrder", {"clientAlgoId": client_algo_id},
                                       signed=True, weight=1)

    async def cancel_conditional(self, client_algo_id: str) -> dict:
        return await self._t().request("DELETE", "/fapi/v1/algoOrder", {"clientAlgoId": client_algo_id},
                                       signed=True, weight=1)

    async def open_conditionals(self, symbol: str | None = None) -> list[dict]:
        return await self._t().request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}, signed=True, weight=1)
