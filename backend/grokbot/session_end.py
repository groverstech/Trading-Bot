"""End-of-test / end-of-session procedure.

    python -m grokbot.session_end [--mode paper|testnet] [--policy report|cancel_pending|close_all]

1. Identify outstanding exposure: open positions, working limit entries and
   unresolved orders recorded by the system.
2. Reconcile with the exchange (testnet/live): positions (futures
   positionRisk, spot base-asset balances), open orders and open algo
   orders; flag anything the exchange holds that the system does not know,
   and vice versa.
3. Verify protective orders for every open position.
4. Report all remaining exposure (notional, unrealized P&L, margin).
5. Apply the cleanup policy:
     report          - change nothing (default for strategy sessions)
     cancel_pending  - cancel working entry orders, keep positions protected
     close_all       - cancel pending entries and close positions at market
   Positions are NEVER closed while the exchange is unreachable or market
   data is stale; they are reported as kept, with the reason.
6. Re-check and state explicitly how many positions and pending orders remain.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from decimal import Decimal

from .db import dumps
from .util import D, ZERO

LIVE_ORDER = ("SUBMITTING", "NEW", "PARTIALLY_FILLED", "UNKNOWN")


def _exposure(e, mode: str) -> dict:
    positions = e.db.all("SELECT * FROM positions WHERE mode=? AND status IN ('OPEN','CLOSING')", (mode,))
    rows, tot_notional, tot_unreal, tot_margin = [], ZERO, ZERO, ZERO
    for p in positions:
        u = e.pf.position_unrealized(p)
        prot = json.loads(p["protection"]) if p.get("protection") else None
        rows.append({"trade_id": p["trade_id"], "symbol": p["symbol"], "market": p["market"],
                     "direction": p["direction"], "qty": p["qty"], "entry": p["entry_price"],
                     "stop": p["stop_price"], "target": p["target_price"], "mark": str(u["mark"]),
                     "notional": float(u["notional"]), "unrealized": float(u["net"]), "margin": p["margin"],
                     "protection": (prot or {}).get("type") or ("local-monitor" if mode == "paper" else "NONE"),
                     "age_min": round((time.time() - p["opened_at"]) / 60, 1)})
        tot_notional += u["notional"]
        tot_unreal += u["net"]
        tot_margin += D(p["margin"])
    working = e.db.all("SELECT id, symbol, market FROM trades WHERE mode=? AND stage='WORKING'", (mode,))
    pending = e.db.all(f"SELECT client_id, symbol, market, side, purpose, status FROM orders WHERE mode=? AND status IN "
                       f"({','.join('?' * len(LIVE_ORDER))})", (mode, *LIVE_ORDER))
    return {"positions": rows, "working_entries": working, "pending_orders": pending,
            "total_notional": float(tot_notional), "total_unrealized": float(tot_unreal), "total_margin": float(tot_margin)}


async def _exchange_state(e, mode: str, symbols: set[str]) -> dict:
    """What the exchange itself reports (testnet/live only)."""
    out: dict = {"futures_positions": [], "futures_open_orders": [], "futures_open_algo": [], "spot_open_orders": [],
                 "spot_balances": {}, "errors": []}
    b = e.broker(mode)
    try:
        if e.trade_client(mode, "futures"):
            for p in await b.futures.position_risk():
                if D(p.get("positionAmt", "0")) != 0:
                    out["futures_positions"].append({k: p.get(k) for k in ("symbol", "positionAmt", "entryPrice",
                                                                           "markPrice", "liquidationPrice", "unRealizedProfit")})
            out["futures_open_orders"] = [{k: o.get(k) for k in ("symbol", "clientOrderId", "type", "side", "status")}
                                          for o in await b.futures.open_orders()]
            algo = await b.futures.open_conditionals()
            out["futures_open_algo"] = [{k: o.get(k) for k in ("symbol", "clientAlgoId", "orderType", "type", "triggerPrice")}
                                        for o in (algo if isinstance(algo, list) else algo.get("orders", []))]
    except Exception as ex:  # noqa: BLE001
        out["errors"].append(f"futures: {ex}")
    try:
        if e.trade_client(mode, "spot"):
            out["spot_open_orders"] = [{k: o.get(k) for k in ("symbol", "clientOrderId", "type", "side", "status")}
                                       for o in await b.spot.open_orders()]
            acct = await b.spot.account()
            bases = {s[:-4] for s in symbols}
            out["spot_balances"] = {x["asset"]: {"free": x["free"], "locked": x["locked"]} for x in acct.get("balances", [])
                                    if x["asset"] in bases and D(x["free"]) + D(x["locked"]) > 0}
    except Exception as ex:  # noqa: BLE001
        out["errors"].append(f"spot: {ex}")
    return out


def _reconcile(exp: dict, ex: dict) -> list[str]:
    issues = []
    known_fut = {p["symbol"] for p in exp["positions"] if p["market"] == "futures"}
    for p in ex["futures_positions"]:
        if p["symbol"] not in known_fut:
            issues.append(f"exchange futures position {p['symbol']} {p['positionAmt']} not tracked by the system")
    for p in exp["positions"]:
        if p["market"] == "futures" and not any(x["symbol"] == p["symbol"] for x in ex["futures_positions"]):
            issues.append(f"system futures position {p['symbol']} not found on exchange")
    return issues


async def finalize(e, mode: str, policy: str = "report", label: str = "session") -> dict:
    assert policy in ("report", "cancel_pending", "close_all")
    t0 = time.time()
    before = _exposure(e, mode)
    symbols = {p["symbol"] for p in before["positions"]}
    rep: dict = {"label": label, "mode": mode, "policy": policy, "ts": t0, "before": before, "actions": [],
                 "protection": [], "reconciliation": [], "exchange_before": None}
    if mode != "paper":
        rep["exchange_before"] = await _exchange_state(e, mode, symbols)
        rep["reconciliation"] = _reconcile(before, rep["exchange_before"])
    for p in e.db.all("SELECT * FROM positions WHERE mode=? AND status='OPEN'", (mode,)):
        r = await e.executor.verify_protection(p, e.broker(mode))
        rep["protection"].append({"trade_id": p["trade_id"], "symbol": p["symbol"], **{k: r.get(k) for k in ("ok", "detail")}})
    if policy in ("cancel_pending", "close_all"):
        for w in e.db.all("SELECT id FROM trades WHERE mode=? AND stage='WORKING'", (mode,)):
            await e.executor.cancel_working(e.executor._trade(w["id"]), f"end-of-{label} cleanup")
            rep["actions"].append({"trade_id": w["id"], "action": "working entry cancelled"})
        n = e.db.execute("UPDATE trades SET stage='CANCELLED', status='cancelled', reject_reason=? WHERE mode=? AND stage IN "
                         "('SCANNED','RUGCHECK_PASSED','SIZED','RISK_APPROVED','AWAITING_APPROVAL','AWAITING_LIVE_CONFIRM')",
                         (f"end-of-{label} cleanup", mode)).rowcount
        if n:
            rep["actions"].append({"action": f"{n} pending opportunities cancelled"})
    if policy == "close_all":
        for p in e.db.all("SELECT * FROM positions WHERE mode=? AND status='OPEN'", (mode,)):
            ok, why = e.risk.exchange_ok(mode, p["market"])
            age = e.md.age(p["market"], p["symbol"])
            if not ok or age > e.cfg.risk.stale_data_seconds:
                rep["actions"].append({"trade_id": p["trade_id"], "symbol": p["symbol"], "action": "KEPT",
                                       "detail": f"not closed blindly: {why if not ok else f'stale data ({age:.0f}s)'}"})
                continue
            r = await e.executor.close_position(p, f"end_of_{label}", "session-end")
            q = e.db.one("SELECT status, exit_price, realized_pnl FROM positions WHERE trade_id=?", (p["trade_id"],))
            rep["actions"].append({"trade_id": p["trade_id"], "symbol": p["symbol"],
                                   "action": "closed" if r.get("ok") else "close FAILED",
                                   "detail": r.get("reason") or f"exit {q['exit_price']}, realized {q['realized_pnl']}"})
    after = _exposure(e, mode)
    rep["after"] = after
    if mode != "paper":
        rep["exchange_after"] = await _exchange_state(e, mode, {p["symbol"] for p in before["positions"]})
    ex_after = rep.get("exchange_after") or {}
    rep["statement"] = (
        f"After end-of-{label} ({policy}): {len(after['positions'])} open position(s), "
        f"{len(after['working_entries'])} working entry order(s), {len(after['pending_orders'])} pending order(s) "
        f"recorded by the system"
        + ("" if mode == "paper" else
           f"; exchange reports {len(ex_after.get('futures_positions', []))} futures position(s), "
           f"{len(ex_after.get('futures_open_orders', [])) + len(ex_after.get('spot_open_orders', []))} open order(s), "
           f"{len(ex_after.get('futures_open_algo', []))} open algo order(s)")
        + f". Remaining notional {after['total_notional']:.2f} USDT, unrealized {after['total_unrealized']:+.4f} USDT.")
    e.logger.record("orchestrator", "session_end", rep["statement"], None,
                    {"policy": policy, "actions": rep["actions"], "reconciliation": rep["reconciliation"],
                     "protection": rep["protection"]}, "warning" if after["positions"] else "info")
    e.state.audit("session-end", "session_end", {"policy": policy, "statement": rep["statement"]})
    return rep


def render(rep: dict) -> str:
    L = [f"### End-of-{rep['label']} procedure ({rep['mode']}, policy `{rep['policy']}`)", ""]
    for n in rep.get("downtime_notes", []):
        L.append(f"* Downtime reconciliation: {n}")
    b = rep["before"]
    L.append(f"* Before: {len(b['positions'])} open position(s), {len(b['working_entries'])} working entr(y/ies), "
             f"{len(b['pending_orders'])} pending order(s); notional {b['total_notional']:.2f} USDT, "
             f"unrealized {b['total_unrealized']:+.4f} USDT")
    for p in b["positions"]:
        L.append(f"  * {p['symbol']} {p['market']} {p['direction']} qty {p['qty']} entry {D(p['entry']):.6g} mark "
                 f"{D(p['mark']):.6g} SL {D(p['stop']):.6g} TP {D(p['target']):.6g} · protection {p['protection']} · "
                 f"open {p['age_min']} min · uPnL {p['unrealized']:+.4f}")
    for pr in rep["protection"]:
        L.append(f"  * protection check {pr['symbol']}: {'OK' if pr['ok'] else ('UNVERIFIED' if pr['ok'] is None else 'INVALID')}"
                 f" ({pr['detail']})")
    if rep["mode"] != "paper":
        L.append(f"* Reconciliation with exchange: {'; '.join(rep['reconciliation']) or 'consistent'}")
    for a in rep["actions"]:
        L.append(f"* Action: {a.get('symbol', '')} {a['action']} {a.get('detail', '')}")
    L += ["", f"**{rep['statement']}**", ""]
    return "\n".join(L)


async def amain(a) -> None:
    from .engine import Engine
    from .settings import load_settings
    e = Engine(load_settings())
    try:
        await e.fx.refresh()
        syms = {p["symbol"] for p in e.db.all("SELECT symbol FROM positions WHERE status='OPEN'")}
        e.md.set_symbols(sorted(syms | set(e.cfg.scanner.spot_symbols)), sorted(syms | set(e.cfg.scanner.futures_symbols)))
        await e.md.load_rules()
        for fn in (e.md.refresh_spot, lambda: e.md.refresh_futures(True)):
            try:
                await fn()
            except Exception as ex:  # noqa: BLE001 - reported via staleness checks
                print("market data:", ex)
        notes = await e.executor.paper_downtime_reconcile()
        for n in notes:
            print("downtime:", n)
            e.logger.record("executor", "recovery", n, None, None, "warning")
        rep = await finalize(e, a.mode or e.state.mode, a.policy, a.label)
        rep["downtime_notes"] = notes
        print(render(rep))
        if a.json:
            open(a.json, "w", encoding="utf-8").write(dumps(rep))
    finally:
        await e.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["paper", "testnet", "live"], default=None)
    ap.add_argument("--policy", choices=["report", "cancel_pending", "close_all"], default="report")
    ap.add_argument("--label", default="session")
    ap.add_argument("--json", default=None)
    asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    main()
