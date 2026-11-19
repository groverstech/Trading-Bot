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
    from .endpoints import require
    require(base, "testnet", "harness endpoint")       # the harness never talks to production
    return BinanceClient(base, market, key, secret, transport=TRANSPORT, env="testnet" if key else None)


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


BASELINES: dict[str, dict] = {}
RUN: dict = {"steps": [], "metrics": {}, "interrupted": False, "guard": {}}


def _uid(prefix: str) -> str:
    import secrets
    return f"{prefix}{int(time.time() * 1000)}{secrets.token_hex(2)}"[:36]


def _data_dir() -> Path:
    d = Path(os.environ.get("GROK_DATA_DIR", Path(__file__).resolve().parents[2] / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_baselines() -> None:
    (_data_dir() / "testnet_baseline.json").write_text(json.dumps(BASELINES), encoding="utf-8")


async def emergency_cleanup(market: str, symbol: str, key: str, secret: str, base0: str) -> dict:
    """Bring the TEST account back to its pre-run state for `symbol`: cancel open
    (algo) orders and flatten only exposure created by the run. Verifies the
    result. Used after normal completion AND after any failure/interruption."""
    base = SPOT if market == "spot" else FUT
    pub, tc = _client(base, market), _client(base, market, key, secret)
    ad = SpotAdapter(pub, tc) if market == "spot" else FuturesAdapter(pub, tc)
    out: dict = {"market": market, "symbol": symbol, "actions": []}
    try:
        if market == "futures":
            algo = await ad.open_conditionals(symbol)
            for o in (algo if isinstance(algo, list) else algo.get("orders", [])):
                await ad.cancel_conditional(o["clientAlgoId"])
                out["actions"].append(f"cancelled algo order {o['clientAlgoId']}")
            for o in await ad.open_orders(symbol):
                await ad.cancel_order(symbol, o["clientOrderId"])
                out["actions"].append(f"cancelled order {o['clientOrderId']}")
            amt = sum((Decimal(p["positionAmt"]) for p in await ad.position_risk(symbol)), Decimal(0))
            if amt != 0:
                r = await ad.new_order(symbol=symbol, side="SELL" if amt > 0 else "BUY", type="MARKET",
                                       quantity=fmt(abs(amt)), reduceOnly="true",
                                       newClientOrderId=_uid("GBVX"))
                out["actions"].append(f"flattened {amt} ({r.get('status')})")
            amt = sum((Decimal(p["positionAmt"]) for p in await ad.position_risk(symbol)), Decimal(0))
            left = await ad.open_orders(symbol)
            algo = await ad.open_conditionals(symbol)
            algo = algo if isinstance(algo, list) else algo.get("orders", [])
            out.update(position_amt=str(amt), open_orders=len(left), open_algo_orders=len(algo),
                       clean=amt == 0 and not left and not algo)
        else:
            for o in await ad.open_orders(symbol):     # cancelling an OCO leg cancels the whole list
                try:
                    await ad.cancel_order(symbol, o["clientOrderId"])
                    out["actions"].append(f"cancelled order {o['clientOrderId']}")
                except BinanceError:
                    pass
            rules = (await ad.exchange_info([symbol]))[symbol]
            acct = await ad.account()
            b = next((x for x in acct["balances"] if x["asset"] == rules.base), {"free": "0", "locked": "0"})
            delta = rules.round_qty(Decimal(b["free"]) + Decimal(b["locked"]) - Decimal(base0), market_order=False)
            px = Decimal((await ad.book_ticker([symbol]))[0]["bidPrice"])
            if delta > 0 and delta * px >= rules.min_notional:
                r = await ad.new_order(symbol=symbol, side="SELL", type="MARKET", quantity=fmt(delta),
                                       newClientOrderId=_uid("GBVX"))
                out["actions"].append(f"sold run-created {delta} {rules.base} ({r.get('status')})")
                acct = await ad.account()
                b = next((x for x in acct["balances"] if x["asset"] == rules.base), {"free": "0", "locked": "0"})
                delta = rules.round_qty(Decimal(b["free"]) + Decimal(b["locked"]) - Decimal(base0), market_order=False)
            left = await ad.open_orders(symbol)
            out.update(residual_base=str(delta), open_orders=len(left),
                       clean=not left and (delta <= 0 or delta * px < rules.min_notional),
                       note="spot residual below the exchange minimum (dust) cannot be sold" if 0 < delta * px < rules.min_notional else "")
    except Exception as ex:  # noqa: BLE001
        out.update(clean=False, error=f"{type(ex).__name__}: {ex}")
    finally:
        await tc.close()
        await pub.close()
    return out


async def guard() -> dict:
    """Runs after every harness run, however it ended."""
    res = {}
    for m, b in BASELINES.items():
        key, sec = os.environ.get(f"GROK_TESTNET_{m.upper()}_KEY"), os.environ.get(f"GROK_TESTNET_{m.upper()}_SECRET")
        if not key:
            continue
        res[m] = await emergency_cleanup(m, b["symbol"], key, sec, b.get("base0", "0"))
    marker = _data_dir() / "LEFTOVER_EXPOSURE.json"
    if any(not r.get("clean") for r in res.values()):
        marker.write_text(json.dumps(res, default=str, indent=1), encoding="utf-8")
    elif marker.exists():
        marker.unlink()
    return res


async def run_market(market: str, symbol: str, key: str | None, secret: str | None,
                     trigger_wait: float = 10.0) -> list[dict]:
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

    # ---- baseline + refusal to touch pre-existing exposure ----
    async def baseline():
        out = {}
        if market == "spot":
            bal = await _spot_bal(tad, {}, extra=(st["rules"].base,))
            b = bal.get(st["rules"].base, {})
            st["base0"] = Decimal(b.get("free", "0")) + Decimal(b.get("locked", "0"))
            oo = [o for o in await tad.open_orders(symbol)]
            out = {"base_asset": st["rules"].base, "base_balance_at_start": str(st["base0"]), "open_orders": len(oo)}
            out["clean"] = not oo
        else:
            pr = await tad.position_risk(symbol)
            amt = sum((Decimal(p["positionAmt"]) for p in pr), Decimal(0))
            oo = await tad.open_orders(symbol)
            algo = await tad.open_conditionals(symbol)
            algo = algo if isinstance(algo, list) else algo.get("orders", [])
            out = {"position_amt": str(amt), "open_orders": len(oo), "open_algo_orders": len(algo)}
            out["clean"] = amt == 0 and not oo and not algo
        BASELINES[market] = {"symbol": symbol, "base0": str(st.get("base0", "0")), "tag": tag}
        _save_baselines()
        return out
    r0 = await rec.step(f"baseline: no pre-existing {symbol} position / open orders (the harness never touches existing exposure)",
                        baseline, lambda r: (r["clean"], json.dumps(r)))
    if r0["status"] != "PASS":
        rec.skip("order lifecycle steps", f"pre-existing exposure on {symbol}: refusing to trade it (close it on the "
                 "testnet UI or use another --symbol)")
        await tc.close()
        await pub.close()
        return rec.steps
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
    base_asset = rules.base

    async def held_delta() -> Decimal:
        """Exposure created by THIS run only (spot: base balance above the start baseline)."""
        if market == "spot":
            bal = await _spot_bal(tad, {}, extra=(base_asset,))
            b = bal.get(base_asset, {})
            cur = Decimal(b.get("free", "0")) + Decimal(b.get("locked", "0"))
            return rules.round_qty(max(Decimal(0), cur - st["base0"]), market_order=False)
        pr = await tad.position_risk(symbol)
        return sum((Decimal(p["positionAmt"]) for p in pr), Decimal(0))

    async def flatten(note: str) -> dict:
        """Close only what this run created; never sells pre-existing holdings."""
        if market == "spot":
            q = await held_delta()
            if q > 0 and q * px >= rules.min_notional:
                return await _res(broker.market_order(market, symbol, "SELL", q, _uid(f"{tag}-F")))
            return {"status": "NOTHING_TO_FLATTEN", "note": note, "residual": str(q)}
        amt = await held_delta()
        if amt != 0:
            return await _res(broker.market_order(market, symbol, "SELL" if amt > 0 else "BUY", abs(amt),
                                                  _uid(f"{tag}-F"), reduce_only=True))
        return {"status": "NOTHING_TO_FLATTEN", "note": note}

    pcid = f"{tag}-P"

    async def partial():
        book = await ad.depth(symbol, 5)
        best_ask, best_qty = Decimal(book["asks"][0][0]), Decimal(book["asks"][0][1])
        want = max(rules.round_qty(min(best_qty * 3, qty * 4), market_order=False), qty)
        r = await tad.new_order(symbol=symbol, side="BUY", type="LIMIT", timeInForce="GTC", quantity=fmt(want),
                                price=fmt(best_ask), newClientOrderId=pcid)
        await asyncio.sleep(1.5)
        r2 = await tad.query_order(symbol, pcid)
        final = await _res(broker.cancel_order(market, symbol, pcid))
        fl = await flatten("partial-fill probe")       # self-contained: nothing left behind, unprotected or not
        return {"request": {"qty": str(want), "price": str(best_ask), "top_ask_qty": str(best_qty)},
                "initial": r.get("status"), "after_1.5s": r2.get("status"), "executed": r2.get("executedQty"),
                "final": final, "flattened": fl}
    await rec.step("partial-fill probe (marketable limit larger than top-of-book; remainder cancelled; fill flattened)",
                   partial, lambda r: (r["final"]["status"] in ("CANCELED", "FILLED"),
                                       f"observed {r['after_1.5s']} executed {r['executed']} → {r['final']['status']}; "
                                       f"flatten {r['flattened']['status']}"), critical=False)

    pos = {"trade_id": tag, "market": market, "symbol": symbol, "direction": "LONG", "qty": "0",
           "stop_price": "0", "target_price": "0", "opened_at": time.time() - 120, "protection": None}

    async def enter_protected():
        """Market entry and exchange-side protection in ONE step: no unprotected window
        beyond the round-trip of the protection request; if protection fails, flatten."""
        r = await _res(broker.market_order(market, symbol, "BUY", qty, mcid))
        q = await tad.query_order(symbol, mcid)
        st["entry_avg"] = Decimal(q.get("avgPrice") or "0") or (Decimal(q.get("cummulativeQuoteQty", "0")) / Decimal(q["executedQty"]))
        held = await held_delta()
        pos.update(qty=str(abs(held)), stop_price=str(st["entry_avg"] * Decimal("0.97")),
                   target_price=str(st["entry_avg"] * Decimal("1.03")))
        try:
            prot = await _place(broker, pos)
        except Exception as ex:  # noqa: BLE001
            fl = await flatten("protection failed")
            raise RuntimeError(f"protection placement failed ({ex}); position flattened: {fl.get('status')}") from ex
        return {**r, "held": str(held), "protection": prot}
    await rec.step(f"market order + fill + IMMEDIATE exchange-side SL/TP (BUY {qty})", enter_protected,
                   lambda r: (r["status"] == "FILLED" and bool(r["protection"].get("type")),
                              f"avg {r['avg_price']}, fee {r['fee_usdt']} USDT, held {r['held']}, protection {r['protection'].get('type')}"))
    if market == "spot":
        await rec.step("protection query (order list)", lambda: tad.query_order_list(pos.get("_lid") or "-"),
                       lambda r: (r.get("listOrderStatus") in ("EXECUTING", "EXEC_STARTED"), f"listOrderStatus {r.get('listOrderStatus')}"))
    else:
        await rec.step("protection query (open algo orders)", lambda: tad.open_conditionals(symbol),
                       lambda r: (len([x for x in (r if isinstance(r, list) else r.get("orders", [])) if str(x.get("clientAlgoId", "")).startswith(tag)]) == 2,
                                  f"{len(r) if isinstance(r, list) else r}"))
    await rec.step("protection verified by broker.verify_protection", lambda: broker.verify_protection(pos),
                   lambda r: (bool(r.get("ok")), r.get("detail", "")))
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

    async def exit_and_unprotect():
        if market == "futures":           # flatten FIRST (closePosition orders keep protecting until then)
            fl = await flatten("planned exit")
            await broker.cancel_protection(pos)
        else:                             # spot OCO locks the balance: cancel, then sell immediately
            await broker.cancel_protection(pos)
            fl = await flatten("planned exit")
        return fl
    await rec.step("planned exit: flatten + remove protection (no unprotected interval on futures)", exit_and_unprotect,
                   lambda r: (r["status"] in ("FILLED", "PARTIALLY_FILLED"), f"avg {r.get('avg_price')} fee {r.get('fee_usdt')}"))
    if market == "futures":
        await rec.step("position reconciliation after flatten (positionAmt == 0)",
                       lambda: tad.position_risk(symbol),
                       lambda r: (sum(Decimal(p["positionAmt"]) for p in r) == 0, "flat"))

    # ---- exchange-triggered protection: one TAKE-PROFIT test and one STOP-LOSS test ----
    for kind in ("take_profit", "stop_loss"):
        await rec.step(f"EXCHANGE-TRIGGERED {kind.replace('_', '-').upper()}: tight level, wait ≤{trigger_wait:g} min "
                       "for the exchange to execute it", lambda kind=kind: _trigger_test(
                           kind, market, symbol, tad, ad, broker, rules, qty, tag, held_delta, flatten, trigger_wait),
                       lambda r: (r["result"] in ("TRIGGERED", "NOT_TRIGGERED") and r["flat_after"],
                                  r["summary"]), critical=False)
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


async def _trigger_test(kind, market, symbol, tad, ad, broker, rules, qty, tag, held_delta, flatten, wait_min):
    """Open a minimum LONG, protect it with one level a few bps from the market
    (TP test: target close / stop far; SL test: stop close / target far) and let
    the EXCHANGE execute it. Detect via broker.check_exit (the same code the
    Executor uses), report fill vs level, and always end flat."""
    out = {"kind": kind, "result": None, "attempts": [], "flat_after": False}
    cid = f"{tag}-{'T' if kind == 'take_profit' else 'S'}"
    try:
        await broker.market_order(market, symbol, "BUY", qty, f"{cid}E")
        held = abs(await held_delta())
        out["held"] = str(held)
        for attempt, bps in enumerate((Decimal("5"), Decimal("10"), Decimal("20")), 1):
            if market == "futures":
                ref = Decimal(next(p for p in await ad.premium_index_all() if p["symbol"] == symbol)["markPrice"])
            else:
                bt = (await ad.book_ticker([symbol]))[0]
                ref = Decimal(bt["askPrice"]) if kind == "take_profit" else Decimal(bt["bidPrice"])
            near, far = bps / Decimal(10_000), Decimal("0.01")
            if kind == "take_profit":
                target, stop = ref * (1 + near), ref * (1 - far)
            else:
                stop, target = ref * (1 - near), ref * (1 + far)
            pos = {"trade_id": f"{cid}{attempt}", "market": market, "symbol": symbol, "direction": "LONG",
                   "qty": str(held), "stop_price": str(rules.round_price(stop)), "target_price": str(rules.round_price(target)),
                   "opened_at": time.time() - 5, "protection": None}
            try:
                info = await _place(broker, pos)
                out["attempts"].append({"offset_bps": str(bps), "ref": str(ref), "stop": pos["stop_price"],
                                        "target": pos["target_price"], "placed": info})
                break
            except BinanceError as ex:   # e.g. -2021 "would immediately trigger" if price moved through the level
                out["attempts"].append({"offset_bps": str(bps), "ref": str(ref), "error": f"{ex.code}: {ex.msg}"})
                pos = None
        if pos is None:
            out["result"] = "PLACEMENT_FAILED"
            return out
        deadline = time.time() + wait_min * 60
        ex = None
        while time.time() < deadline:
            ex = await broker.check_exit(pos)
            if ex.closed:
                break
            await asyncio.sleep(5)
        if ex and ex.closed:
            level = Decimal(pos["target_price"] if kind == "take_profit" else pos["stop_price"])
            adverse = (level - ex.exit_price) if ex.exit_price else Decimal(0)
            out.update(result="TRIGGERED", exchange_reason=ex.reason, exit_price=str(ex.exit_price), level=str(level),
                       fee_usdt=str(ex.fee_usdt), fill_vs_level_bps=float(adverse / level * 10_000) if ex.exit_price else None,
                       matches_expected=ex.reason == kind)
        else:
            out["result"] = "NOT_TRIGGERED"
            if market == "spot":            # OCO locks the balance: release it before selling
                await broker.cancel_protection(pos)
    finally:
        try:
            if out.get("result") != "TRIGGERED":
                fl = await flatten(f"{kind} test cleanup")
                out["cleanup"] = fl.get("status")
                if market == "futures" and "pos" in locals() and pos:
                    await broker.cancel_protection(pos)
            rem = await held_delta()
            out["flat_after"] = rem == 0
            out["residual"] = str(rem)
        except Exception as e2:  # noqa: BLE001
            out["cleanup_error"] = str(e2)
    lvl = out.get("level")
    out["summary"] = (f"{out['result']}" + (f" by exchange as {out.get('exchange_reason')} at {out.get('exit_price')} vs level {lvl} "
                      f"({out.get('fill_vs_level_bps', 0):+.1f} bps, + = worse)" if out["result"] == "TRIGGERED" else
                      " within the window (market did not reach the level; position flattened)" if out["result"] == "NOT_TRIGGERED"
                      else f" ({out['attempts'][-1].get('error') if out['attempts'] else ''})")
                      + f"; flat after: {out['flat_after']}")
    return out


async def _const(v):
    return v


async def _offset(c: BinanceClient):
    return await c.sync_time()


async def _res(coro):
    r = await coro
    return {"status": r.status, "filled_qty": str(r.filled_qty), "avg_price": str(r.avg_price), "fee_usdt": str(r.fee_usdt),
            "net_base_qty": str(r.net_base_qty), "exchange_id": r.exchange_id, "error": r.error, "raw": _scrub(r.raw)}


async def _spot_bal(ad, st, extra=()):
    a = await ad.account()
    return {b["asset"]: {"free": b["free"], "locked": b["locked"]} for b in a["balances"]
            if b["asset"] in ("USDT", "BTC", "ETH", "BNB", *extra)}


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
    real = meta.get("network", "").startswith("real")
    L = ["# Binance testnet validation report", "",
         ("> **NATURE OF RESULTS: ACTUAL EXCHANGE RESPONSES** from the Binance **TEST** environment "
          "(testnet.binance.vision, testnet.binancefuture.com) with test funds. No simulated or paper result appears "
          "in this report. Signatures and API keys are removed." if real else
          "> **NATURE OF RESULTS: MOCK EXCHANGE (automated test run). NOT EXCHANGE RESULTS.** This file must never be "
          "used as evidence of exchange behaviour."), "",
         f"Run {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(meta['ts']))} · symbol {meta['symbol']} · "
         f"host: {meta['host']} · network: {meta.get('network')}"
         + (" · **RUN WAS INTERRUPTED** (end-of-run guard still executed)" if meta.get("interrupted") else ""), "",
         "Status legend: PASS / FAIL = exchange response met / did not meet the expectation · SKIPPED = not executed "
         "(reason given) · exchange-triggered SL/TP tests report TRIGGERED or NOT_TRIGGERED (market did not reach "
         "the level in the window; never counted as a pass of that trigger).", ""]
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
        try:   # keys live only in the operator's shell session: remove them from the app vault
            e.vault.delete_credentials(market, "testnet")
        except Exception:  # noqa: BLE001
            pass
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


def preflight(symbol: str = "BTCUSDT", run_tests: bool = True) -> int:
    """Read-only readiness check. Places NOTHING. Exit code 0 = ready."""
    import importlib
    import re
    import subprocess
    import sys
    rows: list[tuple[str, str, bool | None, str]] = []   # (group, name, ok, detail); ok None = info

    def chk(group, name, ok, detail):
        rows.append((group, name, ok, detail))
    # ---- environment
    chk("environment", "python >= 3.11", sys.version_info >= (3, 11), sys.version.split()[0])
    for mod in ("fastapi", "httpx", "numpy", "cryptography", "websockets", "pydantic"):
        try:
            importlib.import_module(mod)
            chk("environment", f"module {mod}", True, "installed")
        except ImportError:
            chk("environment", f"module {mod}", False, "pip install -r requirements.txt")
    # ---- isolation / safety
    chk("safety", "GROK_PROFILE=testnet", os.environ.get("GROK_PROFILE") == "testnet", os.environ.get("GROK_PROFILE") or "not set")
    chk("safety", "GROK_ALLOW_LIVE not set", os.environ.get("GROK_ALLOW_LIVE") != "1", f"GROK_ALLOW_LIVE={os.environ.get('GROK_ALLOW_LIVE', '0')}")
    try:
        from .settings import load_settings
        st_ = load_settings()
        ep = st_.endpoints
        chk("safety", "all endpoints validated (testnet profile: data + streams + trading on TESTNET hosts)", True,
            f"spot {ep.spot_testnet} · futures {ep.futures_testnet} · data {ep.spot_data}, {ep.futures_data}")
        data_dir = st_.data_dir
        from .db import Database
        live_creds = []
        if st_.db_path.exists():
            db = Database(st_.db_path)
            live_creds = [r["id"] for r in db.all("SELECT id FROM credentials WHERE env='live'")]
            from .config_store import CapitalCfg, ConfigStore, RiskCfg
            cfg = ConfigStore(db)
            chk("safety", "initial capital is ₹50,000", cfg.capital.starting_inr == 50000, f"₹{cfg.capital.starting_inr:,.0f}")
            d, r_ = RiskCfg(), cfg.risk
            looser = [k for k in ("risk_per_trade_pct", "max_risk_per_trade_pct", "max_daily_loss_pct", "max_drawdown_pct",
                                  "max_leverage", "max_positions", "max_exposure_pct", "max_correlated_exposure_pct",
                                  "max_position_pct", "stale_data_seconds", "signal_ttl_seconds")
                      if getattr(r_, k) > getattr(d, k)] + (["min_reward_risk"] if r_.min_reward_risk < d.min_reward_risk else [])
            chk("safety", "risk limits not looser than defaults", not looser, ", ".join(looser) or "all at or tighter than defaults")
            db.close()
        else:
            from .config_store import CapitalCfg
            chk("safety", "initial capital is ₹50,000", CapitalCfg().starting_inr == 50000, "defaults (fresh data dir)")
        chk("safety", "data dir holds NO live (production) credentials", not live_creds,
            f"{data_dir}" + (f" contains {live_creds}" if live_creds else ""))
        chk("safety", "no leftover exposure from a previous run", not (data_dir / "LEFTOVER_EXPOSURE.json").exists(),
            "LEFTOVER_EXPOSURE.json present: run --cleanup-only" if (data_dir / "LEFTOVER_EXPOSURE.json").exists() else "none")
    except Exception as ex:  # noqa: BLE001
        chk("safety", "configuration valid", False, str(ex)[:160])
    # ---- credentials, connectivity, permissions, account configuration (signed, READ-ONLY)
    for m in ("spot", "futures"):
        k, sec = os.environ.get(f"GROK_TESTNET_{m.upper()}_KEY", ""), os.environ.get(f"GROK_TESTNET_{m.upper()}_SECRET", "")
        fmt_ok = bool(re.fullmatch(r"[A-Za-z0-9]{16,256}", k)) and bool(re.fullmatch(r"[A-Za-z0-9]{16,256}", sec))
        chk(m, "credentials present (format)", fmt_ok, "present, format OK" if fmt_ok else f"set GROK_TESTNET_{m.upper()}_KEY/_SECRET")
        for name, ok, detail in asyncio.run(_preflight_market(m, symbol, k if fmt_ok else None, sec if fmt_ok else None)):
            chk(m, name, ok, detail)
    # ---- safety test suite
    if run_tests:
        root = Path(__file__).resolve().parents[1]
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("GROK_")}   # never pass keys to tests
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-m", "safety", "-p", "no:cacheprovider", str(root / "tests")],
                           cwd=str(root), capture_output=True, text=True, env=clean_env,
                           encoding="utf-8", errors="replace")
        last = [x for x in r.stdout.strip().splitlines() if x.strip()][-1:] or ["no output"]
        chk("safety", "automated safety test suite", r.returncode == 0, last[0][:120])
    group = None
    for g, name, ok, detail in rows:
        if g != group:
            print(f"\n[{g}]")
            group = g
        print(f"  {'OK  ' if ok else ('INFO' if ok is None else 'FAIL')}  {name}: {detail}")
    bad = [r_ for r_ in rows if r_[2] is False]
    req = [r_ for r_ in rows if r_[2] is not None]
    print(f"\n{len(req) - len(bad)}/{len(req)} readiness checks passed" + ("" if not bad else " — fix every FAIL before running"))
    (_data_dir() / "testnet_preflight.json").write_text(json.dumps(
        [{"group": g, "check": n, "ok": o, "detail": d} for g, n, o, d in rows], indent=1), encoding="utf-8")
    return 1 if bad else 0


async def _preflight_market(market: str, symbol: str, key: str | None, secret: str | None) -> list:
    out = []
    base = SPOT if market == "spot" else FUT
    pub = _client(base, market)
    ad = SpotAdapter(pub) if market == "spot" else FuturesAdapter(pub)
    try:
        off = await pub.sync_time()
        out.append(("testnet reachable + clock offset < 1 s", abs(off) < 1000, f"{base} offset {off} ms"))
        rules = (await ad.exchange_info([symbol]))[symbol]
        out.append((f"{symbol} trading on testnet", rules.trading, f"status {rules.status}, min notional {rules.min_notional}"))
    except Exception as ex:  # noqa: BLE001
        out.append(("testnet reachable", False, f"{base}: {str(ex)[:120]}"))
        await pub.close()
        return out
    if not key:
        await pub.close()
        return out
    tc = _client(base, market, key, secret)
    sa = SpotAdapter(pub, tc) if market == "spot" else FuturesAdapter(pub, tc)
    try:
        if market == "spot":
            acct = await sa.account()
            out.append(("credentials accepted by testnet (signed read)", True, f"accountType {acct.get('accountType')}"))
            out.append(("trading permission (canTrade)", bool(acct.get("canTrade")), f"canTrade={acct.get('canTrade')}"))
            perms = acct.get("permissions") or acct.get("permissionSets") or []
            flat = [p for x in perms for p in (x if isinstance(x, list) else [x])]
            out.append(("SPOT permission on key/account", "SPOT" in flat or not flat, f"permissions {flat or 'n/a'}"))
            out.append(("withdraw/transfer capability", None,
                        f"account canWithdraw={acct.get('canWithdraw')} (testnet funds have no value); the application has "
                        "NO withdrawal/transfer code and refuses such endpoints by allowlist"))
            usdt = next((Decimal(b["free"]) for b in acct["balances"] if b["asset"] == "USDT"), Decimal(0))
            out.append(("test USDT balance sufficient (>= 50)", usdt >= 50, f"USDT free {usdt}"))
            oo = await sa.open_orders(symbol)
            out.append((f"no pre-existing open {symbol} orders", not oo, f"{len(oo)} open"))
        else:
            acct = await sa.account()
            out.append(("credentials accepted by testnet (signed read)", True, f"{len(acct.get('assets', []))} assets"))
            avail = next((Decimal(a["availableBalance"]) for a in acct.get("assets", []) if a["asset"] == "USDT"), Decimal(0))
            out.append(("test USDT available sufficient (>= 200)", avail >= 200, f"USDT available {avail}"))
            hedge = await sa.position_mode()
            out.append(("position mode One-way (required)", not hedge, f"dualSidePosition={hedge}"))
            try:
                mam = await tc.request("GET", "/fapi/v1/multiAssetsMargin", signed=True)
                out.append(("single-asset margin mode", not mam.get("multiAssetsMargin"), f"multiAssetsMargin={mam.get('multiAssetsMargin')}"))
            except BinanceError as ex:
                out.append(("single-asset margin mode", None, f"not checkable: {ex.msg}"))
            pr = await sa.position_risk(symbol)
            amt = sum((Decimal(p["positionAmt"]) for p in pr), Decimal(0))
            oo = await sa.open_orders(symbol)
            algo = await sa.open_conditionals(symbol)
            algo = algo if isinstance(algo, list) else algo.get("orders", [])
            out.append((f"no pre-existing {symbol} position / orders / algo orders", amt == 0 and not oo and not algo,
                        f"positionAmt {amt}, {len(oo)} orders, {len(algo)} algo orders"))
            out.append(("withdraw/transfer capability", None,
                        "testnet futures keys cannot be inspected via /sapi; the application has NO withdrawal/transfer "
                        "code and refuses such endpoints by allowlist"))
    except BinanceError as ex:
        out.append(("credentials accepted by testnet (signed read)", False, f"{ex.code}: {ex.msg} (production keys are rejected by testnet)"))
    except Exception as ex:  # noqa: BLE001
        out.append(("signed read-only checks", False, str(ex)[:160]))
    finally:
        await tc.close()
        await pub.close()
    return out


async def amain(a) -> None:
    """Runs the checks, appending to RUN as it goes so an interruption keeps what was done."""
    for m in (["spot", "futures"] if a.market == "both" else [a.market]):
        key, sec = os.environ.get(f"GROK_TESTNET_{m.upper()}_KEY"), os.environ.get(f"GROK_TESTNET_{m.upper()}_SECRET")
        RUN["steps"] += await run_market(m, a.symbol, key, sec, trigger_wait=a.trigger_wait)
        if a.pipeline and key and m not in BASELINES:
            RUN["steps"].append({"market": f"{m}-pipeline", "step": "application lifecycle", "status": "SKIPPED",
                                 "note": "baseline not established (unreachable or pre-existing exposure): not trading",
                                 "ts": time.time()})
            continue
        if a.pipeline:
            ps, mt = await run_pipeline(m, a.symbol, key, sec, direction="LONG" if m == "spot" else a.direction,
                                        wait_minutes=a.wait_minutes, cleanup=a.cleanup,
                                        db_dir=str(_data_dir() / f"testnet-pipeline-{m}"))
            RUN["steps"] += ps
            if mt:
                RUN["metrics"][m] = mt


async def finish(a) -> int:
    """ALWAYS runs (normal end, error or interruption): guard cleanup, final
    account state straight from the exchange, report."""
    from . import exec_metrics
    RUN["guard"] = await guard()
    creds = {m: (os.environ.get(f"GROK_TESTNET_{m.upper()}_KEY"), os.environ.get(f"GROK_TESTNET_{m.upper()}_SECRET"))
             for m in ("spot", "futures")}
    acct = await account_state(*creds["spot"], *creds["futures"]) if any(k for k, _ in creds.values()) else \
        {"statement": "No testnet credentials supplied: no test account was queried and no order was placed."}
    real = TRANSPORT is None
    meta = {"ts": time.time(), "symbol": a.symbol, "host": __import__("platform").node(), "pipeline": a.pipeline,
            "cleanup": a.cleanup, "wait_minutes": a.wait_minutes, "trigger_wait": a.trigger_wait,
            "interrupted": RUN["interrupted"], "network": "real Binance TEST network" if real else "MOCK exchange (automated test)"}
    steps = RUN["steps"]
    md = render(steps, meta)
    md += "\n## Exchange execution metrics (testnet application pipeline)\n\n"
    md += ("\n".join(exec_metrics.render(v, f"{k} pipeline") for k, v in RUN["metrics"].items()) if RUN["metrics"] else
           "_Not available: the application pipeline did not run (no credentials, GROK_PROFILE not testnet, or interrupted)._\n")
    md += "\n## End-of-run guard (always executed)\n\n"
    if RUN["guard"]:
        for m, g in RUN["guard"].items():
            md += (f"* **{m}**: {'CLEAN' if g.get('clean') else 'NOT CLEAN — see LEFTOVER_EXPOSURE.json'}; actions: "
                   f"{'; '.join(g.get('actions', [])) or 'none needed'}"
                   + (f"; error: {g['error']}" if g.get("error") else "") + "\n")
    else:
        md += "* No exchange exposure was created by this run.\n"
    md += f"\n## Final test-account state (queried from the exchange)\n\n**{acct['statement']}**\n\n```json\n{json.dumps(acct, indent=1, default=str)[:3000]}\n```\n"
    if real:   # never let a mock run overwrite the real report
        root = Path(__file__).resolve().parents[2]
        (root / "docs").mkdir(exist_ok=True)
        (root / "docs" / "testnet_report.md").write_text(md, encoding="utf-8")
    (_data_dir() / "testnet_report.json").write_text(json.dumps({"meta": meta, "steps": steps, "metrics": RUN["metrics"],
                                                                 "guard": RUN["guard"], "account": acct}, default=str, indent=1), encoding="utf-8")
    (_data_dir() / "testnet_report.md").write_text(md, encoding="utf-8")
    fails = [x for x in steps if x["status"] == "FAIL" and x.get("critical", True)]
    unclean = [m for m, g in RUN["guard"].items() if not g.get("clean")]
    print(f"\n{acct['statement']}")
    if unclean:
        print(f"!!! EXPOSURE NOT FULLY CLEANED on {unclean}: see {_data_dir() / 'LEFTOVER_EXPOSURE.json'} and run "
              "`python -m grokbot.testnet_validate --cleanup-only` (or close it on the testnet website).")
    print(f"{len(steps)} steps · {len(fails)} critical failures"
          + (" · RUN WAS INTERRUPTED" if RUN["interrupted"] else "") + f" · report: docs/testnet_report.md")
    return 130 if RUN["interrupted"] else (1 if fails or unclean else 0)


def main() -> None:
    import signal
    import sys
    for stream in (sys.stdout, sys.stderr):      # never crash on a console that cannot show ≤ → —
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(description="Binance TESTNET validation (never contacts production).")
    ap.add_argument("--market", choices=["spot", "futures", "both"], default="both")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--pipeline", action="store_true",
                    help="also run the full application lifecycle with an operator validation trade (needs GROK_PROFILE=testnet)")
    ap.add_argument("--direction", choices=["LONG", "SHORT"], default="LONG",
                    help="pipeline validation trade direction on futures (spot is always LONG)")
    ap.add_argument("--wait-minutes", type=float, default=20.0, help="pipeline: wait for the exchange to trigger SL/TP")
    ap.add_argument("--trigger-wait", type=float, default=10.0,
                    help="exchange-triggered SL and TP tests: minutes to wait for each")
    ap.add_argument("--cleanup", choices=["close_all", "cancel_pending", "report"], default="close_all",
                    help="pipeline end-of-test policy (positions are never closed while the exchange is unreachable)")
    ap.add_argument("--check", action="store_true", help="only verify prerequisites; place nothing")
    ap.add_argument("--cleanup-only", action="store_true",
                    help="only cancel run-created orders / flatten run-created exposure from a previous interrupted run")
    a = ap.parse_args()
    for h in (SPOT, FUT):
        from .endpoints import require
        require(h, "testnet", "harness host")
    if os.environ.get("GROK_ALLOW_LIVE") == "1":
        raise SystemExit("Refusing to run: GROK_ALLOW_LIVE=1 is set in this shell. Unset it for testnet validation.")
    if a.check:
        raise SystemExit(preflight(a.symbol))
    if a.cleanup_only:
        f = _data_dir() / "testnet_baseline.json"
        if not f.exists():
            raise SystemExit("no testnet_baseline.json in GROK_DATA_DIR: nothing recorded to clean up")
        BASELINES.update(json.loads(f.read_text(encoding="utf-8")))
        res = asyncio.run(guard())
        print(json.dumps(res, indent=1, default=str))
        raise SystemExit(0 if all(r.get("clean") for r in res.values()) else 1)
    marker = _data_dir() / "LEFTOVER_EXPOSURE.json"
    if marker.exists():
        raise SystemExit(f"Refusing to run: {marker} reports exposure left by a previous run. "
                         "Resolve it first with --cleanup-only.")

    def _term(*_):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, _term)
    try:
        asyncio.run(amain(a))
    except KeyboardInterrupt:
        RUN["interrupted"] = True
        print("\n!!! interrupted: running the end-of-run guard (cancel run orders, flatten run exposure)...")
    except Exception as ex:  # noqa: BLE001
        RUN["steps"].append({"market": "harness", "step": "unexpected error", "status": "FAIL",
                             "note": f"{type(ex).__name__}: {ex}", "ts": time.time()})
        print(f"\n!!! harness error: {ex}; running the end-of-run guard...")
    while True:
        try:
            rc = asyncio.run(finish(a))
            break
        except KeyboardInterrupt:
            print("guard must finish to leave the account clean; please wait (Ctrl-C again is ignored once)...")
            RUN["interrupted"] = True
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
