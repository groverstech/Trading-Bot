"""Builds the terminal state snapshot pushed to the UI."""
from __future__ import annotations

import math
import time

from ..db import loads
from ..portfolio import CapitalNotInitialised


def _clean(o):
    if isinstance(o, float):
        return None if math.isnan(o) or math.isinf(o) else o
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def pipeline_row(t: dict) -> dict:
    sig = loads(t["signal"]) or {}
    rc = loads(t["rugcheck"])
    sz = loads(t["sizing"])
    rk = loads(t["risk"])
    ex = loads(t["execution"])
    stage, reason = t["stage"], t["reject_reason"] or ""

    def st(done, failed_prefix):
        if reason.startswith(failed_prefix):
            return "rejected"
        return "approved" if done else "pending"

    order = ["SCANNED", "RUGCHECK_PASSED", "SIZED", "RISK_APPROVED", "EXECUTING", "OPEN", "CLOSED"]
    idx = order.index(stage) if stage in order else -1
    failed_exec = stage in ("FAILED", "ABANDONED") and not reason.startswith(("rugcheck", "sizer", "risk"))
    stages = {
        "scanner": "approved",
        "rugcheck": "rejected" if rc and not rc.get("approved") else ("approved" if rc and rc.get("approved") else
                                                                      st(False, "rugcheck")),
        "sizer": "rejected" if sz and not sz.get("approved") else ("approved" if sz and sz.get("approved") else "pending"),
        "risk": "rejected" if rk and not rk.get("approved") else ("approved" if rk and rk.get("approved") else "pending"),
        "executor": "failed" if failed_exec else ("approved" if idx >= order.index("OPEN") else
                                                  "working" if stage == "EXECUTING" else
                                                  "waiting" if stage.startswith("AWAITING") else "pending"),
    }
    stages["logger"] = "approved"
    stages["equity"] = "approved" if idx >= order.index("OPEN") else "pending"
    if stage in ("REJECTED", "CANCELLED", "EXPIRED") or failed_exec:
        for k, v in stages.items():
            if v == "pending" and k not in ("logger",):
                stages[k] = "skipped"
    return {
        "id": t["id"], "ts": t["created_at"], "updated": t["updated_at"], "mode": t["mode"], "market": t["market"],
        "symbol": t["symbol"], "direction": t["direction"], "strategy": t["strategy"], "timeframe": t["timeframe"],
        "score": t["score"], "stage": stage, "reason": reason, "entry": sig.get("entry"), "stop": sig.get("stop"),
        "target": sig.get("target"), "reasons": sig.get("reasons", []),
        "qty": sz.get("qty") if sz else None, "notional": sz.get("notional") if sz else None,
        "risk_usdt": sz.get("risk_usdt") if sz else None, "leverage": sz.get("leverage") if sz else None,
        "fill_price": ex.get("avg_price") if ex else None, "stages": stages,
    }


def position_row(e, p: dict) -> dict:
    u = e.pf.position_unrealized(p)
    return {
        "trade_id": p["trade_id"], "mode": p["mode"], "market": p["market"], "symbol": p["symbol"],
        "direction": p["direction"], "strategy": p["strategy"], "status": p["status"], "qty": float(p["qty"]),
        "entry": float(p["entry_price"]), "price": float(u["mark"]), "stop": float(p["stop_price"]),
        "target": float(p["target_price"]), "leverage": p["leverage"], "margin": float(p["margin"]),
        "liq_price": float(p["liq_price"]) if p["liq_price"] else None, "notional": float(u["notional"]),
        "unrealized": float(u["net"]), "unrealized_gross": float(u["gross"]), "pnl_pct": u["pnl_pct"],
        "fees": float(p["fees"]), "funding": float(p["funding"]), "opened_at": p["opened_at"],
        "closed_at": p["closed_at"], "exit_price": float(p["exit_price"]) if p["exit_price"] else None,
        "exit_reason": p["exit_reason"], "realized": float(p["realized_pnl"]),
        "price_age": round(e.md.age(p["market"], p["symbol"]), 1),
    }


def build(e) -> dict:
    mode = e.state.mode
    try:
        e.pf.ensure_initialised(mode)
    except CapitalNotInitialised:
        pass
    summary = e.pf.summary(mode)
    risk = e.risk.breach_status(mode) if summary["initialised"] else {}
    db = e.db
    closed = db.all("SELECT realized_pnl FROM positions WHERE mode=? AND status='CLOSED'", (mode,))
    wins = sum(1 for r in closed if float(r["realized_pnl"]) > 0)
    opens = db.all("SELECT * FROM positions WHERE status IN ('OPEN','CLOSING') ORDER BY opened_at DESC")
    recent_closed = db.all("SELECT * FROM positions WHERE mode=? AND status='CLOSED' ORDER BY closed_at DESC LIMIT 25", (mode,))
    pipeline = db.all("SELECT * FROM trades WHERE mode=? ORDER BY created_at DESC LIMIT 30", (mode,))
    pending_orders = db.all("SELECT * FROM orders WHERE status IN ('SUBMITTING','NEW','UNKNOWN','PARTIALLY_FILLED') "
                            "ORDER BY created_at DESC LIMIT 50")
    awaiting = db.all("SELECT * FROM trades WHERE stage IN ('AWAITING_APPROVAL','AWAITING_LIVE_CONFIRM') ORDER BY created_at")
    incidents = db.all("SELECT * FROM incidents WHERE resolved_at IS NULL AND severity IN ('critical','high') "
                       "ORDER BY id DESC LIMIT 10")
    total_trades = int(db.scalar("SELECT COUNT(*) FROM positions WHERE mode=?", (mode,)) or 0)
    md = e.md.snapshot()
    watch = []
    for m in ("spot", "futures"):
        for s, t in md[m]["tickers"].items():
            b = e.scanner.board.get(f"{m}:{s}", {})
            watch.append({**t, "market": m, "rsi": b.get("rsi"), "trend": b.get("trend"),
                          "vol_ratio": b.get("vol_ratio"), "signals": b.get("signals", [])})
    tc = {f"{env}:{m}": (lambda c: c.health.as_dict() if c else None)(e.trade_client(env, m))
          for env in ("testnet", "live") for m in ("spot", "futures")} if mode != "paper" else {}
    return _clean({
        "ts": time.time(),
        "system": {
            "running": e.state.running, "mode": mode, "halt": e.state.halt, "started_at": e.state.started_at,
            "session_started": e.state.session_started, "live_auth": {k: v for k, v in e.state.live_auth.items()
                                                                       if k != "fingerprints"},
            "allow_live_deployment": e.settings.allow_live, "next_cycle": e.orch.next_cycle,
            "last_cycle": e.orch.last_cycle, "loop_errors": e.orch.loop_errors,
            "total_trades": total_trades, "auto_approve": e.cfg.automation.auto_approve,
        },
        "portfolio": summary,
        "performance": {"wins": wins, "losses": len(closed) - wins, "closed": len(closed),
                        "win_rate": wins / len(closed) * 100 if closed else None,
                        "active_positions": sum(1 for p in opens if p["mode"] == mode)},
        "risk": {**risk, "config": e.cfg.risk.model_dump(), "engine": e.risk.metrics},
        "agents": [a.card() for a in e.agents],
        "positions": [position_row(e, p) for p in opens],
        "recent_trades": [position_row(e, p) for p in recent_closed],
        "pipeline": [pipeline_row(t) for t in pipeline],
        "awaiting": [pipeline_row(t) for t in awaiting],
        "pending_orders": pending_orders,
        "incidents": incidents,
        "market": {"spot": {k: v for k, v in md["spot"].items() if k != "tickers"},
                   "futures": {k: v for k, v in md["futures"].items() if k != "tickers"},
                   "health": {"spot_data": e.spot_data.health.as_dict(), "futures_data": e.futures_data.health.as_dict(),
                              **{f"trade:{k}": v for k, v in tc.items() if v}}},
        "watchlist": watch,
    })
