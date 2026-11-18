"""Agent base class and in-process event bus."""
from __future__ import annotations

import asyncio
import time
from typing import Any


class EventBus:
    """Fan-out of events to WebSocket subscribers (bounded queues; slow
    consumers drop events rather than block trading)."""

    def __init__(self):
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, msg: dict) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass


class Agent:
    name = "agent"
    title = "Agent"

    def __init__(self, logger: "LoggerAgentProto | None" = None):
        self.logger = logger
        self.status = "idle"          # idle | working | error | halted | disabled
        self.activity = "Waiting"
        self.last_decision = "—"
        self.ops = 0
        self.errors = 0
        self.last_ts: float | None = None
        self.last_error: str | None = None
        self.metrics: dict[str, Any] = {}

    def busy(self, activity: str) -> None:
        self.status, self.activity, self.last_ts = "working", activity, time.time()

    def done(self, decision: str | None = None, activity: str = "Waiting") -> None:
        if decision:
            self.last_decision = decision
        self.status, self.activity, self.last_ts = "idle", activity, time.time()
        self.ops += 1

    def fail(self, err: Exception | str) -> None:
        self.errors += 1
        self.last_error = str(err)[:300]
        self.status, self.activity, self.last_ts = "error", f"Error: {self.last_error}", time.time()

    def bump(self, key: str, n: int = 1) -> None:
        self.metrics[key] = self.metrics.get(key, 0) + n

    def log(self, event: str, message: str, trade_id: str | None = None, data: Any = None,
            level: str = "info") -> None:
        if self.logger:
            self.logger.record(self.name, event, message, trade_id, data, level)

    def card(self) -> dict:
        return {"name": self.name, "title": self.title, "status": self.status, "activity": self.activity,
                "last_decision": self.last_decision, "ops": self.ops, "errors": self.errors,
                "last_ts": self.last_ts, "last_error": self.last_error, "metrics": self.metrics}


class LoggerAgentProto:  # pragma: no cover - typing helper
    def record(self, agent: str, event: str, message: str, trade_id: str | None, data: Any, level: str) -> None: ...
