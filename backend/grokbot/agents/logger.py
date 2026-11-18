"""AGENT 5 - LOGGER.

The single persistence channel for agent decisions. Every scanner signal,
rug-check verdict, sizing calculation, risk decision, order action, fill,
fee, slippage figure and P&L result is written to ``agent_events`` keyed by
the trade UID, giving a searchable audit trail that survives restarts.
Secrets are scrubbed before anything is written.
"""
from __future__ import annotations

import re
from typing import Any

from ..db import Database, dumps, loads
from ..util import now
from .base import Agent, EventBus

_SECRET_KEYS = re.compile(r"(secret|signature|api[_-]?key|password|token)", re.I)


def scrub(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("***" if _SECRET_KEYS.search(str(k)) and k != "approval_token_ok" else scrub(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    return obj


class LoggerAgent(Agent):
    name = "logger"
    title = "LOGGER"

    def __init__(self, db: Database, bus: EventBus, mode_fn):
        super().__init__(None)
        self.logger = self
        self.db, self.bus, self.mode_fn = db, bus, mode_fn
        self.metrics = {"events": int(db.scalar("SELECT COUNT(*) FROM agent_events") or 0),
                        "trades_recorded": int(db.scalar("SELECT COUNT(*) FROM trades") or 0)}

    def record(self, agent: str, event: str, message: str, trade_id: str | None = None,
               data: Any = None, level: str = "info") -> None:
        ts = now()
        data = scrub(data) if data is not None else None
        mode = self.mode_fn()
        cur = self.db.execute(
            "INSERT INTO agent_events(ts,mode,trade_id,agent,event,level,message,data) VALUES(?,?,?,?,?,?,?,?)",
            (ts, mode, trade_id, agent, event, level, message, dumps(data) if data is not None else None))
        self.metrics["events"] = self.metrics.get("events", 0) + 1
        self.ops += 1
        self.last_ts = ts
        self.last_decision = f"{agent}: {event}"
        self.activity = f"Recorded {agent}.{event}"
        msg = {"type": "event", "event": {"id": cur.lastrowid, "ts": ts, "mode": mode, "trade_id": trade_id,
                                          "agent": agent, "event": event, "level": level, "message": message}}
        self.bus.publish(msg)
        # Logger confirms its own recording of trade lifecycle milestones.
        if agent != "logger" and event in ("order_filled", "position_closed", "execution_failed"):
            self.db.execute(
                "INSERT INTO agent_events(ts,mode,trade_id,agent,event,level,message,data) VALUES(?,?,?,?,?,?,?,?)",
                (now(), mode, trade_id, "logger", "recorded", "info",
                 f"Logger persisted {event.replace('_', ' ')} record", None))
            self.metrics["trades_recorded"] = int(self.db.scalar("SELECT COUNT(*) FROM trades") or 0)
            self.bus.publish({"type": "event", "event": {"ts": now(), "mode": mode, "trade_id": trade_id,
                                                         "agent": "logger", "event": "recorded", "level": "info",
                                                         "message": f"Logger persisted {event.replace('_', ' ')} record"}})

    # --------------------------------------------------------------- queries
    def search(self, q: str | None = None, agent: str | None = None, trade_id: str | None = None,
               mode: str | None = None, level: str | None = None, limit: int = 200,
               before_id: int | None = None) -> list[dict]:
        sql, args = "SELECT * FROM agent_events WHERE 1=1", []
        if q:
            sql += " AND (message LIKE ? OR trade_id LIKE ? OR data LIKE ?)"
            args += [f"%{q}%"] * 3
        for col, val in (("agent", agent), ("trade_id", trade_id), ("mode", mode), ("level", level)):
            if val:
                sql += f" AND {col}=?"
                args.append(val)
        if before_id:
            sql += " AND id<?"
            args.append(before_id)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(min(limit, 1000))
        rows = self.db.all(sql, args)
        for r in rows:
            r["data"] = loads(r["data"])
        return rows

    def trade_trail(self, trade_id: str) -> dict:
        t = self.db.one("SELECT * FROM trades WHERE id=?", (trade_id,))
        if not t:
            return {}
        for k in ("signal", "rugcheck", "sizing", "risk", "execution"):
            t[k] = loads(t[k])
        t.pop("approval_token", None)
        return {
            "trade": t,
            "events": self.search(trade_id=trade_id, limit=500)[::-1],
            "orders": self.db.all("SELECT * FROM orders WHERE trade_id=? ORDER BY created_at", (trade_id,)),
            "fills": self.db.all("SELECT * FROM fills WHERE trade_id=? ORDER BY ts", (trade_id,)),
            "position": self.db.one("SELECT * FROM positions WHERE trade_id=?", (trade_id,)),
            "ledger": self.db.all("SELECT * FROM ledger WHERE trade_id=? ORDER BY id", (trade_id,)),
        }
