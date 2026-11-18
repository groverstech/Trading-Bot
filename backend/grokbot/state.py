"""Persistent system state: operating mode, run state, trading halt, incidents
and the live-trading authorization workflow.

Live trading activation is a three-step, audited process:
  1. AUTHORIZE  - operator password + all preconditions satisfied
                  (deployment flag, verified live API keys with withdrawals
                  disabled, confirmed risk configuration, passing safety
                  self-test, jurisdiction/tax acknowledgement).
  2. ACTIVATE   - separate confirmation phrase switches mode to LIVE.
  3. FIRST ORDER- the first real-money order is held until separately
                  confirmed by the operator.
Authorization is revoked automatically on every process start, on credential
change, if the deployment flag is removed, or on demand. The system never
switches into LIVE by itself.
"""
from __future__ import annotations

import json
import secrets
from typing import Any

from .db import Database, dumps
from .util import now

MODES = ("paper", "testnet", "live")
ACTIVATION_PHRASE = "ACTIVATE LIVE TRADING"


class SystemState:
    def __init__(self, db: Database, allow_live_deployment: bool):
        self.db = db
        self.allow_live_deployment = allow_live_deployment
        self.boot_id = secrets.token_hex(8)
        if not db.meta_get("install_id"):
            db.meta_set("install_id", secrets.token_hex(12))
        self.started_at = now()
        self.session_started = None
        # Fresh process: live authorization never survives a restart.
        la = self.live_auth
        if la.get("status") != "inactive":
            self._set("live_auth", {"status": "inactive", "first_order_confirmed": False})
            self.live_log("revoked", {"reason": "process restart / redeployment"})
        if self.mode == "live":
            # Positions stay monitored, but no new live entries until re-authorized.
            self._set("mode", "paper")
            self.live_log("mode_reset", {"reason": "restart while in LIVE mode; reverted to PAPER"})
        # The bot never auto-starts trading after a restart.
        if self.running:
            self._set("running", False)
            self.audit("system", "auto_stop_on_restart", {})

    # ------------------------------------------------------------ helpers
    def _get(self, key: str, default: Any) -> Any:
        raw = self.db.meta_get(f"state:{key}")
        return json.loads(raw) if raw is not None else default

    def _set(self, key: str, value: Any) -> None:
        self.db.meta_set(f"state:{key}", json.dumps(value))

    def audit(self, actor: str, action: str, detail: Any) -> None:
        self.db.execute("INSERT INTO audit_log(ts,actor,action,detail) VALUES(?,?,?,?)",
                        (now(), actor, action, dumps(detail)))

    def live_log(self, event: str, detail: Any) -> None:
        self.db.execute("INSERT INTO live_auth_log(ts,event,detail) VALUES(?,?,?)", (now(), event, dumps(detail)))
        self.audit("live-auth", event, detail)

    # --------------------------------------------------------------- mode
    @property
    def mode(self) -> str:
        return self._get("mode", "paper")

    def set_mode(self, mode: str, actor: str) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode}")
        if mode == "live":
            raise PermissionError("LIVE mode can only be entered through the live activation workflow")
        old = self.mode
        self._set("mode", mode)
        self.audit(actor, "mode_change", {"from": old, "to": mode})
        if old == "live":
            self.revoke_live(actor, "mode changed away from LIVE")

    @property
    def running(self) -> bool:
        return self._get("running", False)

    def set_running(self, running: bool, actor: str) -> None:
        self._set("running", running)
        self.session_started = now() if running else None
        self.audit(actor, "start" if running else "stop", {"mode": self.mode})

    # --------------------------------------------------------------- halt
    @property
    def halt(self) -> dict:
        return self._get("halt", {"halted": False})

    @property
    def halted(self) -> bool:
        return bool(self.halt.get("halted"))

    def set_halt(self, kind: str, detail: dict, actor: str) -> int:
        inc = self.incident(kind, "critical", detail)
        self._set("halt", {"halted": True, "kind": kind, "detail": detail, "ts": now(), "incident_id": inc, "by": actor})
        self.audit(actor, "halt", {"kind": kind, "detail": detail, "incident": inc})
        return inc

    def clear_halt(self, actor: str, note: str) -> None:
        h = self.halt
        if h.get("incident_id"):
            self.db.execute("UPDATE incidents SET resolved_at=?, resolved_by=? WHERE id=?",
                            (now(), f"{actor}: {note}", h["incident_id"]))
        self._set("halt", {"halted": False, "last": h, "resumed_at": now(), "resumed_by": actor})
        self.audit(actor, "resume_after_halt", {"previous": h, "note": note})

    def incident(self, kind: str, severity: str, detail: dict) -> int:
        cur = self.db.execute("INSERT INTO incidents(ts,mode,kind,severity,detail) VALUES(?,?,?,?,?)",
                              (now(), self.mode, kind, severity, dumps(detail)))
        return int(cur.lastrowid)

    # ---------------------------------------------------------- live auth
    @property
    def live_auth(self) -> dict:
        return self._get("live_auth", {"status": "inactive", "first_order_confirmed": False})

    def live_authorized(self) -> bool:
        return self.live_auth.get("status") == "active" and self.allow_live_deployment

    def authorize_live(self, checks: list[dict], fingerprints: dict) -> None:
        failed = [c for c in checks if not c["ok"]]
        if failed:
            raise PermissionError("preconditions not met: " + "; ".join(c["name"] for c in failed))
        self._set("live_auth", {"status": "authorized", "authorized_at": now(), "boot_id": self.boot_id,
                                "fingerprints": fingerprints, "first_order_confirmed": False})
        self.live_log("authorized", {"checks": checks, "fingerprints": fingerprints})

    def activate_live(self, phrase: str, fingerprints: dict) -> None:
        la = self.live_auth
        if la.get("status") != "authorized" or la.get("boot_id") != self.boot_id:
            raise PermissionError("live trading must be authorized first (in this process)")
        if phrase.strip() != ACTIVATION_PHRASE:
            raise PermissionError(f"confirmation phrase must be exactly: {ACTIVATION_PHRASE}")
        if la.get("fingerprints") != fingerprints:
            raise PermissionError("API credentials changed since authorization")
        if not self.allow_live_deployment:
            raise PermissionError("deployment does not allow live trading (GROK_ALLOW_LIVE != 1)")
        self._set("live_auth", {**la, "status": "active", "activated_at": now()})
        old = self.mode
        self._set("mode", "live")
        self.audit("operator", "mode_change", {"from": old, "to": "live"})
        self.live_log("activated", {"mode_from": old})

    def confirm_first_live_order(self) -> None:
        la = self.live_auth
        if la.get("status") != "active":
            raise PermissionError("live trading is not active")
        self._set("live_auth", {**la, "first_order_confirmed": True, "first_order_confirmed_at": now()})
        self.live_log("first_order_confirmed", {})

    def revoke_live(self, actor: str, reason: str) -> None:
        if self.live_auth.get("status") == "inactive" and self.mode != "live":
            return
        self._set("live_auth", {"status": "inactive", "first_order_confirmed": False})
        if self.mode == "live":
            self._set("mode", "paper")
        self.live_log("revoked", {"by": actor, "reason": reason})

    def check_live_integrity(self, fingerprints: dict) -> None:
        la = self.live_auth
        if la.get("status") in ("authorized", "active"):
            if not self.allow_live_deployment:
                self.revoke_live("system", "deployment flag GROK_ALLOW_LIVE removed")
            elif la.get("fingerprints") != fingerprints:
                self.revoke_live("system", "API credentials changed")
            elif la.get("boot_id") != self.boot_id:
                self.revoke_live("system", "authorization from a previous process")
