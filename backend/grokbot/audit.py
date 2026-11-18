"""Independent financial audit of a Grok Bot database.

    python -m grokbot.audit [--mode paper] [--db path]

Recomputes, from raw fills and the ledger ONLY (no use of Portfolio code),
every figure the system reports and compares:
  1. INR -> USDT capital conversion and the allocation split.
  2. Per position: entry VWAP, quantity, fees, margin = qty*entry/leverage,
     liquidation price vs Binance's isolated closed-form formula, and for
     closed positions realized P&L = gross - fees + funding.
  3. Per account (spot/futures/reserve): cash recomputed from fills vs ledger.
  4. Fee rates implied by each fill vs configured rates.
  5. Equity identity: total = starting + realized + unrealized (at entry marks).
  6. Ledger integrity: every trade-linked ledger row maps to a fill or funding event.
Prints a report and exits non-zero on any mismatch.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from decimal import Decimal

from .db import Database
from .settings import load_settings
from .util import D, ZERO

TOL = Decimal("0.000001")


def run(db: Database, mode: str) -> tuple[list[dict], dict]:
    checks: list[dict] = []

    def chk(name, ok, detail, expected=None, actual=None):
        checks.append({"check": name, "ok": bool(ok), "detail": detail,
                       "expected": str(expected) if expected is not None else None,
                       "actual": str(actual) if actual is not None else None})

    cfg = {r["key"]: json.loads(r["value"]) for r in db.all("SELECT key, value FROM config")}
    cap = json.loads(db.meta_get(f"capital_init:{mode}") or "null")
    if not cap:
        chk("capital initialised", False, f"no capital record for mode {mode}")
        return checks, {}
    start = D(cap["starting_inr"]) / D(cap["usd_inr"])
    chk("INR→USDT conversion", abs(start - D(cap["starting_usdt"])) < TOL,
        f"₹{cap['starting_inr']:,} / {cap['usd_inr']} USD-INR", start, cap["starting_usdt"])
    alloc = defaultdict(lambda: ZERO)
    for r in db.all("SELECT account, amount FROM ledger WHERE mode=? AND kind='allocation'", (mode,)):
        alloc[r["account"]] += D(r["amount"])
    chk("allocation sums to starting capital", abs(sum(alloc.values()) - start) < TOL,
        ", ".join(f"{k} {v:.4f}" for k, v in alloc.items()), start, sum(alloc.values()))

    fees_cfg = cfg.get("fees", {})
    positions = db.all("SELECT * FROM positions WHERE mode=?", (mode,))
    cash = defaultdict(lambda: ZERO, {k: v for k, v in alloc.items()})
    realized_total, unreal_total = ZERO, ZERO
    for p in positions:
        tid = p["trade_id"]
        fills = db.all("SELECT f.*, o.purpose, o.side, o.type FROM fills f JOIN orders o ON o.client_id=f.client_id "
                       "WHERE f.trade_id=? ORDER BY f.ts", (tid,))
        entries = [f for f in fills if f["purpose"] == "entry"]
        exits = [f for f in fills if f["purpose"] == "exit"]
        eq = sum((D(f["net_qty"] or f["qty"]) for f in entries), ZERO)
        if not entries:
            chk(f"{tid} has entry fills", False, "position without entry fills")
            continue
        vwap = sum((D(f["net_qty"] or f["qty"]) * D(f["price"]) for f in entries), ZERO) / eq
        d = 1 if p["direction"] == "LONG" else -1
        chk(f"{tid} entry VWAP", abs(vwap - D(p["entry_price"])) <= vwap * TOL, f"{p['symbol']} from {len(entries)} fill(s)",
            vwap, p["entry_price"])
        fee_all = sum((D(f["fee_usdt"]) for f in fills), ZERO)
        chk(f"{tid} fees", abs(fee_all - D(p["fees"])) < TOL, "sum of fill fees", fee_all, p["fees"])
        for f in fills:
            px, q = D(f["price"]), D(f["qty"])
            if q > 0 and D(f["fee_usdt"]) > 0:
                rate = D(f["fee_usdt"]) / (px * q)
                key = ("spot" if p["market"] == "spot" else "futures") + ("_maker" if f["type"] == "LIMIT" else "_taker")
                exp = D(str(fees_cfg.get(key, rate)))
                if mode == "paper":
                    chk(f"{tid} fee rate {f['client_id']}", abs(rate - exp) < D("0.0000001"), key, exp, round(rate, 8))
        if p["market"] == "futures":
            m_exp = (eq * vwap / D(p["leverage"])) if p["status"] != "CLOSED" else ZERO
            chk(f"{tid} margin = qty×entry/leverage", abs(m_exp - D(p["margin"])) < D("0.0001"), f"{p['leverage']}x", m_exp, p["margin"])
            if p["liq_price"]:
                L, mmr = D(p["leverage"]), D("0.005")
                lp = vwap * (1 - 1 / L) / (1 - mmr) if d == 1 else vwap * (1 + 1 / L) / (1 + mmr)
                chk(f"{tid} liquidation (Binance isolated formula, MMR 0.5%)", abs(lp - D(p["liq_price"])) <= lp * D("0.0001"),
                    "LP = EP(1∓1/L)/(1∓MMR)", lp, p["liq_price"])
        # cash flows
        if p["market"] == "spot":
            cash["spot"] -= sum((D(f["qty"]) * D(f["price"]) for f in entries), ZERO)
            cash["spot"] += sum((D(f["qty"]) * D(f["price"]) for f in exits), ZERO)
            cash["spot"] -= fee_all
        else:
            gross_closed = sum((d * (D(f["price"]) - vwap) * D(f["qty"]) for f in exits), ZERO)
            cash["futures"] += gross_closed - fee_all
        funding = D(p["funding"])
        led_funding = sum((D(r["amount"]) for r in db.all(
            "SELECT amount FROM ledger WHERE trade_id=? AND kind='funding'", (tid,))), ZERO)
        chk(f"{tid} funding = ledger funding", abs(funding - led_funding) < TOL, "", led_funding, funding)
        cash["futures"] += led_funding
        if p["status"] == "CLOSED":
            xq = sum((D(f["qty"]) for f in exits), ZERO)
            gross = sum((d * (D(f["price"]) - vwap) * D(f["qty"]) for f in exits), ZERO)
            if p["exit_reason"] == "liquidation" and not exits:
                gross = d * (D(p["exit_price"]) - vwap) * eq
                xq = eq
            real = gross - fee_all + funding
            chk(f"{tid} closed qty = entry qty", abs(xq - eq) < TOL, p["exit_reason"] or "", eq, xq)
            chk(f"{tid} realized P&L", abs(real - D(p["realized_pnl"])) < D("0.00001"),
                f"{p['symbol']} {p['direction']} {p['exit_reason']}", real, p["realized_pnl"])
            realized_total += real
        else:
            unreal_total += -fee_all + funding  # marked at entry price (no market data in audit)
    led = defaultdict(lambda: ZERO)
    for r in db.all("SELECT account, amount FROM ledger WHERE mode=?", (mode,)):
        led[r["account"]] += D(r["amount"])
    for acct in ("spot", "futures", "reserve"):
        chk(f"{acct} cash: recomputed from fills vs ledger", abs(cash[acct] - led[acct]) < D("0.00001"), "",
            cash[acct], led[acct])
    # equity identity at entry marks: cash + holdings(at entry) == start + realized + (−open fees + open funding)
    hold = sum((D(p["qty"]) * D(p["entry_price"]) for p in positions if p["status"] != "CLOSED" and p["market"] == "spot"), ZERO)
    total = led["spot"] + hold + led["futures"] + led["reserve"]
    chk("equity identity (open positions marked at entry)", abs(total - (start + realized_total + unreal_total)) < D("0.00001"),
        "spot cash + holdings + futures wallet + reserve = start + realized − open fees + open funding",
        start + realized_total + unreal_total, total)
    orphans = db.all("SELECT l.id, l.kind FROM ledger l WHERE l.mode=? AND l.trade_id IS NOT NULL AND l.kind IN ('trade','fee','pnl') "
                     "AND NOT EXISTS (SELECT 1 FROM fills f WHERE f.trade_id=l.trade_id)", (mode,))
    chk("no orphan ledger rows", not orphans, f"{len(orphans)} trade-linked ledger rows without fills")
    summary = {"mode": mode, "starting_usdt": str(start), "positions": len(positions),
               "closed": sum(1 for p in positions if p["status"] == "CLOSED"), "realized": str(realized_total),
               "ledger": {k: str(v) for k, v in led.items()}}
    return checks, summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="paper")
    ap.add_argument("--db", default=None)
    a = ap.parse_args()
    db = Database(a.db or load_settings().db_path)
    checks, summary = run(db, a.mode)
    bad = [c for c in checks if not c["ok"]]
    for c in checks:
        print(f"{'PASS' if c['ok'] else 'FAIL'}  {c['check']}: {c['detail']}"
              + (f" (expected {c['expected']}, recorded {c['actual']})" if c["expected"] is not None else ""))
    print(json.dumps(summary, indent=1))
    print(f"\n{len(checks)} checks, {len(bad)} failures")
    raise SystemExit(1 if bad else 0)


if __name__ == "__main__":
    main()
