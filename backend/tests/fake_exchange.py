"""In-memory Binance simulator served through httpx.MockTransport.

Implements the subset of Spot (/api/v3) and USDⓈ-M Futures (/fapi) REST
endpoints the bot uses, with failure injection, so adapters, brokers and
the full pipeline can be tested without network access.
"""
from __future__ import annotations

import json
import math
import time
from urllib.parse import parse_qsl, urlsplit

import httpx

RULES = {
    "BTCUSDT": {"tick": "0.01", "step": "0.00001", "minq": "0.00001", "fstep": "0.001", "ftick": "0.10", "fmin_notional": "100"},
    "ETHUSDT": {"tick": "0.01", "step": "0.0001", "minq": "0.0001", "fstep": "0.001", "ftick": "0.01", "fmin_notional": "20"},
}


def synthetic_klines(n: int, start: float, drift: float, tf_ms: int, end_ms: int, vol: float = 0.002,
                     last_vol_mult: float = 1.0) -> list[list]:
    out = []
    p = start
    t0 = end_ms - n * tf_ms
    for i in range(n):
        o = p
        c = p * (1 + drift + vol * math.sin(i / 3.0))
        h, l = max(o, c) * (1 + vol / 2), min(o, c) * (1 - vol / 2)
        v = 100 * (last_vol_mult if i == n - 1 else 1) * (1 + 0.1 * math.cos(i))
        ot = t0 + i * tf_ms
        out.append([ot, f"{o:.2f}", f"{h:.2f}", f"{l:.2f}", f"{c:.2f}", f"{v:.4f}", ot + tf_ms - 1, f"{v * c:.2f}",
                    100, "0", "0", "0"])
        p = c
    return out


class FakeBinance:
    def __init__(self):
        self.prices = {"BTCUSDT": 60000.0, "ETHUSDT": 3000.0}
        self.spread = {"BTCUSDT": 0.01, "ETHUSDT": 0.01}
        self.depth_qty = 50.0          # base qty per level
        self.funding = 0.0001
        self.quote_volume = 5e9
        self.klines_override: dict = {}
        self.orders: dict[str, dict] = {}        # clientOrderId -> order
        self.order_lists: dict[str, dict] = {}
        self.algo: dict[str, dict] = {}
        self.positions: dict[str, float] = {}    # futures positionAmt
        self.trades: list[dict] = []
        self.calls: list[tuple[str, str, dict]] = []
        self.fail_next_order_timeout = False     # order accepted, but response lost
        self.fail_market_data = False
        self.reject_next_order: tuple[int, str] | None = None
        self.partial_fill_ratio = 1.0
        self.withdrawals_enabled = False
        self.hedge_mode = False
        self._oid = 1000
        self.cancels: list[str] = []

    # ------------------------------------------------------------ helpers
    def _json(self, data, status=200):
        return httpx.Response(status, json=data, headers={"x-mbx-used-weight-1m": "10"})

    def _book(self, sym):
        p, s = self.prices[sym], self.spread[sym]
        bids = [[f"{p - s / 2 - i * p * 0.0001:.2f}", f"{self.depth_qty:.5f}"] for i in range(100)]
        asks = [[f"{p + s / 2 + i * p * 0.0001:.2f}", f"{self.depth_qty:.5f}"] for i in range(100)]
        return {"lastUpdateId": 1, "bids": bids, "asks": asks}

    def _fill(self, market, sym, side, qty, cid, reduce_only=False):
        self._oid += 1
        filled = qty * self.partial_fill_ratio
        p = self.prices[sym] * (1.0001 if side == "BUY" else 0.9999)
        o = {"symbol": sym, "orderId": self._oid, "clientOrderId": cid, "side": side, "type": "MARKET",
             "origQty": str(qty), "executedQty": f"{filled:.8f}", "status": "FILLED" if filled >= qty else "EXPIRED",
             "cummulativeQuoteQty": f"{filled * p:.8f}", "avgPrice": f"{p:.8f}", "market": market}
        self.orders[cid] = o
        fee = filled * p * (0.001 if market == "spot" else 0.0005)
        tr = {"symbol": sym, "orderId": self._oid, "side": side, "qty": f"{filled:.8f}", "price": f"{p:.8f}",
              "commission": f"{fee:.8f}", "commissionAsset": "USDT", "realizedPnl": "0", "time": int(time.time() * 1000),
              "market": market}
        self.trades.append(tr)
        if market == "futures":
            amt = filled if side == "BUY" else -filled
            self.positions[sym] = round(self.positions.get(sym, 0.0) + amt, 8)
        o["fills"] = [{"price": f"{p:.8f}", "qty": f"{filled:.8f}", "commission": f"{fee:.8f}", "commissionAsset": "USDT"}]
        return o

    # ------------------------------------------------------------ handler
    def handler(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        path, host = url.path, url.netloc
        q = dict(parse_qsl(url.query))
        self.calls.append((request.method, path, q))
        if "er-api" in host:
            return self._json({"result": "success", "rates": {"INR": 85.0}, "time_last_update_unix": 1})
        market = "futures" if path.startswith("/fapi") else "spot"
        if self.fail_market_data and request.method == "GET" and "signature" not in q:
            return httpx.Response(503, text="unavailable")
        # ---------------- public
        if path in ("/api/v3/time", "/fapi/v1/time"):
            return self._json({"serverTime": int(time.time() * 1000)})
        if path in ("/api/v3/exchangeInfo", "/fapi/v1/exchangeInfo"):
            syms = []
            for s, r in RULES.items():
                fut = market == "futures"
                syms.append({"symbol": s, "status": "TRADING", "baseAsset": s[:-4], "quoteAsset": "USDT",
                             "contractType": "PERPETUAL" if fut else "", "orderTypes": ["MARKET", "LIMIT"],
                             "filters": [
                                 {"filterType": "PRICE_FILTER", "tickSize": r["ftick"] if fut else r["tick"]},
                                 {"filterType": "LOT_SIZE", "stepSize": r["fstep"] if fut else r["step"],
                                  "minQty": r["fstep"] if fut else r["minq"], "maxQty": "9000"},
                                 {"filterType": "MARKET_LOT_SIZE", "stepSize": r["fstep"] if fut else "0",
                                  "minQty": r["fstep"] if fut else "0", "maxQty": "1000"},
                                 ({"filterType": "MIN_NOTIONAL", "notional": r["fmin_notional"]} if fut else
                                  {"filterType": "NOTIONAL", "minNotional": "5"})]})
            return self._json({"symbols": syms})
        if path in ("/api/v3/ticker/24hr", "/fapi/v1/ticker/24hr"):
            return self._json([{"symbol": s, "lastPrice": str(p), "priceChangePercent": "1.0", "highPrice": str(p * 1.02),
                                "lowPrice": str(p * 0.98), "quoteVolume": str(self.quote_volume)}
                               for s, p in self.prices.items()])
        if path in ("/api/v3/ticker/bookTicker", "/fapi/v1/ticker/bookTicker"):
            return self._json([{"symbol": s, "bidPrice": str(p - self.spread[s] / 2), "askPrice": str(p + self.spread[s] / 2)}
                               for s, p in self.prices.items()])
        if path == "/fapi/v1/premiumIndex":
            return self._json([{"symbol": s, "markPrice": str(p), "indexPrice": str(p), "lastFundingRate": str(self.funding),
                                "nextFundingTime": int(time.time() // 28800 + 1) * 28800000} for s, p in self.prices.items()])
        if path == "/fapi/v1/openInterest":
            return self._json({"symbol": q["symbol"], "openInterest": "12345"})
        if path in ("/api/v3/depth", "/fapi/v1/depth"):
            return self._json(self._book(q["symbol"]))
        if path in ("/api/v3/klines", "/fapi/v1/klines"):
            key = (q["symbol"], q["interval"])
            if key in self.klines_override:
                return self._json(self.klines_override[key])
            tf_ms = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}[q["interval"]]
            end = int(q.get("endTime") or time.time() * 1000) // tf_ms * tf_ms + tf_ms
            n = int(q.get("limit", 300))
            start = self.prices[q["symbol"]] * 0.97
            return self._json(synthetic_klines(n, start, 0.0001, tf_ms, end))
        # ---------------- signed
        if "signature" not in q:
            return self._json({"code": -2015, "msg": "missing signature"}, 401)
        if path == "/fapi/v1/listenKey":
            return self._json({"listenKey": "fake-listen-key"})
        if path == "/sapi/v1/account/apiRestrictions":
            return self._json({"enableWithdrawals": self.withdrawals_enabled, "enableSpotAndMarginTrading": True,
                               "enableFutures": True, "ipRestrict": True})
        if path == "/api/v3/account":
            return self._json({"canTrade": True, "canWithdraw": False,
                               "balances": [{"asset": "USDT", "free": "10000", "locked": "0"}]})
        if path in ("/fapi/v3/account",):
            return self._json({"assets": [{"asset": "USDT", "walletBalance": "5000", "availableBalance": "5000"}]})
        if path == "/fapi/v3/balance":
            return self._json([{"asset": "USDT", "balance": "5000", "availableBalance": "5000"}])
        if path == "/fapi/v1/positionSide/dual":
            return self._json({"dualSidePosition": self.hedge_mode})
        if path == "/fapi/v1/leverage":
            return self._json({"symbol": q["symbol"], "leverage": int(q["leverage"]), "maxNotionalValue": "1000000"})
        if path == "/fapi/v1/marginType":
            return self._json({"code": 200, "msg": "success"})
        if path in ("/api/v3/account/commission", "/fapi/v1/commissionRate"):
            return self._json({"symbol": q["symbol"], "standardCommission": {"taker": "0.001", "maker": "0.001"},
                               "takerCommissionRate": "0.0005", "makerCommissionRate": "0.0002"})
        if path == "/fapi/v1/openAlgoOrders":
            return self._json([v for v in self.algo.values() if v.get("algoStatus") == "NEW"])
        if path in ("/api/v3/order", "/fapi/v1/order"):
            cid = q.get("newClientOrderId") or q.get("origClientOrderId")
            if request.method == "POST":
                if self.reject_next_order:
                    code, msg = self.reject_next_order
                    self.reject_next_order = None
                    return self._json({"code": code, "msg": msg}, 400)
                if cid in self.orders and self.orders[cid]["status"] not in ("FILLED",):
                    return self._json({"code": -2010, "msg": "Duplicate order sent."}, 400)
                if cid in self.orders:
                    return self._json({"code": -2010, "msg": "Duplicate clientOrderId"}, 400)
                if q["type"] in ("LIMIT", "LIMIT_MAKER"):
                    r = self._place_limit(market, q, cid)
                    if r.get("__error__"):
                        return self._json({"code": -2010, "msg": "Order would immediately match and take."}, 400)
                    return self._json(r)
                o = self._fill(market, q["symbol"], q["side"], float(q["quantity"]), cid, q.get("reduceOnly") == "true")
                if self.fail_next_order_timeout:
                    self.fail_next_order_timeout = False
                    raise httpx.ReadTimeout("simulated timeout after order accepted", request=request)
                return self._json(o)
            if request.method == "DELETE":
                o = self.orders.get(cid)
                if not o or o["status"] not in ("NEW", "PARTIALLY_FILLED"):
                    return self._json({"code": -2011, "msg": "Unknown order sent."}, 400)
                o["status"] = "CANCELED"
                self.cancels.append(cid)
                return self._json({k: v for k, v in o.items() if k != "fills"})
            if request.method == "GET":
                if cid not in self.orders:
                    return self._json({"code": -2013, "msg": "Order does not exist."}, 400)
                o = dict(self.orders[cid])
                o.pop("fills", None)
                return self._json(o)
        if path == "/api/v3/myTrades":
            return self._json([t for t in self.trades if t["market"] == "spot" and t["symbol"] == q["symbol"]
                               and (not q.get("orderId") or str(t["orderId"]) == q.get("orderId"))])
        if path == "/fapi/v1/userTrades":
            ts = [t for t in self.trades if t["market"] == "futures" and t["symbol"] == q["symbol"]]
            if q.get("orderId"):
                ts = [t for t in ts if str(t["orderId"]) == q["orderId"]]
            return self._json(ts)
        if path == "/api/v3/orderList/oco":
            self.order_lists[q["listClientOrderId"]] = {"listOrderStatus": "EXECUTING", "params": q,
                                                        "orders": [{"clientOrderId": q["aboveClientOrderId"]},
                                                                   {"clientOrderId": q["belowClientOrderId"]}]}
            return self._json({"orderListId": 1, "listClientOrderId": q["listClientOrderId"]})
        if path == "/api/v3/orderList":
            lid = q.get("origClientOrderId") or q.get("listClientOrderId")
            if request.method == "DELETE":
                self.order_lists.get(lid, {})["listOrderStatus"] = "ALL_DONE"
                return self._json({"ok": True})
            return self._json(self.order_lists.get(lid, {"listOrderStatus": "ALL_DONE", "orders": []}))
        if path == "/fapi/v1/algoOrder":
            cid = q.get("clientAlgoId")
            if request.method == "POST":
                assert q["algoType"] == "CONDITIONAL" and "triggerPrice" in q
                self.algo[cid] = {**q, "algoStatus": "NEW"}
                return self._json({"algoId": len(self.algo), "clientAlgoId": cid})
            if request.method == "DELETE":
                if cid in self.algo:
                    self.algo[cid]["algoStatus"] = "CANCELED"
                return self._json({"ok": True})
            return self._json(self.algo.get(cid, {}))
        if path == "/fapi/v3/positionRisk":
            sym = q.get("symbol")
            amt = self.positions.get(sym, 0.0)
            ep = self.prices.get(sym, 0)
            return self._json([{"symbol": sym, "positionAmt": str(amt), "entryPrice": str(ep), "leverage": "2",
                                "liquidationPrice": f"{ep * 0.5 / 0.996:.2f}" if amt > 0 else "0", "markPrice": str(ep),
                                "isolatedMargin": "0", "notional": str(amt * ep)}])
        if path == "/fapi/v1/income":
            return self._json([])
        return self._json({"code": -1, "msg": f"unhandled {request.method} {path}"}, 404)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def _place_limit(self, market, q, cid):
        sym, side, price, qty = q["symbol"], q["side"], float(q["price"]), float(q["quantity"])
        p, sp = self.prices[sym], self.spread[sym]
        bid, ask = p - sp / 2, p + sp / 2
        crosses = price >= ask if side == "BUY" else price <= bid
        post_only = q["type"] == "LIMIT_MAKER" or q.get("timeInForce") == "GTX"
        self._oid += 1
        o = {"symbol": sym, "orderId": self._oid, "clientOrderId": cid, "side": side, "type": "LIMIT",
             "price": q["price"], "origQty": q["quantity"], "executedQty": "0", "cummulativeQuoteQty": "0",
             "avgPrice": "0", "status": "NEW", "market": market}
        if crosses and post_only:
            if market == "spot":
                return {"__error__": True}
            o["status"] = "EXPIRED"
        self.orders[cid] = o
        return {k: v for k, v in o.items() if k != "market"}

    def fill_resting(self, cid: str, qty: float):
        """Fill a resting limit order (partially or fully) at its limit price."""
        o = self.orders[cid]
        market, sym, side = o["market"], o["symbol"], o["side"]
        price = float(o["price"])
        done = float(o["executedQty"]) + qty
        o["executedQty"] = f"{done:.8f}"
        o["cummulativeQuoteQty"] = f"{done * price:.8f}"
        o["avgPrice"] = f"{price:.8f}"
        o["status"] = "FILLED" if done >= float(o["origQty"]) - 1e-12 else "PARTIALLY_FILLED"
        fee = qty * price * (0.001 if market == "spot" else 0.0002)
        self.trades.append({"symbol": sym, "orderId": o["orderId"], "side": side, "qty": f"{qty:.8f}",
                            "price": f"{price:.8f}", "commission": f"{fee:.8f}", "commissionAsset": "USDT",
                            "realizedPnl": "0", "time": int(time.time() * 1000), "market": market})
        if market == "futures":
            self.positions[sym] = round(self.positions.get(sym, 0.0) + (qty if side == "BUY" else -qty), 8)

    # simulate protective stop trigger on futures
    def trigger_futures_stop(self, sym: str, cid_prefix: str):
        amt = self.positions.get(sym, 0.0)
        side = "SELL" if amt > 0 else "BUY"
        self._fill("futures", sym, side, abs(amt), f"{cid_prefix}-SLX")
        self.positions[sym] = 0.0


def dumps(o):
    return json.dumps(o)
