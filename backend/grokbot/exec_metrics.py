"""Execution metrics computed from the persisted records of one mode.

Used by the paper evidence report and the testnet validation report so both
present the same, comparable numbers. Results are always labelled with
their nature: SIMULATED (paper fills) or EXCHANGE (orders on Binance).
"""
from __future__ import annotations

import json
from collections import Counter

from . import audit
from .db import Database
from .util import D, ZERO

NATURE = {"paper": "SIMULATED (paper fills on real market data; no exchange orders)",
          "testnet": "EXCHANGE (orders on the Binance TEST environment; test funds)",
          "live": "EXCHANGE (real funds)"}
APPROVED_STAGES = ("RISK_APPROVED", "AWAITING_APPROVAL", "AWAITING_LIVE_CONFIRM", "EXECUTING", "WORKING", "OPEN", "CLOSED")


def compute(db: Database, mode: str) -> dict:
    trades = db.all("SELECT stage, reject_reason, risk FROM trades WHERE mode=?", (mode,))
    orders = db.all("SELECT * FROM orders WHERE mode=?", (mode,))
    fills = db.all("SELECT * FROM fills WHERE mode=?", (mode,))
    pos = db.all("SELECT * FROM positions WHERE mode=?", (mode,))
    approved = [t for t in trades if t["stage"] in APPROVED_STAGES
                or (t["stage"] in ("FAILED", "ABANDONED", "EXPIRED", "CANCELLED") and t["risk"]
                    and (json.loads(t["risk"]) or {}).get("approved"))]
    rejected = [t for t in trades if t["stage"] == "REJECTED"]
    reasons: Counter = Counter()
    for t in rejected:
        r = t["reject_reason"] or ""
        agent = r.split(":", 1)[0]
        checks = [x.strip().split(" ")[0].split("(")[0] for x in r.split(":", 1)[1].split(";")] if ":" in r else [r]
        for c in checks:
            if c:
                reasons[f"{agent}: {c}"] += 1
    exits = [p for p in pos if p["status"] == "CLOSED"]
    sl = [p for p in exits if (p["exit_reason"] or "").startswith("stop_loss")]
    tp = [p for p in exits if (p["exit_reason"] or "").startswith("take_profit")]
    exit_bps = []
    for p in exits:
        m = json.loads(p["exit_meta"]) if p.get("exit_meta") else {}
        if m.get("slippage_vs_level_bps") is not None:
            exit_bps.append((p["symbol"], p["exit_reason"], m["slippage_vs_level_bps"]))
    partial = [o for o in orders if D(o["filled_qty"] or 0) > 0 and D(o["filled_qty"]) < D(o["qty"])]
    failed = [o for o in orders if o["status"] in ("REJECTED", "NOT_FOUND", "UNKNOWN", "ABANDONED")]
    filled = [o for o in orders if o["status"] == "FILLED" or D(o["filled_qty"] or 0) > 0]
    live = [o for o in orders if o["status"] in ("SUBMITTING", "NEW", "PARTIALLY_FILLED", "UNKNOWN")]
    checks, _ = audit.run(db, mode) if db.meta_get(f"capital_init:{mode}") else ([], {})
    return {
        "mode": mode, "nature": NATURE[mode],
        "opportunities": len(trades), "approved": len(approved), "rejected": len(rejected),
        "rejection_reasons": reasons.most_common(),
        "other_outcomes": dict(Counter(t["stage"] for t in trades if t["stage"] not in ("REJECTED",) + APPROVED_STAGES)),
        "orders_submitted": len(orders), "orders_by_purpose": dict(Counter(o["purpose"] for o in orders)),
        "orders_by_type": dict(Counter(o["type"] for o in orders)),
        "successful_fills": len(filled), "fill_records": len(fills), "partial_fills": len(partial),
        "failed_orders": len(failed), "cancelled_or_expired": sum(1 for o in orders if o["status"] in ("CANCELED", "EXPIRED")),
        "positions_opened": len(pos), "stop_loss_exits": len(sl), "take_profit_exits": len(tp),
        "other_exits": len(exits) - len(sl) - len(tp),
        "exit_reasons": dict(Counter(p["exit_reason"] for p in exits)),
        "exit_slippage_vs_level_bps": exit_bps,
        "slippage_usdt": float(sum((D(f["slippage_usdt"]) for f in fills), ZERO)),
        "fees_usdt": float(sum((D(f["fee_usdt"]) for f in fills), ZERO)),
        "realized_pnl_usdt": float(sum((D(p["realized_pnl"]) for p in exits), ZERO)),
        "open_positions": [{"symbol": p["symbol"], "market": p["market"], "direction": p["direction"], "qty": p["qty"]}
                           for p in pos if p["status"] != "CLOSED"],
        "pending_orders": [{"client_id": o["client_id"], "symbol": o["symbol"], "status": o["status"]} for o in live],
        "audit_checks": len(checks), "audit_failures": [c for c in checks if not c["ok"]],
    }


def render(m: dict, title: str | None = None) -> str:
    L = [f"#### {title or 'Execution metrics'} — {m['nature']}", "",
         "| Metric | Value |", "|---|---|",
         f"| Opportunities detected | {m['opportunities']} |",
         f"| Approved by central risk | {m['approved']} |",
         f"| Rejected | {m['rejected']} |",
         f"| Other outcomes (expired/cancelled/failed before order) | {m['other_outcomes'] or '—'} |",
         f"| Orders submitted | {m['orders_submitted']} (by purpose {m['orders_by_purpose']}, by type {m['orders_by_type']}) |",
         f"| Successful fills (orders with fills / fill records) | {m['successful_fills']} / {m['fill_records']} |",
         f"| Partial fills | {m['partial_fills']} |",
         f"| Failed orders | {m['failed_orders']} |",
         f"| Cancelled / expired orders | {m['cancelled_or_expired']} |",
         f"| Positions opened | {m['positions_opened']} |",
         f"| Stop-loss exits | {m['stop_loss_exits']} |",
         f"| Take-profit exits | {m['take_profit_exits']} |",
         f"| Other exits | {m['other_exits']} ({m['exit_reasons']}) |",
         f"| Exit fill vs protective level (bps, + = worse) | "
         + (", ".join(f"{s} {r} {b:+.1f}" for s, r, b in m["exit_slippage_vs_level_bps"]) or "—") + " |",
         f"| Slippage vs mid at order time (USDT) | {m['slippage_usdt']:.6f} |",
         f"| Fees (USDT) | {m['fees_usdt']:.6f} |",
         f"| Realized P&L (USDT, net of fees and funding) | {m['realized_pnl_usdt']:+.6f} |",
         f"| **Remaining open positions** | **{len(m['open_positions'])}** {m['open_positions'] or ''} |",
         f"| **Remaining pending orders** | **{len(m['pending_orders'])}** {m['pending_orders'] or ''} |",
         f"| Independent audit | {m['audit_checks'] - len(m['audit_failures'])}/{m['audit_checks']} checks passed |", ""]
    if m["rejection_reasons"]:
        L += ["Rejection reasons:", ""] + [f"* {k}: {v}" for k, v in m["rejection_reasons"]] + [""]
    return "\n".join(L)
