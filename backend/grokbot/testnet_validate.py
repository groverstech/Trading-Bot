"""Binance TESTNET validation harness.

    python -m grokbot.testnet_validate [--market spot|futures|both] [--symbol BTCUSDT]

Credentials (TEST ENVIRONMENT ONLY) are read from the environment:
    GROK_TESTNET_SPOT_KEY / GROK_TESTNET_SPOT_SECRET          (testnet.binance.vision)
    GROK_TESTNET_FUTURES_KEY / GROK_TESTNET_FUTURES_SECRET    (testnet.binancefuture.com)

Refuses to run against any non-testnet host. Every step records the request
(signature/keys removed) and the exchange's actual response, and writes
docs/testnet_report.md + data/testnet_report.json.

Steps exercise the same adapter/broker code the Executor uses: connectivity,
time sync, exchange filters, market data, balances, limit order + ack,
order status, duplicate-client-id rejection, cancellation, market order +
fills/commission, partial-fill probe, stop-loss + take-profit placement
(Spot OCO / Futures Algo conditional orders), protection query + cancel,
position reconciliation, trade history, restart recovery by client order id,
and (Futures) the user-data stream. Without credentials, the unauthenticated
steps still run and a dummy-key probe proves signed requests reach the
exchange.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import traceback
from decimal import Decimal
from pathlib import Path

from .exchange.broker import ExchangeBroker
from .exchange.client import BinanceClient, BinanceError
from .exchange.futures import FuturesAdapter
from .exchange.spot import SpotAdapter
from .market.data import MarketData
from .util import D, fmt

SPOT = "https://testnet.binance.vision"
FUT = "https://testnet.binancefuture.com"
FUT_WS = "wss://fstream.binancefuture.com"


def _scrub(o):
    if isinstance(o, dict):
        return {k: ("***" if k.lower() in ("signature", "x-mbx-apikey", "listenkey") else _scrub(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [_scrub(v) for v in o[:12]] + (["…"] if len(o) > 12 else [])
    return o


class Recorder:
    def __init__(self, market: str):
        self.market = market
        self.steps: list[dict] = []

    async def step(self, name: str, fn, expect=None, critical: bool = True):
        t0 = time.time()
        rec = {"market": self.market, "step": name, "ts": t0, "critical": critical}
        try:
            out = await fn()
            rec["response"] = _scrub(out)
            ok, note = expect(out) if expect else (True, "")
            rec["status"] = "PASS" if ok else "FAIL"
            rec["note"] = note
        except BinanceError as e:
            rec["status"] = "FAIL" if not (expect and getattr(expect, "accepts_error", False)) else "PASS"
            rec["response"] = {"http": e.status, "code": e.code, "msg": e.msg, "path": e.path}
            if expect and getattr(expect, "accepts_error", False):
                ok, note = expect(e)
                rec["status"], rec["note"] = ("PASS" if ok else "FAIL"), note
        except Exception as e:  # noqa: BLE001
            rec["status"] = "FAIL"
            rec["response"] = {"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-600:]}
        rec["ms"] = round((time.time() - t0) * 1000)
        self.steps.append(rec)
        print(f"[{self.market}] {rec['status']:<4} {name} ({rec['ms']} ms) {rec.get('note', '')}", flush=True)
        return rec

    def skip(self, name: str, why: str):
        self.steps.append({"market": self.market, "step": name, "status": "SKIPPED", "note": why, "ts": time.time()})
        print(f"[{self.market}] SKIP {name}: {why}", flush=True)


def accepts_error(fn):
    fn.accepts_error = True
    return fn


async def run_market(market: str, symbol: str, key: str | None, secret: str | None) -> list[dict]:
    base = SPOT if market == "spot" else FUT
    rec = Recorder(market)
    pub = BinanceClient(base, market)
    ad = SpotAdapter(pub) if market == "spot" else FuturesAdapter(pub)
    st: dict = {}

    # ---------------- public
    await rec.step("connectivity + server time", lambda: pub.request("GET", pub.time_path),
                   lambda r: (abs(r["serverTime"] / 1000 - time.time()) < 30, f"serverTime {r['serverTime']}"))
    await rec.step("clock sync (offset ms)", lambda: _offset(pub), lambda r: (abs(r) < 5000, f"offset {r} ms"))

    async def rules():
        st["rules"] = (await ad.exchange_info([symbol]))[symbol]
        r = st["rules"]
        return {"status": r.status, "tick": str(r.tick_size), "step": str(r.step_size), "min_qty": str(r.min_qty),
                "min_notional": str(r.min_notional)}
    res = await rec.step("exchange filters (precision / min size / min notional)", rules,
                         lambda r: (r["status"] == "TRADING", f"min notional {r['min_notional']}"))
    if res["status"] != "PASS":
        for n in ("market data", "signed steps"):
            rec.skip(n, f"{base} not reachable from this environment")
        await pub.close()
        return rec.steps

    async def mdata():
        if market == "spot":
            t = (await ad.ticker_24hr([symbol]))[0]
            bk = (await ad.book_ticker([symbol]))[0]
        else:
            t = next(x for x in await ad.ticker_24hr_all() if x["symbol"] == symbol)
            bk = next(x for x in await ad.book_ticker_all() if x["symbol"] == symbol)
            pi = next(x for x in await ad.premium_index_all() if x["symbol"] == symbol)
            st["mark"] = float(pi["markPrice"])
            t = {**t, "markPrice": pi["markPrice"], "lastFundingRate": pi["lastFundingRate"]}
        st["bid"], st["ask"] = float(bk["bidPrice"]), float(bk["askPrice"])
        kl = await ad.klines(symbol, "1m", 5)
        depth = await ad.depth(symbol, 20)
        return {"last": t.get("lastPrice"), "bid": bk["bidPrice"], "ask": bk["askPrice"],
                "mark": t.get("markPrice"), "funding": t.get("lastFundingRate"), "klines": len(kl),
                "depth_levels": [len(depth["bids"]), len(depth["asks"])]}
    await rec.step("market data (ticker, book, klines, depth)", mdata,
                   lambda r: (r["klines"] == 5 and r["depth_levels"][0] > 0, f"bid {r['bid']} / ask {r['ask']}"))

    if not key or not secret:
        @accepts_error
        def expect_key_rejected(e):
            return (isinstance(e, BinanceError) and e.code in (-2014, -2015, -1022)), f"exchange answered {getattr(e, 'code', e)}"
        dummy = BinanceClient(base, market, "x" * 64, "y" * 64)
        await rec.step("signed request reaches exchange (dummy key → expected rejection)",
                       lambda: dummy.request("GET", "/api/v3/account" if market == "spot" else "/fapi/v3/account", signed=True),
                       expect_key_rejected, critical=False)
        await dummy.close()
        for n in ["account balances", "commission rates", "limit order + acknowledgement", "order status",
                  "duplicate client order id rejected", "cancel order", "market order + fills/commission",
                  "partial-fill probe", "stop-loss + take-profit placement", "protection query / cancel",
                  "position reconciliation", "trade history", "restart recovery by client order id",
                  *(["user-data stream event"] if market == "futures" else []), "flatten / cleanup"]:
            rec.skip(n, f"no testnet credentials (set GROK_TESTNET_{market.upper()}_KEY / _SECRET)")
        await pub.close()
        return rec.steps

    # ---------------- signed
    tc = BinanceClient(base, market, key, secret)
    tad = SpotAdapter(pub, tc) if market == "spot" else FuturesAdapter(pub, tc)
    md = MarketData(SpotAdapter(pub), FuturesAdapter(pub)) if market == "spot" else MarketData(SpotAdapter(BinanceClient(SPOT, "spot")), FuturesAdapter(pub), True)
    md.rules[market][symbol] = st["rules"]
    broker = ExchangeBroker("testnet", tad if market == "spot" else SpotAdapter(BinanceClient(SPOT, "spot")),
                            tad if market == "futures" else FuturesAdapter(pub), md)
    rules = st["rules"]
    tag = f"GBV{int(time.time())}"
    user_events: list = []
    ustream_task = None

    if market == "spot":
        await rec.step("account balances", lambda: _spot_bal(tad, st),
                       lambda r: (float(r.get("USDT", {}).get("free", 0)) > 20, f"USDT free {r.get('USDT', {}).get('free')}"))
        await rec.step("commission rates", lambda: tad.commission(symbol), lambda r: (True, str(r.get("standardCommission"))))
    else:
        await rec.step("account balances", lambda: _fut_bal(tad, st),
                       lambda r: (float(r.get("USDT", {}).get("available", 0)) > 50, f"USDT available {r.get('USDT', {}).get('available')}"))
        await rec.step("position mode is one-way", lambda: tad.position_mode(), lambda r: (r is False, f"dualSide={r}"))
        await rec.step("set margin type ISOLATED + leverage 2x",
                       lambda: _prep(tad, symbol), lambda r: (int(r.get("leverage", 0)) == 2, json.dumps(r)[:80]))
        await rec.step("commission rates", lambda: tad.commission(symbol), lambda r: (True, json.dumps(r)[:80]))
        ustream_task = asyncio.create_task(_user_stream(tc, user_events))

    px = Decimal(str(st["bid"]))
    min_notional = max(rules.min_notional, Decimal("10"))
    qty = rules.round_qty((min_notional * Decimal("1.5")) / px, market_order=False)
    if qty < rules.min_qty:
        qty = rules.min_qty
    far = rules.floor_price(px * Decimal("0.90"))
    cid = f"{tag}-L"

    async def place_limit():
        r = await tad.new_order(symbol=symbol, side="BUY", type="LIMIT", timeInForce="GTC", quantity=fmt(qty),
                                price=fmt(far), newClientOrderId=cid)
        st["limit"] = r
        return r
    await rec.step(f"limit order + acknowledgement (BUY {qty} @ {far}, 10% below market)", place_limit,
                   lambda r: (r.get("status") == "NEW" and r.get("clientOrderId") == cid, f"status {r.get('status')}"))
    await rec.step("order status (query by client order id)", lambda: tad.query_order(symbol, cid),
                   lambda r: (r.get("status") == "NEW", f"status {r.get('status')}, executedQty {r.get('executedQty')}"))

    @accepts_error
    def dup_rejected(e):
        return isinstance(e, BinanceError) and e.status == 400, f"rejected: {getattr(e, 'code', '')} {getattr(e, 'msg', '')}"
    await rec.step("duplicate client order id rejected", lambda: tad.new_order(
        symbol=symbol, side="BUY", type="LIMIT", timeInForce="GTC", quantity=fmt(qty), price=fmt(far), newClientOrderId=cid),
        dup_rejected)
    await rec.step("cancel order (via broker: cancel then re-query final state)",
                   lambda: _res(broker.cancel_order(market, symbol, cid)),
                   lambda r: (r["status"] == "CANCELED", f"final status {r['status']}"))

    mcid = f"{tag}-E"
    await rec.step(f"market order + fills/commission via broker (BUY {qty})",
                   lambda: _res(broker.market_order(market, symbol, "BUY", qty, mcid)),
                   lambda r: (r["status"] == "FILLED" and Decimal(r["filled_qty"]) > 0,
                              f"avg {r['avg_price']}, fee {r['fee_usdt']} USDT, net qty {r['net_base_qty']}"))
    held = None
    q = await tad.query_order(symbol, mcid)
    st["entry_avg"] = Decimal(q.get("avgPrice") or "0") or (Decimal(q.get("cummulativeQuoteQty", "0")) / Decimal(q["executedQty"]))
    if market == "spot":
        bal = await _spot_bal(tad, {})
        held = rules.round_qty(min(Decimal(bal.get(rules.base, {}).get("free", "0")), Decimal(q["executedQty"])), market_order=False)
    else:
        held = Decimal(q["executedQty"])

    pcid = f"{tag}-P"
    async def partial():
        book = await ad.depth(symbol, 5)
        best_ask, best_qty = Decimal(book["asks"][0][0]), Decimal(book["asks"][0][1])
        want = rules.round_qty(min(best_qty * 3, qty * 4), market_order=False)
        want = max(want, qty)
        r = await tad.new_order(symbol=symbol, side="BUY", type="LIMIT", timeInForce="GTC", quantity=fmt(want),
                                price=fmt(best_ask), newClientOrderId=pcid)
        st["partial_req"] = {"qty": str(want), "price": str(best_ask), "top_ask_qty": str(best_qty)}
        await asyncio.sleep(1.5)
        r2 = await tad.query_order(symbol, pcid)
        final = await _res(broker.cancel_order(market, symbol, pcid))
        return {"request": st["partial_req"], "initial": r.get("status"), "after_1.5s": r2.get("status"),
                "executed": r2.get("executedQty"), "final": final}
    await rec.step("partial-fill probe (marketable limit larger than top-of-book, remainder cancelled)", partial,
                   lambda r: (r["final"]["status"] in ("CANCELED", "FILLED"),
                              f"observed {r['after_1.5s']} executed {r['executed']} → {r['final']['status']}"), critical=False)
    if market == "spot":
        bal = await _spot_bal(tad, {})
        held = rules.round_qty(Decimal(bal.get(rules.base, {}).get("free", "0")), market_order=False)
    else:
        pr = await tad.position_risk(symbol)
        held = abs(sum(Decimal(p["positionAmt"]) for p in pr))

    pos = {"trade_id": tag, "market": market, "symbol": symbol, "direction": "LONG", "qty": str(held),
           "stop_price": str(st["entry_avg"] * Decimal("0.97")), "target_price": str(st["entry_avg"] * Decimal("1.03")),
           "opened_at": time.time() - 120, "protection": None}
    await rec.step("stop-loss + take-profit placement via broker (Spot OCO / Futures Algo)",
                   lambda: _place(broker, pos), lambda r: (bool(r.get("type")), json.dumps(r)))
    if market == "spot":
        await rec.step("protection query (order list)", lambda: tad.query_order_list(pos["_lid"]),
                       lambda r: (r.get("listOrderStatus") in ("EXECUTING", "EXEC_STARTED"), f"listOrderStatus {r.get('listOrderStatus')}"))
    else:
        await rec.step("protection query (open algo orders)", lambda: tad.open_conditionals(symbol),
                       lambda r: (len([x for x in (r if isinstance(r, list) else r.get("orders", [])) if str(x.get("clientAlgoId", "")).startswith(tag)]) == 2,
                                  f"{len(r) if isinstance(r, list) else r}"))
    await rec.step("position reconciliation via broker.check_exit (expect still open)",
                   lambda: _exit(broker, pos), lambda r: (r["closed"] is False, json.dumps(r)))
    if market == "futures":
        async def liq_cmp():
            pr = [p for p in await tad.position_risk(symbol) if Decimal(p["positionAmt"]) != 0][0]
            from .agents.sizer import liquidation_price
            ours = liquidation_price(Decimal(pr["entryPrice"]), int(pr.get("leverage") or 2), "LONG")
            return {"binance_liquidation": pr["liquidationPrice"], "ours_estimate": str(round(ours, 2)),
                    "entry": pr["entryPrice"], "leverage": pr.get("leverage"), "isolatedMargin": pr.get("isolatedMargin"),
                    "notional": pr.get("notional"), "markPrice": pr.get("markPrice")}
        await rec.step("liquidation price: Binance vs our estimate", liq_cmp,
                       lambda r: (Decimal(r["ours_estimate"]) >= Decimal(r["binance_liquidation"]) * Decimal("0.995"),
                                  f"binance {r['binance_liquidation']} vs ours {r['ours_estimate']} (ours should be ≥, i.e. conservative)"),
                       critical=False)
    await rec.step("cancel protection via broker", lambda: _cancel_prot(broker, pos), lambda r: (True, "cancelled"))
    xcid = f"{tag}-X"
    await rec.step(f"flatten: market SELL {held} via broker (reduce-only on futures)",
                   lambda: _res(broker.market_order(market, symbol, "SELL", held, xcid, reduce_only=True)),
                   lambda r: (r["status"] in ("FILLED", "PARTIALLY_FILLED"), f"avg {r['avg_price']} fee {r['fee_usdt']}"))
    if market == "futures":
        await rec.step("position reconciliation after flatten (positionAmt == 0)",
                       lambda: tad.position_risk(symbol),
                       lambda r: (sum(Decimal(p["positionAmt"]) for p in r) == 0, "flat"))
    await rec.step("trade history (my trades / user trades)",
                   lambda: (tad.my_trades(symbol, start_time=int((time.time() - 600) * 1000)) if market == "spot"
                            else tad.user_trades(symbol, start_time=int((time.time() - 600) * 1000))),
                   lambda r: (len(r) >= 2, f"{len(r)} fills in last 10 min"))
    fresh = BinanceClient(base, market, key, secret)
    fad = SpotAdapter(pub, fresh) if market == "spot" else FuturesAdapter(pub, fresh)
    await rec.step("restart recovery: new client finds entry order by client order id",
                   lambda: fad.query_order(symbol, mcid), lambda r: (r.get("clientOrderId") == mcid, f"status {r.get('status')}"))
    await fresh.close()
    if market == "futures":
        await asyncio.sleep(2)
        if ustream_task:
            ustream_task.cancel()
        await rec.step("user-data stream delivered ORDER_TRADE_UPDATE events",
                       lambda: _const({"events": user_events[:10]}),
                       lambda r: (any(e.get("e") == "ORDER_TRADE_UPDATE" for e in r["events"]), f"{len(user_events)} events"))
    await tc.close()
    await pub.close()
    return rec.steps


async def _const(v):
    return v


async def _offset(c: BinanceClient):
    return await c.sync_time()


async def _res(coro):
    r = await coro
    return {"status": r.status, "filled_qty": str(r.filled_qty), "avg_price": str(r.avg_price), "fee_usdt": str(r.fee_usdt),
            "net_base_qty": str(r.net_base_qty), "exchange_id": r.exchange_id, "error": r.error, "raw": _scrub(r.raw)}


async def _spot_bal(ad, st):
    a = await ad.account()
    return {b["asset"]: {"free": b["free"], "locked": b["locked"]} for b in a["balances"]
            if b["asset"] in ("USDT", "BTC", "ETH", "BNB")}


async def _fut_bal(ad, st):
    return {b["asset"]: {"balance": b["balance"], "available": b["availableBalance"]} for b in await ad.balance()
            if b["asset"] in ("USDT", "USDC", "BNB")}


async def _prep(ad, symbol):
    await ad.set_margin_type(symbol, "ISOLATED")
    return await ad.set_leverage(symbol, 2)


async def _place(broker, pos):
    info = await broker.place_protection(pos)
    pos["protection"] = json.dumps(info)
    pos["_lid"] = info.get("list_client_id")
    return info


async def _cancel_prot(broker, pos):
    await broker.cancel_protection(pos)
    return {"ok": True}


async def _exit(broker, pos):
    e = await broker.check_exit(pos)
    return {"closed": e.closed, "reason": e.reason}


async def _user_stream(client, sink):
    import websockets
    lk = (await client.request("POST", "/fapi/v1/listenKey", api_key_only=True))["listenKey"]
    async with websockets.connect(f"{FUT_WS}/ws/{lk}") as ws:
        while True:
            sink.append(json.loads(await ws.recv()))


def render(steps: list[dict], meta: dict) -> str:
    L = ["# Binance testnet validation report", "",
         f"Run {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(meta['ts']))} · symbol {meta['symbol']} · "
         f"host: {meta['host']}", "",
         "Only Binance TEST environments are contacted (testnet.binance.vision, testnet.binancefuture.com). "
         "Responses below are the exchange's actual replies (signatures/keys removed).", ""]
    for m in ("spot", "futures"):
        ss = [s for s in steps if s["market"] == m]
        if not ss:
            continue
        cnt = {k: sum(1 for s in ss if s["status"] == k) for k in ("PASS", "FAIL", "SKIPPED")}
        L += [f"## {'Spot' if m == 'spot' else 'USDⓈ-M Futures'} — {cnt['PASS']} pass · {cnt['FAIL']} fail · {cnt['SKIPPED']} skipped", "",
              "| # | Step | Result | Note | ms |", "|---:|---|:-:|---|---:|"]
        for i, s in enumerate(ss, 1):
            L.append(f"| {i} | {s['step']} | {s['status']} | {str(s.get('note', '')).replace('|', '/')[:160]} | {s.get('ms', '')} |")
        L += ["", "<details><summary>Exchange responses</summary>", ""]
        for i, s in enumerate(ss, 1):
            if "response" in s:
                L += [f"**{i}. {s['step']}**", "```json", json.dumps(s["response"], indent=1, default=str)[:2500], "```"]
        L += ["</details>", ""]
    return "\n".join(L)


async def amain(a) -> int:
    steps: list[dict] = []
    for m in (["spot", "futures"] if a.market == "both" else [a.market]):
        key = os.environ.get(f"GROK_TESTNET_{m.upper()}_KEY")
        sec = os.environ.get(f"GROK_TESTNET_{m.upper()}_SECRET")
        steps += await run_market(m, a.symbol, key, sec)
    meta = {"ts": time.time(), "symbol": a.symbol, "host": os.uname().nodename}
    root = Path(__file__).resolve().parents[2]
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "testnet_report.md").write_text(render(steps, meta))
    out = Path(os.environ.get("GROK_DATA_DIR", root / "data"))
    out.mkdir(parents=True, exist_ok=True)
    (out / "testnet_report.json").write_text(json.dumps({"meta": meta, "steps": steps}, default=str, indent=1))
    fails = [s for s in steps if s["status"] == "FAIL" and s.get("critical", True)]
    print(f"\n{len(steps)} steps · {len(fails)} critical failures · report: docs/testnet_report.md")
    return 1 if fails else 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["spot", "futures", "both"], default="both")
    ap.add_argument("--symbol", default="BTCUSDT")
    a = ap.parse_args()
    for h in (SPOT, FUT):
        assert "testnet" in h, "harness may only target testnet hosts"
    raise SystemExit(asyncio.run(amain(a)))


if __name__ == "__main__":
    main()
