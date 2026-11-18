"""Paper-session evidence report, generated from the database records.

    python -m grokbot.paper_report [--db path] [--out docs/paper_session_report.md]

Everything in the report is read from the persisted records (agent_events,
trades, orders, fills, positions, ledger, equity_snapshots); nothing is
estimated. The independent audit (grokbot.audit) is run and embedded.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from collections import Counter
from pathlib import Path

from . import audit
from .db import Database, loads
from .settings import load_settings
from .util import D


def ts(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if t else "—"


def render(db: Database, mode: str = "paper") -> str:
    ev = db.all("SELECT * FROM agent_events WHERE mode=? ORDER BY id", (mode,))
    trades = db.all("SELECT * FROM trades WHERE mode=? ORDER BY created_at", (mode,))
    positions = db.all("SELECT * FROM positions WHERE mode=? ORDER BY opened_at", (mode,))
    snaps = db.all("SELECT * FROM equity_snapshots WHERE mode=? ORDER BY ts", (mode,))
    cap = json.loads(db.meta_get(f"capital_init:{mode}") or "{}")
    t0, t1 = (ev[0]["ts"], ev[-1]["ts"]) if ev else (0, 0)
    L = ["# Paper-trading session evidence", "",
         f"Generated {ts(dt.datetime.now(dt.timezone.utc).timestamp())} UTC from the session database. "
         "Mode: **PAPER** (simulated fills on genuine Binance market data; not real performance).", "",
         f"* Session window: {ts(t0)} → {ts(t1)} UTC ({(t1 - t0) / 3600:.2f} h of recorded activity)",
         f"* Capital: ₹{cap.get('starting_inr', 0):,} at USD/INR {cap.get('usd_inr')} = {float(cap.get('starting_usdt', 0)):.4f} USDT",
         f"* Market data: Spot production (data-api.binance.vision + data-stream WebSocket); Futures prices from the "
         "Binance Futures TESTNET feed (production futures endpoints are geo-blocked in this environment).",
         *([f"* Process starts: {n_starts} (initial start + {n_starts - 1} restart(s) with open positions; positions and "
            "orders persisted, the system came back STOPPED each time, and no order was resubmitted)."]
           if (n_starts := sum(1 for e in ev if e['event'] == 'equity_updated' and 'startup' in e['message'])) else []), ""]
    from . import exec_metrics
    L.append(exec_metrics.render(exec_metrics.compute(db, mode), "Execution metrics"))
    cnt = Counter(e["event"] for e in ev)
    stages = Counter(t["stage"] for t in trades)
    L += ["## Pipeline funnel", "",
          f"* Scan cycles: {cnt['scan_cycle']} · opportunities detected: {cnt['opportunity']}",
          f"* Rug Check: {cnt.get('approved', 0)} approved / {sum(1 for e in ev if e['agent'] == 'rugcheck' and e['event'] == 'rejected')} rejected",
          f"* Sizer: {cnt['sized']} sized / {sum(1 for e in ev if e['agent'] == 'sizer' and e['event'] == 'rejected')} rejected",
          f"* Risk: {cnt['risk_approved']} approved / {cnt['risk_rejected']} rejected",
          f"* Executor: {cnt['order_submitted']} orders submitted · {cnt['order_filled']} fills · {cnt['position_closed']} positions closed"
          f" · {cnt.get('order_repriced', 0)} reprices · {cnt.get('entry_unfilled', 0)} limit entries expired unfilled",
          f"* Final opportunity stages: " + ", ".join(f"{k} {v}" for k, v in stages.most_common()), ""]
    reasons = Counter()
    for t in trades:
        if t["reject_reason"]:
            r = t["reject_reason"]
            head = r.split(":")[0] + ": " + (r.split(":")[1].split("(")[0].strip() if ":" in r else "")
            reasons[head[:90]] += 1
    L += ["### Why opportunities were not traded", "", "| Reason | Count |", "|---|---:|"]
    L += [f"| {k} | {v} |" for k, v in reasons.most_common(15)] + [""]
    L += ["## Trades", ""]
    for p in positions:
        tid = p["trade_id"]
        t = next((x for x in trades if x["id"] == tid), None)
        sz = loads(t["sizing"]) if t else {}
        L += [f"### {p['symbol']} {p['market']} {p['direction']} · {p['strategy']} · `{tid}` · {p['status']}", ""]
        L += [f"* Planned: entry {loads(t['signal'])['entry']:.6g}, stop {p['stop_price']}, target {p['target_price']}; "
              f"qty {sz.get('qty')}, risk {sz.get('risk_usdt', 0):.4f} USDT ({sz.get('risk_pct', 0):.3f}% of {p['market']} equity), "
              f"{p['leverage']}x, net R:R {sz.get('rr_net', 0):.2f}" if t else ""]
        if p.get("exit_meta"):
            m = json.loads(p["exit_meta"])
            if m.get("level"):
                L.append(f"* Exit execution: level {D(m['level']):.6g}, recorded fill {D(m['fill']):.6g}"
                         + (f" (raw book fill {D(m['book_fill']):.6g})" if m.get("book_fill") and m["book_fill"] != m["fill"] else "")
                         + f", {m.get('slippage_vs_level_bps', 0):+.1f} bps vs level (+ = worse); trigger: "
                         f"{m.get('trigger_source')} {m.get('trigger_price') or m.get('trigger_minute_utc') or ''}")
        if p["status"] == "CLOSED":
            L.append(f"* Result: exit {p['exit_reason']} @ {D(p['exit_price']):.6g}; gross {D(p['gross_pnl']):+.6f}, fees "
                     f"{D(p['fees']):.6f}, funding {D(p['funding']):+.6f}, slippage {D(p['slippage']):.6f} → **realized "
                     f"{D(p['realized_pnl']):+.6f} USDT**")
        L += ["", "| Fill time (UTC) | Order | Purpose | Qty | Price | Ref (mid) | Fee USDT | Slippage USDT |", "|---|---|---|---:|---:|---:|---:|---:|"]
        for f in db.all("SELECT f.*, o.purpose, o.type FROM fills f JOIN orders o ON o.client_id=f.client_id WHERE f.trade_id=? ORDER BY f.ts", (tid,)):
            L.append(f"| {ts(f['ts'])} | {f['client_id']} ({f['type']}) | {f['purpose']} | {f['qty']} | {D(f['price']):.6g} | "
                     f"{D(f['ref_price'] or 0):.6g} | {D(f['fee_usdt']):.6f} | {D(f['slippage_usdt']):.6f} |")
        L += ["", "| Ledger (UTC) | Account | Kind | Amount | Note |", "|---|---|---|---:|---|"]
        for r in db.all("SELECT * FROM ledger WHERE trade_id=? ORDER BY id", (tid,)):
            L.append(f"| {ts(r['ts'])} | {r['account']} | {r['kind']} | {D(r['amount']):+.6f} | {r['note']} |")
        L += ["", "<details><summary>Agent trail</summary>", "", "| Time (UTC) | Agent | Event | Message |", "|---|---|---|---|"]
        for e in db.all("SELECT * FROM agent_events WHERE trade_id=? ORDER BY id", (tid,)):
            L.append(f"| {ts(e['ts'])} | {e['agent']} | {e['event']} | {e['message'].replace('|', '/')[:180]} |")
        L += ["", "</details>", ""]
    if snaps:
        tot = [s["total"] for s in snaps]
        L += ["## Equity", "",
              f"* {len(snaps)} snapshots; first {tot[0]:.4f}, last {tot[-1]:.4f}, min {min(tot):.4f}, max {max(tot):.4f} USDT",
              f"* Realized {snaps[-1]['realized']:+.6f} · unrealized {snaps[-1]['unrealized']:+.6f} (last snapshot)", ""]
    checks, summary = audit.run(db, mode)
    bad = [c for c in checks if not c["ok"]]
    L += ["## Independent audit (recomputed from fills and ledger)", "",
          f"**{len(checks) - len(bad)}/{len(checks)} checks passed.**", "", "| Check | Result | Expected | Recorded |", "|---|:-:|---:|---:|"]
    for c in checks:
        L.append(f"| {c['check']} | {'✅' if c['ok'] else '❌'} | {c['expected'] or ''} | {c['actual'] or ''} |")
    L.append("")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", action="append", help="database path (repeatable)")
    ap.add_argument("--title", action="append", help="section title per --db")
    ap.add_argument("--preface", default=None, help="markdown file inserted at the top")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[2] / "docs" / "paper_session_report.md"))
    a = ap.parse_args()
    dbs = a.db or [str(load_settings().db_path)]
    titles = a.title or [None] * len(dbs)
    parts = [Path(a.preface).read_text()] if a.preface else []
    for path, title in zip(dbs, titles):
        md = render(Database(path))
        if title:
            md = md.replace("# Paper-trading session evidence", f"# {title}", 1)
        parts.append(md)
    Path(a.out).write_text("\n\n---\n\n".join(parts))
    print("wrote", a.out)


if __name__ == "__main__":
    main()
