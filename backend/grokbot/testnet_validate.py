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
TRANSPORT = None  # tests inject a mock transport here


def _client(base, market, key=None, secret=None):
    return BinanceClient(base, market, key, secret, transport=TRANSPORT)


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
    pub = _client(base, market)
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
        dummy = _client(base, market, "x" * 64, "y" * 64)
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
    tc = _client(base, market, key, secret)
    tad = SpotAdapter(pub, tc) if market == "spot" else FuturesAdapter(pub, tc)
    md = MarketData(SpotAdapter(pub), FuturesAdapter(pub)) if market == "spot" else MarketData(SpotAdapter(_client(SPOT, "spot")), FuturesAdapter(pub), True)
    md.rules[market][symbol] = st["rules"]
    broker = ExchangeBroker("testnet", tad if market == "spot" else SpotAdapter(_client(SPOT, "spot")),
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
    fresh = _client(base, market, key, secret)
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
    for m in ("spot", "futures", "spot-pipeline", "futures-pipeline"):
        ss = [s for s in steps if s["market"] == m]
        if not ss:
            continue
        cnt = {k: sum(1 for s in ss if s["status"] == k) for k in ("PASS", "FAIL", "SKIPPED")}
        title = {"spot": "Spot", "futures": "USDⓈ-M Futures", "spot-pipeline": "Spot: full application pipeline",
                 "futures-pipeline": "USDⓈ-M Futures: full application pipeline"}[m]
        L += [f"## {title} — {cnt['PASS']} pass · {cnt['FAIL']} fail · {cnt['SKIPPED']} skipped", "",
              "| # | Step | Result | Note | ms |", "|---:|---|:-:|---|---:|"]
        for i, s in enumerate(ss, 1):
            L.append(f"| {i} | {s['step']} | {s['status']} | {str(s.get('note', '')).replace('|', '/')[:160]} | {s.get('ms', '')} |")
        L += ["", "<details><summary>Exchange responses</summary>", ""]
        for i, s in enumerate(ss, 1):
            if "response" in s:
                L += [f"**{i}. {s['step']}**", "```json", json.dumps(s["response"], indent=1, default=str)[:2500], "```"]
        L += ["</details>", ""]
    return "\n".join(L)


DEFAULT_LEVELS = {"spot": (0.45, 1.35), "futures": (0.35, 0.90)}   # stop %, target % for validation trades


async def run_pipeline(market: str, symbol: str, key: str | None, secret: str | None, *, direction: str = "LONG",
                       wait_minutes: float = 20.0, cleanup: str = "close_all", stop_pct: float | None = None,
                       target_pct: float | None = None, db_dir: str | None = None) -> tuple[list[dict], dict | None]:
    """Full application lifecycle on the TESTNET with a clearly-labelled
    operator VALIDATION trade (not a strategy signal):

      market data -> signal -> Rug Check -> Sizer -> central Risk -> order
      submission -> fill -> exchange-side stop-loss/take-profit (verified on
      the exchange) -> wait for the EXCHANGE to trigger SL or TP -> exit ->
      reconciliation (system vs exchange) -> equity update -> end-of-test
      cleanup -> independent audit.

    Nothing here bypasses a control: if Rug Check, the Sizer or the risk
    engine rejects the trade, that is reported and the lifecycle stops.
    A stop-loss is never assumed to fill at its level: the actual exit fill
    and its distance from the level are reported."""
    rec = Recorder(f"{market}-pipeline")
    if not key or not secret:
        rec.skip("application lifecycle", f"no testnet credentials (GROK_TESTNET_{market.upper()}_KEY / _SECRET)")
        return rec.steps, None
    if os.environ.get("GROK_PROFILE") != "testnet":
        rec.skip("application lifecycle", "set GROK_PROFILE=testnet so all market data comes from the testnet")
        return rec.steps, None
    from . import audit, exec_metrics, session_end
    from .engine import Engine
    from .settings import load_settings
    from .strategy.strategies import Signal
    settings = load_settings()
    if db_dir:
        from dataclasses import replace
        from pathlib import Path as _P
        _P(db_dir).mkdir(parents=True, exist_ok=True)
        settings = replace(settings, data_dir=_P(db_dir), db_path=_P(db_dir) / "grokbot.db")
    e = Engine(settings, transport=TRANSPORT)
    st: dict = {}
    sp, tp = stop_pct or DEFAULT_LEVELS[market][0], target_pct or DEFAULT_LEVELS[market][1]
    try:
        e.vault.store_credentials(market, "testnet", key, secret)
        e.reset_clients()
        e.cfg.update("scanner", {"spot_symbols": [symbol], "futures_symbols": [symbol]}, "testnet-validation")
        if e.state.mode != "testnet":
            e.state.set_mode("testnet", "testnet-validation")
        await e.orch.start()           # market data (+streams), monitor loop, recovery; scanner stays STOPPED
        await asyncio.sleep(3)
        e.pf.ensure_initialised("testnet")
        before = e.pf.summary("testnet")
        snaps_before = int(e.db.scalar("SELECT COUNT(*) FROM equity_snapshots WHERE mode='testnet'") or 0)

        async def mdata():
            await (e.md.refresh_spot() if market == "spot" else e.md.refresh_futures(True))
            t = e.md.ticker(market, symbol)
            st["mid"] = (t.bid + t.ask) / 2
            return {"bid": t.bid, "ask": t.ask, "mark": t.mark or None, "age_s": round(e.md.age(market, symbol), 2),
                    "source": e.md.adapters[market].data.base_url}
        r = await rec.step("1. market data (testnet book)", mdata,
                           lambda r: ("testnet" in r["source"] and r["age_s"] < 20, f"bid {r['bid']} ask {r['ask']} from {r['source']}"))
        if r["status"] != "PASS":
            return rec.steps, None
        k = 1 if direction == "LONG" else -1
        mid = st["mid"]
        sig = Signal("validation", direction, mid, mid * (1 - k * sp / 100), mid * (1 + k * tp / 100), 1.0,
                     [f"operator testnet validation trade: stop {sp}%, target {tp}% (not a strategy signal)"], {})
        cand = {"market": market, "symbol": symbol, "timeframe": "1m", "candle_open": int(time.time()), "signal": sig}
        await rec.step("2. signal created (operator validation, labelled)", lambda: _const(sig.as_dict()),
                       lambda r: (True, f"{direction} entry {mid:.6g} SL {sig.stop:.6g} TP {sig.target:.6g}"))

        async def go():
            tid = e.orch.create_trade("testnet", cand)
            st["tid"] = tid
            await e.orch.process(tid)
            return e.orch.trade(tid)
        tr = (await rec.step("3-7. Rug Check → Sizer → Risk → order submission → fill", go,
                             lambda t: (t["stage"] in ("OPEN", "WORKING"), f"stage {t['stage']} {t['reject_reason'] or ''}")))
        t = e.orch.trade(st["tid"])
        stages = [("3. Rug Check", t["rugcheck"]), ("4. Sizer", t["sizing"]), ("5. Central risk", t["risk"])]
        for name, blob in stages:
            if blob is None:
                rec.skip(name, "not reached")
                continue
            failed = [c["name"] for c in blob.get("checks", []) if not c["ok"]]
            rec.steps.append({"market": rec.market, "step": name, "status": "PASS" if blob.get("approved") else "FAIL",
                              "note": "approved" if blob.get("approved") else f"rejected: {blob.get('reason') or failed}",
                              "response": blob, "ts": time.time()})
        if t["stage"] not in ("OPEN", "WORKING"):
            rec.skip("6-12. order lifecycle", f"trade not executed ({t['stage']}): controls rejected it; nothing was bypassed")
        else:
            o = e.db.one("SELECT * FROM orders WHERE trade_id=? AND purpose='entry' ORDER BY created_at LIMIT 1", (st["tid"],))
            rec.steps.append({"market": rec.market, "step": "6. order acknowledged + filled by the exchange",
                              "status": "PASS" if o and o["status"] in ("FILLED", "PARTIALLY_FILLED") else "FAIL",
                              "note": f"{o['client_id']} {o['type']} {o['status']} filled {o['filled_qty']} @ {o['avg_price']} "
                                      f"fee {o['fee_usdt']} USDT", "response": {k2: o[k2] for k2 in o.keys() if k2 != "raw"},
                              "ts": time.time()})
            pos = e.db.one("SELECT * FROM positions WHERE trade_id=?", (st["tid"],))
            await rec.step("7. exchange-side SL/TP present and valid (queried from the exchange)",
                           lambda: e.executor.verify_protection(pos, e.broker("testnet")),
                           lambda r: (bool(r.get("ok")), r.get("detail", "")))
            deadline = time.time() + wait_minutes * 60

            async def wait_exit():
                while time.time() < deadline:
                    p2 = e.db.one("SELECT status, exit_reason FROM positions WHERE trade_id=?", (st["tid"],))
                    if p2["status"] == "CLOSED":
                        return {"closed": True, "reason": p2["exit_reason"], "waited_s": round(wait_minutes * 60 - (deadline - time.time()))}
                    await asyncio.sleep(5)
                return {"closed": False, "reason": None, "waited_s": round(wait_minutes * 60)}
            w = await rec.step(f"8. wait ≤{wait_minutes:g} min for the EXCHANGE to trigger SL or TP", wait_exit,
                               lambda r: (True, f"exchange closed it: {r['reason']}" if r["closed"] else
                                          "neither level reached in the window (not a failure; cleanup follows)"),
                               critical=False)
            p3 = e.db.one("SELECT * FROM positions WHERE trade_id=?", (st["tid"],))
            if p3["status"] == "CLOSED":
                meta = json.loads(p3["exit_meta"] or "{}")
                rec.steps.append({"market": rec.market, "step": "9. exit fill vs protective level",
                                  "status": "PASS", "note": f"{p3['exit_reason']} level {meta.get('level')} fill "
                                  f"{p3['exit_price']} ({meta.get('slippage_vs_level_bps', 0):+.1f} bps; a stop-loss does "
                                  "NOT guarantee its price)", "response": meta, "ts": time.time()})
        rep = await session_end.finalize(e, "testnet", cleanup, "test")
        st["session_end"] = rep
        clean = not rep["after"]["positions"] and not rep["after"]["pending_orders"] and not rep["after"]["working_entries"]
        exa = rep.get("exchange_after") or {}
        ex_flat = not [p for p in exa.get("futures_positions", []) if p["symbol"] == symbol] and \
            not [o for o in exa.get("futures_open_algo", []) + exa.get("futures_open_orders", []) + exa.get("spot_open_orders", [])
                 if o.get("symbol") == symbol]
        rec.steps.append({"market": rec.market, "step": f"10. reconciliation + end-of-test cleanup ({cleanup})",
                          "status": "PASS" if (clean and ex_flat) or cleanup == "report" else "FAIL",
                          "note": rep["statement"] + ("" if not rep["reconciliation"] else " · ISSUES: " + "; ".join(rep["reconciliation"])),
                          "response": {k2: rep[k2] for k2 in ("actions", "reconciliation", "protection", "statement")},
                          "ts": time.time()})
        after = e.pf.summary("testnet")
        snaps_after = int(e.db.scalar("SELECT COUNT(*) FROM equity_snapshots WHERE mode='testnet'") or 0)
        rec.steps.append({"market": rec.market, "step": "11. equity update",
                          "status": "PASS" if snaps_after > snaps_before else "FAIL",
                          "note": f"equity {before['total_equity']:.4f} → {after['total_equity']:.4f} USDT, realized "
                                  f"{after['realized_pnl'] - before['realized_pnl']:+.6f}, fees {after['fees_paid'] - before['fees_paid']:.6f}, "
                                  f"{snaps_after - snaps_before} new equity snapshot(s)",
                          "response": {"before": before["total_equity"], "after": after["total_equity"]}, "ts": time.time()})
        bad = [c for c in audit.run(e.db, "testnet")[0] if not c["ok"]]
        rec.steps.append({"market": rec.market, "step": "12. independent audit of testnet books",
                          "status": "PASS" if not bad else "FAIL", "note": f"{len(bad)} mismatches", "response": bad,
                          "ts": time.time()})
        return rec.steps, exec_metrics.compute(e.db, "testnet")
    finally:
        await e.close()


async def account_state(key_s, sec_s, key_f, sec_f) -> dict:
    """What the TEST accounts hold right now, straight from the exchange."""
    out: dict = {"queried_at": time.time()}
    if key_f and sec_f:
        c = _client(FUT, "futures", key_f, sec_f)
        fa = FuturesAdapter(_client(FUT, "futures"), c)
        try:
            out["futures_positions"] = [{k: p.get(k) for k in ("symbol", "positionAmt", "entryPrice", "unRealizedProfit")}
                                        for p in await fa.position_risk() if Decimal(p.get("positionAmt", "0")) != 0]
            out["futures_open_orders"] = [{k: o.get(k) for k in ("symbol", "clientOrderId", "type", "status")}
                                          for o in await fa.open_orders()]
            algo = await fa.open_conditionals()
            out["futures_open_algo_orders"] = [{k: o.get(k) for k in ("symbol", "clientAlgoId", "orderType", "type")}
                                               for o in (algo if isinstance(algo, list) else algo.get("orders", []))]
        except Exception as ex:  # noqa: BLE001
            out["futures_error"] = str(ex)
        await c.close()
    if key_s and sec_s:
        c = _client(SPOT, "spot", key_s, sec_s)
        sa = SpotAdapter(_client(SPOT, "spot"), c)
        try:
            out["spot_open_orders"] = [{k: o.get(k) for k in ("symbol", "clientOrderId", "type", "status")}
                                       for o in await sa.open_orders()]
        except Exception as ex:  # noqa: BLE001
            out["spot_error"] = str(ex)
        await c.close()
    n_pos = len(out.get("futures_positions", []))
    n_ord = sum(len(out.get(k, [])) for k in ("futures_open_orders", "futures_open_algo_orders", "spot_open_orders"))
    queried = [m for m, kk in (("futures", key_f), ("spot", key_s)) if kk]
    out["statement"] = (f"Test account(s) queried ({', '.join(queried) or 'none: no credentials'}): "
                        f"{n_pos} open futures position(s), {n_ord} open order(s) incl. algo orders."
                        + (" Spot holdings are balances, not positions; see the pipeline cleanup step." if key_s else ""))
    return out


def preflight() -> int:
    """Check prerequisites without placing anything. Exit code 0 = ready."""
    import importlib
    import re
    import sys
    import urllib.request
    rows = []

    def chk(name, ok, detail):
        rows.append((name, ok, detail))
    chk("python >= 3.11", sys.version_info >= (3, 11), sys.version.split()[0])
    for mod in ("fastapi", "httpx", "numpy", "cryptography", "websockets", "pydantic"):
        try:
            importlib.import_module(mod)
            chk(f"module {mod}", True, "installed")
        except ImportError:
            chk(f"module {mod}", False, "pip install -r requirements.txt")
    chk("GROK_PROFILE=testnet (needed for --pipeline)", os.environ.get("GROK_PROFILE") == "testnet",
        os.environ.get("GROK_PROFILE") or "not set")
    for m in ("SPOT", "FUTURES"):
        k, s_ = os.environ.get(f"GROK_TESTNET_{m}_KEY", ""), os.environ.get(f"GROK_TESTNET_{m}_SECRET", "")
        fmt_ok = bool(re.fullmatch(r"[A-Za-z0-9]{16,256}", k)) and bool(re.fullmatch(r"[A-Za-z0-9]{16,256}", s_))
        chk(f"GROK_TESTNET_{m}_KEY/_SECRET", fmt_ok, "present, format OK" if fmt_ok else "missing or malformed")
    for name, url in (("spot testnet reachable", f"{SPOT}/api/v3/ping"), ("futures testnet reachable", f"{FUT}/fapi/v1/ping")):
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                chk(name, r.status == 200, f"HTTP {r.status}")
        except Exception as ex:  # noqa: BLE001
            chk(name, False, str(ex)[:80])
    for k in ("GROK_ALLOW_LIVE",):
        chk("live trading NOT enabled in this shell", os.environ.get(k) != "1", f"{k}={os.environ.get(k, '0')}")
    for name, ok, detail in rows:
        print(f"{'OK  ' if ok else 'FAIL'}  {name}: {detail}")
    bad = [r for r in rows if not r[1]]
    print(f"\n{len(rows) - len(bad)}/{len(rows)} prerequisites met" + ("" if not bad else " — fix the FAIL lines first"))
    return 1 if bad else 0


async def amain(a) -> int:
    from . import exec_metrics
    steps: list[dict] = []
    metrics: dict[str, dict] = {}
    creds = {m: (os.environ.get(f"GROK_TESTNET_{m.upper()}_KEY"), os.environ.get(f"GROK_TESTNET_{m.upper()}_SECRET"))
             for m in ("spot", "futures")}
    for m in (["spot", "futures"] if a.market == "both" else [a.market]):
        key, sec = creds[m]
        steps += await run_market(m, a.symbol, key, sec)
        if a.pipeline:
            ps, mt = await run_pipeline(m, a.symbol, key, sec, direction="LONG" if m == "spot" else a.direction,
                                        wait_minutes=a.wait_minutes,
                                        cleanup=a.cleanup, db_dir=os.path.join(os.environ.get("GROK_DATA_DIR", "data"),
                                                                               f"testnet-pipeline-{m}"))
            steps += ps
            if mt:
                metrics[m] = mt
    acct = await account_state(*creds["spot"], *creds["futures"]) if any(k for k, _ in creds.values()) else \
        {"statement": "No testnet credentials supplied: no test account was queried and no order was placed."}
    meta = {"ts": time.time(), "symbol": a.symbol, "host": os.uname().nodename, "pipeline": a.pipeline,
            "cleanup": a.cleanup, "wait_minutes": a.wait_minutes}
    root = Path(__file__).resolve().parents[2]
    (root / "docs").mkdir(exist_ok=True)
    md = render(steps, meta)
    md += "\n## Exchange execution metrics (testnet application pipeline)\n\n"
    md += ("\n".join(exec_metrics.render(v, f"{k} pipeline") for k, v in metrics.items()) if metrics else
           "_Not available: the application pipeline did not run (no credentials or GROK_PROFILE not testnet)._\n")
    md += f"\n## Final test-account state\n\n**{acct['statement']}**\n\n```json\n{json.dumps(acct, indent=1, default=str)[:3000]}\n```\n"
    (root / "docs" / "testnet_report.md").write_text(md)
    out = Path(os.environ.get("GROK_DATA_DIR", root / "data"))
    out.mkdir(parents=True, exist_ok=True)
    (out / "testnet_report.json").write_text(json.dumps({"meta": meta, "steps": steps, "metrics": metrics,
                                                         "account": acct}, default=str, indent=1))
    fails = [s for s in steps if s["status"] == "FAIL" and s.get("critical", True)]
    print(f"\n{acct['statement']}")
    print(f"{len(steps)} steps · {len(fails)} critical failures · report: docs/testnet_report.md")
    return 1 if fails else 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Binance TESTNET validation (never contacts production).")
    ap.add_argument("--market", choices=["spot", "futures", "both"], default="both")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--pipeline", action="store_true",
                    help="also run the full application lifecycle with an operator validation trade (needs GROK_PROFILE=testnet)")
    ap.add_argument("--direction", choices=["LONG", "SHORT"], default="LONG",
                    help="validation trade direction (spot is always LONG)")
    ap.add_argument("--wait-minutes", type=float, default=20.0, help="how long to wait for the exchange to trigger SL/TP")
    ap.add_argument("--cleanup", choices=["close_all", "cancel_pending", "report"], default="close_all",
                    help="end-of-test policy (positions are never closed while the exchange is unreachable)")
    ap.add_argument("--check", action="store_true", help="only verify prerequisites; place nothing")
    a = ap.parse_args()
    for h in (SPOT, FUT):
        assert "testnet" in h, "harness may only target testnet hosts"
    if a.check:
        raise SystemExit(preflight())
    if a.market == "spot":
        a.direction = "LONG"
    raise SystemExit(asyncio.run(amain(a)))


if __name__ == "__main__":
    main()
