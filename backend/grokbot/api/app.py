"""HTTP API + WebSocket for the Grok Bot Terminal.

Security model:
* Single operator account; password set on first run (or GROK_ADMIN_PASSWORD).
* Session token in an HttpOnly, SameSite=Strict cookie; only its SHA-256 is stored.
* Every state-changing request must carry ``X-Grok-Request: 1`` (CSRF defence
  in addition to SameSite) and is rate limited.
* Sensitive operations (credentials, live activation, resume after halt,
  first live order) require re-entering the password.
* API secrets are write-only: never returned, never logged.
* Binds to 127.0.0.1 by default.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import analytics
from ..db import dumps, loads
from ..engine import Engine
from ..portfolio import CapitalNotInitialised
from ..security import RateLimiter
from ..settings import Settings, load_settings
from ..state import ACTIVATION_PHRASE
from ..strategy import backtest as bt
from ..strategy.strategies import STRATEGIES, STRATEGY_INFO
from . import snapshot

log = logging.getLogger("grokbot.api")
COOKIE = "grok_session"


class PasswordBody(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class ChangePassword(BaseModel):
    current: str
    new: str = Field(min_length=10, max_length=256)


class CredBody(BaseModel):
    venue: Literal["spot", "futures"]
    env: Literal["testnet", "live"]
    api_key: str = Field(min_length=16, max_length=256)
    api_secret: str = Field(min_length=16, max_length=256)
    password: str


class ModeBody(BaseModel):
    mode: Literal["paper", "testnet"]


class ReasonBody(BaseModel):
    reason: str = Field("manual emergency stop", max_length=500)


class ResumeBody(BaseModel):
    password: str
    note: str = Field(min_length=3, max_length=500)


class ActivateBody(BaseModel):
    password: str
    phrase: str


class AckBody(BaseModel):
    password: str
    ack: bool
    statement: str = Field(max_length=2000, default="")


class BacktestBody(BaseModel):
    strategies: list[str]
    market: Literal["spot", "futures"] = "spot"
    symbol: str = Field("BTCUSDT", pattern=r"^[A-Z0-9]{5,20}$")
    timeframe: Literal["5m", "15m", "1h", "4h", "1d"] = "1h"
    bars: int = Field(1500, ge=300, le=5000)


def create_app(settings: Settings | None = None, engine: Engine | None = None, start_engine: bool = True) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.engine = engine or Engine(settings)
        if start_engine:
            await app.state.engine.orch.start()
            app.state.engine.equity.update(app.state.engine.state.mode, "startup")
        yield
        if start_engine:
            await app.state.engine.close()

    app = FastAPI(title="Grok Bot Terminal", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    login_limiter = RateLimiter(5, 60)
    mutation_limiter = RateLimiter(120, 60)

    def E(request: Request) -> Engine:
        return request.app.state.engine

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/"):
            e: Engine = request.app.state.engine
            public = path in ("/api/health", "/api/auth/status", "/api/auth/login", "/api/auth/setup")
            if not public and not e.auth.validate(request.cookies.get(COOKIE)):
                return JSONResponse({"detail": "authentication required"}, status_code=401)
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                if request.headers.get("x-grok-request") != "1":
                    return JSONResponse({"detail": "missing X-Grok-Request header"}, status_code=403)
                client = request.client.host if request.client else "?"
                if not mutation_limiter.allow(client):
                    return JSONResponse({"detail": "rate limit exceeded"}, status_code=429)
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self' ws: wss:")
        return resp

    def require_password(e: Engine, pw: str) -> None:
        if not e.auth.verify_password(pw):
            e.state.audit("operator", "password_check_failed", {})
            raise HTTPException(403, "password incorrect")

    # ------------------------------------------------------------- auth
    @app.get("/api/health")
    async def health(request: Request):
        e = E(request)
        return {"ok": True, "mode": e.state.mode, "running": e.state.running, "halted": e.state.halted}

    @app.get("/api/auth/status")
    async def auth_status(request: Request):
        e = E(request)
        return {"configured": e.auth.is_configured(), "authenticated": e.auth.validate(request.cookies.get(COOKIE))}

    def _set_cookie(resp: Response, token: str, request: Request) -> None:
        resp.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=request.url.scheme == "https",
                        max_age=settings.session_hours * 3600, path="/")

    @app.post("/api/auth/setup")
    async def setup(body: PasswordBody, request: Request, response: Response):
        e = E(request)
        if e.auth.is_configured():
            raise HTTPException(409, "already configured")
        try:
            e.auth.set_password(body.password)
        except ValueError as ex:
            raise HTTPException(400, str(ex))
        e.state.audit("operator", "password_set", {})
        _set_cookie(response, e.auth.create_session(), request)
        return {"ok": True}

    @app.post("/api/auth/login")
    async def login(body: PasswordBody, request: Request, response: Response):
        e = E(request)
        client = request.client.host if request.client else "?"
        if not login_limiter.allow(client):
            raise HTTPException(429, "too many login attempts; wait a minute")
        if not e.auth.verify_password(body.password):
            e.state.audit("anonymous", "login_failed", {"client": client})
            await asyncio.sleep(0.5)
            raise HTTPException(401, "invalid password")
        e.state.audit("operator", "login", {"client": client})
        _set_cookie(response, e.auth.create_session(), request)
        return {"ok": True}

    @app.post("/api/auth/logout")
    async def logout(request: Request, response: Response):
        E(request).auth.revoke(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/auth/sessions")
    async def sessions(request: Request):
        return E(request).auth.list_sessions()

    @app.post("/api/auth/password")
    async def change_password(body: ChangePassword, request: Request, response: Response):
        e = E(request)
        require_password(e, body.current)
        e.auth.set_password(body.new)
        e.state.audit("operator", "password_changed", {})
        _set_cookie(response, e.auth.create_session(), request)
        return {"ok": True}

    # ------------------------------------------------------------ state
    @app.get("/api/state")
    async def state(request: Request):
        return snapshot.build(E(request))

    @app.get("/api/events")
    async def events(request: Request, q: str | None = None, agent: str | None = None, trade_id: str | None = None,
                     level: str | None = None, mode: str | None = None, limit: int = Query(200, le=1000),
                     before_id: int | None = None):
        return E(request).logger.search(q, agent, trade_id, mode, level, limit, before_id)

    @app.get("/api/trades")
    async def trades(request: Request, stage: str | None = None, mode: str | None = None, limit: int = Query(100, le=1000)):
        e = E(request)
        sql, args = "SELECT * FROM trades WHERE 1=1", []
        if stage:
            sql += " AND stage=?"
            args.append(stage)
        sql += " AND mode=?"
        args.append(mode or e.state.mode)
        rows = e.db.all(sql + " ORDER BY created_at DESC LIMIT ?", (*args, limit))
        return [snapshot.pipeline_row(t) for t in rows]

    @app.get("/api/trades/{trade_id}")
    async def trade_detail(trade_id: str, request: Request):
        trail = E(request).logger.trade_trail(trade_id)
        if not trail:
            raise HTTPException(404, "trade not found")
        return snapshot._clean(trail)

    @app.post("/api/trades/{trade_id}/approve")
    async def approve(trade_id: str, request: Request):
        try:
            return await E(request).orch.approve(trade_id, "operator")
        except ValueError as ex:
            raise HTTPException(409, str(ex))

    @app.post("/api/trades/{trade_id}/reject")
    async def reject(trade_id: str, request: Request):
        if not E(request).orch.reject(trade_id, "operator"):
            raise HTTPException(409, "trade is not awaiting approval")
        return {"ok": True}

    @app.post("/api/trades/{trade_id}/confirm-live")
    async def confirm_live(trade_id: str, body: PasswordBody, request: Request):
        e = E(request)
        require_password(e, body.password)
        try:
            return await e.orch.confirm_first_live(trade_id, "operator")
        except (ValueError, PermissionError) as ex:
            raise HTTPException(409, str(ex))

    @app.get("/api/positions")
    async def positions(request: Request, status: str = "CLOSED", mode: str | None = None, limit: int = Query(200, le=2000)):
        e = E(request)
        rows = e.db.all("SELECT * FROM positions WHERE mode=? AND status=? ORDER BY opened_at DESC LIMIT ?",
                        (mode or e.state.mode, status, limit))
        return snapshot._clean([snapshot.position_row(e, p) for p in rows])

    @app.post("/api/positions/{trade_id}/close")
    async def close_position(trade_id: str, request: Request):
        e = E(request)
        pos = e.db.one("SELECT * FROM positions WHERE trade_id=? AND status='OPEN'", (trade_id,))
        if not pos:
            raise HTTPException(404, "open position not found")
        e.state.audit("operator", "manual_close", {"trade_id": trade_id})
        return await e.executor.close_position(pos, "manual_close", "operator")

    @app.get("/api/orders")
    async def orders(request: Request, mode: str | None = None, limit: int = Query(200, le=2000)):
        e = E(request)
        rows = e.db.all("SELECT client_id,trade_id,mode,market,symbol,side,type,purpose,qty,status,exchange_id,"
                        "filled_qty,avg_price,fee_usdt,created_at,updated_at,error FROM orders WHERE mode=? "
                        "ORDER BY created_at DESC LIMIT ?", (mode or e.state.mode, limit))
        return rows

    @app.get("/api/fills")
    async def fills(request: Request, mode: str | None = None, limit: int = Query(200, le=2000)):
        e = E(request)
        return e.db.all("SELECT * FROM fills WHERE mode=? ORDER BY ts DESC LIMIT ?", (mode or e.state.mode, limit))

    @app.get("/api/equity")
    async def equity(request: Request, mode: str | None = None, limit: int = Query(2000, le=10000)):
        e = E(request)
        m = mode or e.state.mode
        return {"mode": m, "capital": e.pf.capital_info(m), "points": analytics.equity_curve(e.db, m, limit)}

    @app.get("/api/analytics")
    async def get_analytics(request: Request, mode: str | None = None):
        e = E(request)
        m = mode or e.state.mode
        res = analytics.compute(e.db, m, e.pf.starting_usdt(m) and float(e.pf.starting_usdt(m)))
        res["agents"] = {a.name: a.card()["metrics"] for a in e.agents}
        res["risk_engine"] = e.risk.metrics
        return snapshot._clean(res)

    @app.get("/api/klines")
    async def klines(request: Request, market: Literal["spot", "futures"], symbol: str = Query(pattern=r"^[A-Z0-9]{5,20}$"),
                     tf: Literal["1m", "5m", "15m", "1h", "4h", "1d"] = "15m", limit: int = Query(300, le=1000)):
        e = E(request)
        c = await e.md.klines(market, symbol, tf, limit)
        return {"symbol": symbol, "market": market, "tf": tf, "candles": [
            [int(c.open_time[i] / 1000), c.open[i], c.high[i], c.low[i], c.close[i], c.volume[i]] for i in range(len(c))]}

    # ----------------------------------------------------------- config
    @app.get("/api/config")
    async def get_config(request: Request):
        e = E(request)
        return {**e.cfg.as_dict(), "strategy_info": STRATEGY_INFO, "live_risk_confirmed":
                e.db.meta_get("live_risk_confirmed") == e.risk_config_hash()}

    @app.put("/api/config/{section}")
    async def put_config(section: str, request: Request):
        e = E(request)
        patch = await request.json()
        if not isinstance(patch, dict):
            raise HTTPException(400, "body must be an object")
        try:
            new = e.cfg.update(section, patch, "operator")
        except (KeyError, ValueError) as ex:
            raise HTTPException(400, str(ex))
        if section == "scanner":
            e.md.set_symbols(e.cfg.scanner.spot_symbols, e.cfg.scanner.futures_symbols)
            await e.md.load_rules()
        if section == "risk" and e.db.meta_get("live_risk_confirmed") != e.risk_config_hash():
            if e.state.live_auth.get("status") != "inactive":
                e.state.revoke_live("system", "risk configuration changed")
        return new

    @app.post("/api/capital/reallocate")
    async def reallocate(request: Request):
        e = E(request)
        try:
            moves = e.pf.reallocate(e.state.mode)
        except ValueError as ex:
            raise HTTPException(409, str(ex))
        e.state.audit("operator", "reallocate", {"mode": e.state.mode, "moves": moves})
        e.equity.update(e.state.mode, "reallocation")
        return {"moves": moves}

    # ----------------------------------------------------------- system
    @app.post("/api/system/start")
    async def start(request: Request):
        e = E(request)
        if e.state.halted:
            raise HTTPException(409, "trading is halted; resume requires explicit authorization")
        if e.state.mode == "live" and not e.state.live_authorized():
            raise HTTPException(409, "live trading not authorized")
        try:
            e.pf.ensure_initialised(e.state.mode)
        except CapitalNotInitialised as ex:
            raise HTTPException(409, str(ex))
        e.state.set_running(True, "operator")
        e.orch.trigger_scan()
        return {"ok": True}

    @app.post("/api/system/stop")
    async def stop(request: Request):
        E(request).state.set_running(False, "operator")
        return {"ok": True}

    @app.post("/api/system/scan-now")
    async def scan_now(request: Request):
        e = E(request)
        if not e.state.running:
            raise HTTPException(409, "system is stopped")
        e.orch.trigger_scan()
        return {"ok": True}

    @app.post("/api/system/mode")
    async def set_mode(body: ModeBody, request: Request):
        e = E(request)
        if e.state.running:
            raise HTTPException(409, "stop the system before changing mode")
        if body.mode == "testnet":
            missing = [m for m in e.cfg.scanner.markets if not e.vault.get_credentials(m, "testnet")]
            if missing:
                raise HTTPException(409, f"store testnet API credentials for: {', '.join(missing)}")
        e.state.set_mode(body.mode, "operator")
        e.equity.update(body.mode, "mode change") if e.fx.rate else None
        return {"mode": body.mode}

    @app.post("/api/emergency/stop")
    async def emergency_stop(body: ReasonBody, request: Request):
        e = E(request)
        e.state.set_running(False, "operator")
        res = await e.orch.halt("manual_emergency_stop", {"reason": body.reason}, "operator")
        return res

    @app.post("/api/emergency/resume")
    async def emergency_resume(body: ResumeBody, request: Request):
        e = E(request)
        require_password(e, body.password)
        if not e.state.halted:
            raise HTTPException(409, "not halted")
        e.orch.resume("operator", body.note)
        return {"ok": True}

    @app.get("/api/incidents")
    async def incidents(request: Request, limit: int = Query(100, le=1000)):
        rows = E(request).db.all("SELECT * FROM incidents ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["detail"] = loads(r["detail"])
        return rows

    @app.get("/api/audit")
    async def audit(request: Request, limit: int = Query(200, le=2000)):
        rows = E(request).db.all("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["detail"] = loads(r["detail"])
        return rows

    # ------------------------------------------------------ credentials
    @app.get("/api/credentials")
    async def list_creds(request: Request):
        return E(request).vault.list_credentials()

    @app.post("/api/credentials")
    async def store_creds(body: CredBody, request: Request):
        e = E(request)
        require_password(e, body.password)
        try:
            out = e.vault.store_credentials(body.venue, body.env, body.api_key, body.api_secret)
        except ValueError as ex:
            raise HTTPException(400, str(ex))
        e.state.audit("operator", "credentials_stored", {"venue": body.venue, "env": body.env, "hint": out["key_hint"]})
        e.reset_clients()
        return out

    @app.delete("/api/credentials/{venue}/{env}")
    async def delete_creds(venue: Literal["spot", "futures"], env: Literal["testnet", "live"], request: Request):
        e = E(request)
        e.vault.delete_credentials(venue, env)
        e.state.audit("operator", "credentials_deleted", {"venue": venue, "env": env})
        e.reset_clients()
        return {"ok": True}

    @app.post("/api/credentials/{venue}/{env}/verify")
    async def verify_creds(venue: Literal["spot", "futures"], env: Literal["testnet", "live"], request: Request):
        return await E(request).verify_credentials(venue, env)

    @app.get("/api/exchange/balances")
    async def exchange_balances(request: Request, env: Literal["testnet", "live"] = "testnet"):
        e = E(request)
        return await e.broker(env).balances()

    # ------------------------------------------------------------ live
    @app.get("/api/live/status")
    async def live_status(request: Request):
        e = E(request)
        la = e.state.live_auth
        return {"checks": e.live_checks(), "auth": {k: v for k, v in la.items() if k != "fingerprints"},
                "mode": e.state.mode, "activation_phrase": ACTIVATION_PHRASE,
                "selftest": json.loads(e.db.meta_get("safety_selftest") or "null"),
                "jurisdiction": json.loads(e.db.meta_get("live_jurisdiction_ack") or "null"),
                "log": e.db.all("SELECT * FROM live_auth_log ORDER BY id DESC LIMIT 50")}

    @app.post("/api/live/confirm-risk")
    async def confirm_risk(body: PasswordBody, request: Request):
        e = E(request)
        require_password(e, body.password)
        e.db.meta_set("live_risk_confirmed", e.risk_config_hash())
        e.state.live_log("risk_config_confirmed", {"risk": e.cfg.risk.model_dump()})
        return {"ok": True}

    @app.post("/api/live/ack-jurisdiction")
    async def ack_jurisdiction(body: AckBody, request: Request):
        e = E(request)
        require_password(e, body.password)
        e.db.meta_set("live_jurisdiction_ack", json.dumps({"ack": body.ack, "statement": body.statement, "ts": time.time()}))
        e.state.live_log("jurisdiction_ack", {"ack": body.ack, "statement": body.statement})
        return {"ok": True}

    @app.post("/api/live/selftest")
    async def selftest(request: Request):
        return await E(request).run_selftest()

    @app.post("/api/live/authorize")
    async def live_authorize(body: PasswordBody, request: Request):
        e = E(request)
        require_password(e, body.password)
        try:
            e.state.authorize_live(e.live_checks(), e.fingerprints())
        except PermissionError as ex:
            raise HTTPException(409, str(ex))
        return {"ok": True}

    @app.post("/api/live/activate")
    async def live_activate(body: ActivateBody, request: Request):
        e = E(request)
        require_password(e, body.password)
        if e.state.running:
            raise HTTPException(409, "stop the system before activating live mode")
        failed = [c["name"] for c in e.live_checks() if not c["ok"]]
        if failed:
            e.state.revoke_live("system", f"preconditions failed at activation: {failed}")
            raise HTTPException(409, f"preconditions no longer met: {', '.join(failed)}")
        try:
            e.state.activate_live(body.phrase, e.fingerprints())
        except PermissionError as ex:
            raise HTTPException(409, str(ex))
        e.equity.update("live", "live activation") if e.fx.rate else None
        return {"ok": True, "mode": "live"}

    @app.post("/api/live/revoke")
    async def live_revoke(request: Request):
        e = E(request)
        e.state.set_running(False, "operator") if e.state.mode == "live" else None
        e.state.revoke_live("operator", "manual revoke")
        return {"ok": True}

    # -------------------------------------------------------- backtest
    @app.get("/api/strategies")
    async def strategies(request: Request):
        e = E(request)
        return {n: {"info": STRATEGY_INFO[n], **e.cfg.strategies[n].model_dump()} for n in STRATEGIES}

    @app.post("/api/backtest")
    async def backtest(body: BacktestBody, request: Request):
        e = E(request)
        bad = [s for s in body.strategies if s not in STRATEGIES]
        if bad or not body.strategies:
            raise HTTPException(400, f"unknown strategies {bad}")
        c = await bt.fetch_history(e.md.adapters[body.market], body.symbol, body.timeframe, body.bars)
        if len(c) < 300:
            raise HTTPException(400, "not enough history")
        f = e.cfg.fees
        fee = f.spot_taker if body.market == "spot" else f.futures_taker
        results = []
        for s in body.strategies:
            r = await asyncio.to_thread(bt.run, c, s, e.cfg.strategies[s].params, allow_short=body.market == "futures",
                                        fee_rate=fee, slippage_bps=f.paper_extra_slippage_bps + 2,
                                        risk_pct=e.cfg.risk.risk_per_trade_pct)
            results.append(bt.safe(r))
        e.db.execute("INSERT INTO backtests(ts,params,result) VALUES(?,?,?)",
                     (time.time(), body.model_dump_json(),
                      dumps([{k: v for k, v in r.items() if k not in ("equity_curve", "trade_list")} for r in results])))
        return {"results": results, "data_source": e.md.adapters[body.market].data.base_url}

    @app.get("/api/backtests")
    async def backtests(request: Request):
        rows = E(request).db.all("SELECT * FROM backtests ORDER BY id DESC LIMIT 50")
        for r in rows:
            r["params"], r["result"] = loads(r["params"]), loads(r["result"])
        return rows

    # ------------------------------------------------------------ websocket
    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        e: Engine = websocket.app.state.engine
        if not e.auth.validate(websocket.cookies.get(COOKIE)):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        q = e.bus.subscribe()
        token = websocket.cookies.get(COOKIE)
        try:
            await websocket.send_text(json.dumps({"type": "snapshot", "data": snapshot.build(e)}, default=str))
            last = time.time()
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=1.5)
                    if msg["type"] == "event":
                        await websocket.send_text(json.dumps(msg, default=str))
                except asyncio.TimeoutError:
                    pass
                if time.time() - last >= 1.5:
                    if not e.auth.validate(token):
                        await websocket.close(code=4401)
                        return
                    await websocket.send_text(json.dumps({"type": "snapshot", "data": snapshot.build(e)}, default=str))
                    last = time.time()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            e.bus.unsubscribe(q)

    # ------------------------------------------------------------- frontend
    dist = settings.frontend_dist
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}")
        async def spa(path: str):
            f = (dist / path).resolve()
            if path and f.is_file() and dist.resolve() in f.parents:
                return FileResponse(f)
            return FileResponse(dist / "index.html")

    return app
