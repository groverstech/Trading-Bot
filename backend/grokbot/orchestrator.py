"""Central orchestrator.

Lifecycle of every opportunity (one row in ``trades``, keyed by trade UID):

  SCANNED -> RUGCHECK_PASSED -> SIZED -> RISK_APPROVED
          -> [AWAITING_APPROVAL | AWAITING_LIVE_CONFIRM] -> EXECUTING -> OPEN -> CLOSED
  terminal failures: REJECTED, FAILED, EXPIRED, CANCELLED, ABANDONED

* Opportunities are inserted with a UNIQUE dedupe key
  (mode, market, symbol, strategy, timeframe, candle, direction), so the same
  signal can never be processed twice - by concurrent workers or after restart.
* Each transition is a compare-and-set on the current stage.
* Any agent exception fails only that opportunity; loops keep running.
* Rejected opportunities can never reach the Executor: execution requires
  the stage RISK_APPROVED plus a valid risk approval token.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time

from .db import dumps, loads
from .util import new_trade_id, now

log = logging.getLogger("grokbot.orchestrator")

PIPELINE = ["SCANNED", "RUGCHECK_PASSED", "SIZED", "RISK_APPROVED", "EXECUTING", "OPEN", "CLOSED"]
PRE_EXEC = ("SCANNED", "RUGCHECK_PASSED", "SIZED", "RISK_APPROVED", "AWAITING_APPROVAL", "AWAITING_LIVE_CONFIRM")


class Orchestrator:
    def __init__(self, c):
        self.c = c  # engine container
        self.stop_event = asyncio.Event()
        self.tasks: list[asyncio.Task] = []
        self._locks: dict[str, asyncio.Lock] = {}
        self.last_cycle: float | None = None
        self.next_cycle: float | None = None
        self.loop_errors: dict[str, str] = {}
        self._scan_now = asyncio.Event()

    # ------------------------------------------------------------ helpers
    def _cas(self, trade_id: str, frm: tuple[str, ...], to: str, **fields) -> bool:
        sets = "".join(f", {k}=?" for k in fields)
        status = "rejected" if to == "REJECTED" else "pending" if to in PRE_EXEC else to.lower()
        cur = self.c.db.execute(
            f"UPDATE trades SET stage=?, status=?, updated_at=?{sets} WHERE id=? AND stage IN ({','.join('?' * len(frm))})",
            (to, status, now(), *fields.values(), trade_id, *frm))
        return cur.rowcount == 1

    def trade(self, trade_id: str) -> dict | None:
        t = self.c.db.one("SELECT * FROM trades WHERE id=?", (trade_id,))
        if t:
            for k in ("signal", "rugcheck", "sizing", "risk", "execution"):
                t[k] = loads(t[k])
        return t

    def schedule_allows(self) -> bool:
        s = self.c.cfg.automation.schedule
        if not s.enabled:
            return True
        n = dt.datetime.now(dt.timezone.utc)
        if n.weekday() not in s.days:
            return False
        hm = n.strftime("%H:%M")
        return s.start_utc <= hm <= s.end_utc if s.start_utc <= s.end_utc else (hm >= s.start_utc or hm <= s.end_utc)

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        c = self.c
        c.md.set_symbols(c.cfg.scanner.spot_symbols, c.cfg.scanner.futures_symbols)
        await c.fx.refresh()
        try:
            await c.md.load_rules()
        except Exception as e:  # noqa: BLE001
            log.warning("initial rules load failed: %s", e)
        notes = await c.executor.recover()
        self._expire_stale(on_startup=True)
        if notes:
            c.state.incident("startup_recovery", "info", {"notes": notes})
        self.tasks = [
            asyncio.create_task(c.md.run(self.stop_event), name="market-data"),
            asyncio.create_task(self._fx_loop(), name="fx"),
            asyncio.create_task(self._scanner_loop(), name="scanner"),
            asyncio.create_task(self._monitor_loop(), name="monitor"),
        ]

    async def stop(self) -> None:
        self.stop_event.set()
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def _sleep(self, seconds: float, wake: asyncio.Event | None = None) -> None:
        waits = [asyncio.create_task(self.stop_event.wait())]
        if wake:
            waits.append(asyncio.create_task(wake.wait()))
        done, pending = await asyncio.wait(waits, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
        for p in pending:
            p.cancel()

    async def _fx_loop(self) -> None:
        while not self.stop_event.is_set():
            await self._sleep(1800)
            await self.c.fx.refresh()

    async def _monitor_loop(self) -> None:
        c = self.c
        while not self.stop_event.is_set():
            try:
                await c.executor.monitor()
                await c.equity.periodic(c.state.mode)
                self._expire_stale()
                c.bus.publish({"type": "tick"})
                self.loop_errors.pop("monitor", None)
            except Exception as e:  # noqa: BLE001
                log.exception("monitor loop")
                self.loop_errors["monitor"] = str(e)
            await self._sleep(2)

    async def _scanner_loop(self) -> None:
        c = self.c
        await self._sleep(5)  # let market data warm up
        while not self.stop_event.is_set():
            interval = c.cfg.scanner.interval_seconds
            self.next_cycle = time.time() + interval
            try:
                if (c.state.running and not c.state.halted and self.schedule_allows()
                        and c.cfg.automation.enabled_agents.get("scanner", True)):
                    await self.run_cycle()
                elif c.state.halted:
                    c.scanner.status, c.scanner.activity = "halted", "Trading halted - scanner paused"
                elif not c.state.running:
                    c.scanner.status, c.scanner.activity = "idle", "System stopped"
                else:
                    c.scanner.activity = "Outside trading schedule"
                self.loop_errors.pop("scanner", None)
            except Exception as e:  # noqa: BLE001
                log.exception("scanner loop")
                c.scanner.fail(e)
                self.loop_errors["scanner"] = str(e)
            self._scan_now.clear()
            await self._sleep(max(5, self.next_cycle - time.time()), self._scan_now)

    def trigger_scan(self) -> None:
        self._scan_now.set()

    def _expire_stale(self, on_startup: bool = False) -> None:
        ttl = self.c.cfg.risk.signal_ttl_seconds
        cutoff = now() - ttl
        stages = PRE_EXEC if on_startup else ("SCANNED", "RUGCHECK_PASSED", "SIZED", "RISK_APPROVED",
                                              "AWAITING_APPROVAL", "AWAITING_LIVE_CONFIRM")
        q = "UPDATE trades SET stage='EXPIRED', status='expired', updated_at=?, reject_reason=? WHERE stage IN (%s)" % \
            ",".join("?" * len(stages))
        args: list = [now(), "restart: pending signals are never resumed" if on_startup else f"signal older than {ttl:.0f}s"]
        if not on_startup:
            q += " AND created_at < ?"
            args += [*stages, cutoff]
        else:
            args += list(stages)
        n = self.c.db.execute(q, args).rowcount
        if n:
            self.c.logger.record("orchestrator", "expired", f"{n} pending opportunit{'y' if n == 1 else 'ies'} expired"
                                 + (" on restart" if on_startup else ""), None, None, "info")

    # ------------------------------------------------------------- pipeline
    async def run_cycle(self) -> None:
        c = self.c
        mode = c.state.mode
        c.equity.update(mode, "pre-scan") if not c.pf.capital_info(mode) else None
        cands = await c.scanner.scan()
        self.last_cycle = time.time()
        for cand in cands:
            tid = self.create_trade(mode, cand)
            if tid:
                await self.process(tid)

    def create_trade(self, mode: str, cand: dict) -> str | None:
        sig = cand["signal"]
        key = f"{mode}:{cand['market']}:{cand['symbol']}:{sig.strategy}:{cand['timeframe']}:{cand['candle_open']}:{sig.direction}"
        tid = new_trade_id()
        t = now()
        cur = self.c.db.execute(
            "INSERT OR IGNORE INTO trades(id,created_at,updated_at,mode,market,symbol,direction,strategy,timeframe,"
            "dedupe_key,stage,status,score,signal) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, t, t, mode, cand["market"], cand["symbol"], sig.direction, sig.strategy, cand["timeframe"], key,
             "SCANNED", "pending", sig.score, dumps(sig.as_dict())))
        if cur.rowcount != 1:
            self.c.scanner.bump("duplicates_suppressed")
            return None
        self.c.scanner.log("opportunity", f"Scanner detected {cand['symbol']} {cand['market']} {sig.direction} "
                           f"({sig.strategy}, {cand['timeframe']}, score {sig.score:.2f}): {'; '.join(sig.reasons[:3])}",
                           tid, sig.as_dict())
        return tid

    async def process(self, trade_id: str) -> None:
        lock = self._locks.setdefault(trade_id, asyncio.Lock())
        async with lock:
            try:
                await self._process(trade_id)
            except Exception as e:  # noqa: BLE001 - agent failure isolates to this trade
                log.exception("pipeline %s", trade_id)
                t = self.trade(trade_id)
                if t and t["stage"] in PRE_EXEC:
                    self._cas(trade_id, PRE_EXEC, "FAILED", reject_reason=f"agent error at {t['stage']}: {e}")
                self.c.logger.record("orchestrator", "pipeline_error", f"Pipeline error for {trade_id}: {e}",
                                     trade_id, None, "error")
        self._locks.pop(trade_id, None)

    async def _process(self, trade_id: str) -> None:
        c = self.c
        t = self.trade(trade_id)
        if not t:
            return
        mode = t["mode"]
        if t["stage"] == "SCANNED":
            if c.state.halted:
                self._cas(trade_id, ("SCANNED",), "CANCELLED", reject_reason="trading halted")
                return
            eq = c.pf.summary(mode)[t["market"]]["equity"]
            max_notional = eq * c.cfg.risk.max_position_pct / 100
            try:
                rc = await c.rugcheck.check(t, max_notional)
            except Exception as e:
                c.rugcheck.fail(e)
                raise
            if not rc["approved"]:
                self._cas(trade_id, ("SCANNED",), "REJECTED", rugcheck=dumps(rc), reject_reason=f"rugcheck: {rc['reason']}")
                return
            self._cas(trade_id, ("SCANNED",), "RUGCHECK_PASSED", rugcheck=dumps(rc))
            t = self.trade(trade_id)
        if t["stage"] == "RUGCHECK_PASSED":
            try:
                sz = await c.sizer.size(mode, t)
            except Exception as e:
                c.sizer.fail(e)
                raise
            if not sz["approved"]:
                self._cas(trade_id, ("RUGCHECK_PASSED",), "REJECTED", sizing=dumps(sz), reject_reason=f"sizer: {sz['reason']}")
                return
            self._cas(trade_id, ("RUGCHECK_PASSED",), "SIZED", sizing=dumps(sz))
            t = self.trade(trade_id)
        if t["stage"] == "SIZED":
            await self._risk(t)
            t = self.trade(trade_id)
        if t["stage"] == "RISK_APPROVED":
            if mode == "live" and not c.state.live_auth.get("first_order_confirmed"):
                self._cas(trade_id, ("RISK_APPROVED",), "AWAITING_LIVE_CONFIRM")
                c.logger.record("orchestrator", "awaiting_live_confirmation",
                                f"First real-money order {t['symbol']} held for operator confirmation", trade_id, None,
                                "warning")
                return
            if not c.cfg.automation.auto_approve:
                self._cas(trade_id, ("RISK_APPROVED",), "AWAITING_APPROVAL")
                c.logger.record("orchestrator", "awaiting_approval",
                                f"{t['symbol']} {t['direction']} awaiting manual approval (automated approval off)",
                                trade_id, None, "info")
                return
            if not c.cfg.automation.enabled_agents.get("executor", True):
                self._cas(trade_id, ("RISK_APPROVED",), "CANCELLED", reject_reason="executor disabled")
                return
            await c.executor.execute(t)

    async def _risk(self, t: dict) -> bool:
        c = self.c
        dec = await c.risk.validate(t["mode"], t, t["sizing"], c.sizer)
        token = dec.pop("token", None)
        if not dec["approved"]:
            self._cas(t["id"], ("SIZED",), "REJECTED", risk=dumps(dec), reject_reason=f"risk: {dec['reason']}")
            c.logger.record("risk", "risk_rejected", f"Risk manager REJECTED {t['symbol']}: {dec['reason']}",
                            t["id"], dec, "warning")
            return False
        self._cas(t["id"], ("SIZED",), "RISK_APPROVED", risk=dumps(dec), approval_token=token)
        c.logger.record("risk", "risk_approved", f"Risk manager approved {t['symbol']} "
                        f"({len(dec['checks'])} checks passed)", t["id"], dec, "info")
        return True

    async def _reapprove(self, trade_id: str, from_stage: str) -> dict:
        """Re-run central risk validation for a held trade (state may have changed)."""
        t = self.trade(trade_id)
        if not t or t["stage"] != from_stage:
            raise ValueError(f"trade not in {from_stage}")
        # Re-validation is measured from now; the original signal age still applies.
        if not self._cas(trade_id, (from_stage,), "SIZED"):
            raise ValueError("trade state changed")
        t = self.trade(trade_id)
        ok = await self._risk(t)
        return self.trade(trade_id) if ok else {"rejected": True, **(self.trade(trade_id) or {})}

    async def approve(self, trade_id: str, actor: str) -> dict:
        t = await self._reapprove(trade_id, "AWAITING_APPROVAL")
        if t.get("rejected"):
            return {"ok": False, "reason": t.get("reject_reason")}
        self.c.state.audit(actor, "manual_approve", {"trade_id": trade_id})
        return await self.c.executor.execute(t)

    async def confirm_first_live(self, trade_id: str, actor: str) -> dict:
        t = self.trade(trade_id)
        if not t or t["stage"] != "AWAITING_LIVE_CONFIRM":
            raise ValueError("trade is not awaiting live confirmation")
        self.c.state.confirm_first_live_order()
        t = await self._reapprove(trade_id, "AWAITING_LIVE_CONFIRM")
        if t.get("rejected"):
            return {"ok": False, "reason": t.get("reject_reason")}
        self.c.state.audit(actor, "first_live_order_confirmed", {"trade_id": trade_id})
        return await self.c.executor.execute(t)

    def reject(self, trade_id: str, actor: str) -> bool:
        ok = self._cas(trade_id, ("AWAITING_APPROVAL", "AWAITING_LIVE_CONFIRM"), "CANCELLED",
                       reject_reason=f"rejected by {actor}")
        if ok:
            self.c.state.audit(actor, "manual_reject", {"trade_id": trade_id})
        return ok

    # ------------------------------------------------------------- safety
    async def halt(self, kind: str, detail: dict, actor: str) -> dict:
        """Emergency halt: stop new entries, cancel pending entries, apply the
        emergency policy to open positions, record an incident, and require
        explicit authorization to resume."""
        c = self.c
        if c.state.halted:
            return {"already_halted": True}
        inc = c.state.set_halt(kind, detail, actor)
        n = c.db.execute(
            f"UPDATE trades SET stage='CANCELLED', status='cancelled', updated_at=?, reject_reason=? "
            f"WHERE stage IN ({','.join('?' * len(PRE_EXEC))})", (now(), f"halt: {kind}", *PRE_EXEC)).rowcount
        policy = c.cfg.risk.emergency_policy
        actions = []
        for pos in c.db.all("SELECT * FROM positions WHERE status='OPEN'"):
            if policy == "flatten":
                ok, why = c.risk.exchange_ok(pos["mode"], pos["market"])
                fresh = c.md.age(pos["market"], pos["symbol"]) <= c.cfg.risk.stale_data_seconds
                if ok and fresh:
                    r = await c.executor.close_position(pos, f"emergency_{kind}", actor)
                    actions.append({"trade_id": pos["trade_id"], "action": "closed" if r["ok"] else "close_failed",
                                    "detail": r.get("reason")})
                else:
                    actions.append({"trade_id": pos["trade_id"], "action": "kept",
                                    "detail": f"exchange unavailable or stale data - not closing blindly ({why})"})
            else:
                actions.append({"trade_id": pos["trade_id"], "action": "kept_protected",
                                "detail": "stop-loss/take-profit remain active"})
        c.logger.record("orchestrator", "emergency_halt",
                        f"TRADING HALTED ({kind}) by {actor}: {n} pending entries cancelled, policy '{policy}' applied "
                        f"to {len(actions)} position(s) (incident #{inc})", None,
                        {"detail": detail, "actions": actions}, "critical")
        for a in (c.scanner, c.rugcheck, c.sizer):
            a.status, a.activity = "halted", "Trading halted"
        c.bus.publish({"type": "halt"})
        return {"incident": inc, "cancelled": n, "actions": actions}

    async def on_breach(self, mode: str, kind: str, detail: dict) -> None:
        if not self.c.state.halted:
            await self.halt(kind, {"mode": mode, **detail}, "risk-engine")

    def resume(self, actor: str, note: str) -> None:
        c = self.c
        b = c.risk.breach_status(c.state.mode)
        c.state.clear_halt(actor, note)
        if b["daily_loss_breached"] or b["drawdown_breached"]:
            # Re-baseline so an acknowledged breach does not immediately re-trigger.
            c.db.meta_set(f"day_start:{c.state.mode}:{c.pf.day_key()}", str(c.pf.summary(c.state.mode)["total_equity"]))
            c.db.meta_set(f"peak_equity:{c.state.mode}", str(c.pf.summary(c.state.mode)["total_equity"]))
            c.state.audit(actor, "risk_baseline_reset", {"previous": b})
        for a in (c.scanner, c.rugcheck, c.sizer):
            a.status, a.activity = "idle", "Waiting"
        c.logger.record("orchestrator", "resumed", f"Trading resumed by {actor}: {note}", None, None, "warning")
