"""Binance Spot adapter (REST, /api/v3).

Public endpoints work against the market-data base URL; signed endpoints use
the trading client for the selected environment (testnet or live).
Withdrawal endpoints are deliberately not implemented.
"""
from __future__ import annotations

from .client import BinanceClient
from .filters import SymbolRules, parse_symbol


class SpotAdapter:
    market = "spot"

    def __init__(self, data: BinanceClient, trade: BinanceClient | None = None):
        self.data = data
        self.trade = trade

    # ---------------------------------------------------------------- public
    async def server_time(self) -> int:
        return int((await self.data.request("GET", "/api/v3/time"))["serverTime"])

    async def exchange_info(self, symbols: list[str]) -> dict[str, SymbolRules]:
        import json
        data = await self.data.request(
            "GET", "/api/v3/exchangeInfo", {"symbols": json.dumps(symbols, separators=(",", ":"))}, weight=20)
        return {s["symbol"]: parse_symbol(s, "spot") for s in data["symbols"]}

    async def klines(self, symbol: str, interval: str, limit: int = 300,
                     start_time: int | None = None, end_time: int | None = None) -> list[list]:
        return await self.data.request("GET", "/api/v3/klines", {
            "symbol": symbol, "interval": interval, "limit": limit,
            "startTime": start_time, "endTime": end_time}, weight=2)

    async def depth(self, symbol: str, limit: int = 100) -> dict:
        w = 5 if limit <= 100 else 25
        return await self.data.request("GET", "/api/v3/depth", {"symbol": symbol, "limit": limit}, weight=w)

    async def ticker_24hr(self, symbols: list[str]) -> list[dict]:
        import json
        w = 2 if len(symbols) <= 20 else 40
        return await self.data.request(
            "GET", "/api/v3/ticker/24hr", {"symbols": json.dumps(symbols, separators=(",", ":"))}, weight=w)

    async def book_ticker(self, symbols: list[str]) -> list[dict]:
        import json
        return await self.data.request(
            "GET", "/api/v3/ticker/bookTicker", {"symbols": json.dumps(symbols, separators=(",", ":"))}, weight=4)

    # ---------------------------------------------------------------- signed
    def _t(self) -> BinanceClient:
        if self.trade is None or not self.trade.has_credentials:
            raise RuntimeError("Spot trading client not configured (no API credentials for this environment)")
        return self.trade

    async def account(self) -> dict:
        return await self._t().request("GET", "/api/v3/account", {"omitZeroBalances": "true"}, signed=True, weight=20)

    async def commission(self, symbol: str) -> dict:
        return await self._t().request("GET", "/api/v3/account/commission", {"symbol": symbol}, signed=True, weight=20)

    async def api_restrictions(self) -> dict:
        """Key permissions (only on api.binance.com; not available on testnet)."""
        return await self._t().request("GET", "/sapi/v1/account/apiRestrictions", signed=True, weight=1)

    async def new_order(self, **params) -> dict:
        params.setdefault("newOrderRespType", "FULL")
        return await self._t().request("POST", "/api/v3/order", params, signed=True, weight=1, order=True)

    async def query_order(self, symbol: str, client_id: str) -> dict:
        return await self._t().request("GET", "/api/v3/order",
                                       {"symbol": symbol, "origClientOrderId": client_id}, signed=True, weight=4)

    async def cancel_order(self, symbol: str, client_id: str) -> dict:
        return await self._t().request("DELETE", "/api/v3/order",
                                       {"symbol": symbol, "origClientOrderId": client_id}, signed=True, weight=1)

    async def open_orders(self, symbol: str | None = None) -> list[dict]:
        return await self._t().request("GET", "/api/v3/openOrders", {"symbol": symbol}, signed=True,
                                       weight=6 if symbol else 80)

    async def all_orders(self, symbol: str, limit: int = 100) -> list[dict]:
        return await self._t().request("GET", "/api/v3/allOrders", {"symbol": symbol, "limit": limit},
                                       signed=True, weight=20)

    async def my_trades(self, symbol: str, order_id: int | None = None, start_time: int | None = None) -> list[dict]:
        return await self._t().request("GET", "/api/v3/myTrades", {
            "symbol": symbol, "orderId": order_id, "startTime": start_time}, signed=True, weight=20)

    async def oco_sell(self, symbol: str, quantity: str, take_profit: str, stop_trigger: str,
                       stop_limit: str, list_client_id: str) -> dict:
        """Protective OCO for a long spot holding: LIMIT_MAKER take-profit above,
        STOP_LOSS_LIMIT below (POST /api/v3/orderList/oco)."""
        return await self._t().request("POST", "/api/v3/orderList/oco", {
            "symbol": symbol, "side": "SELL", "quantity": quantity,
            "listClientOrderId": list_client_id,
            "aboveType": "LIMIT_MAKER", "abovePrice": take_profit,
            "aboveClientOrderId": f"{list_client_id}T"[:36],
            "belowType": "STOP_LOSS_LIMIT", "belowStopPrice": stop_trigger, "belowPrice": stop_limit,
            "belowTimeInForce": "GTC", "belowClientOrderId": f"{list_client_id}S"[:36],
            "newOrderRespType": "FULL",
        }, signed=True, weight=1, order=True)

    async def query_order_list(self, list_client_id: str) -> dict:
        return await self._t().request("GET", "/api/v3/orderList",
                                       {"origClientOrderId": list_client_id}, signed=True, weight=4)

    async def cancel_order_list(self, symbol: str, list_client_id: str) -> dict:
        return await self._t().request("DELETE", "/api/v3/orderList",
                                       {"symbol": symbol, "listClientOrderId": list_client_id}, signed=True, weight=1)
