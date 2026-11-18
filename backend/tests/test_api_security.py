import httpx
import pytest

from conftest import PASSWORD
from grokbot.api.app import create_app

H = {"X-Grok-Request": "1"}


@pytest.fixture
async def client(engine, settings):
    app = create_app(settings, engine=engine, start_engine=False)
    app.state.engine = engine
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def login(c):
    r = await c.post("/api/auth/login", json={"password": PASSWORD}, headers=H)
    assert r.status_code == 200


@pytest.mark.safety
async def test_requires_authentication(client):
    assert (await client.get("/api/state")).status_code == 401
    assert (await client.post("/api/system/start", headers=H)).status_code == 401
    assert (await client.get("/api/health")).status_code == 200


@pytest.mark.safety
async def test_wrong_password_and_csrf_header(client):
    r = await client.post("/api/auth/login", json={"password": "nope-nope-nope"}, headers=H)
    assert r.status_code == 401
    await login(client)
    assert (await client.post("/api/system/start")).status_code == 403  # missing CSRF header
    assert (await client.post("/api/system/start", headers=H)).status_code == 200


@pytest.mark.safety
async def test_credentials_are_write_only_and_password_protected(client, engine):
    await login(client)
    body = {"venue": "spot", "env": "testnet", "api_key": "K" * 64, "api_secret": "S" * 64, "password": "bad-password!"}
    assert (await client.post("/api/credentials", json=body, headers=H)).status_code == 403
    body["password"] = PASSWORD
    r = await client.post("/api/credentials", json=body, headers=H)
    assert r.status_code == 200
    listing = (await client.get("/api/credentials")).text
    assert "S" * 64 not in listing and "K" * 64 not in listing
    raw = engine.db.one("SELECT * FROM credentials")
    assert b"S" * 64 not in raw["api_secret_enc"]  # encrypted at rest
    events = engine.db.all("SELECT data, message FROM agent_events")
    audit = engine.db.all("SELECT detail FROM audit_log")
    assert all("S" * 64 not in str(x) for x in events + audit)


@pytest.mark.safety
async def test_live_cannot_be_entered_via_mode_switch(client, engine):
    await login(client)
    r = await client.post("/api/system/mode", json={"mode": "live"}, headers=H)
    assert r.status_code == 422
    with pytest.raises(PermissionError):
        engine.state.set_mode("live", "test")


@pytest.mark.safety
async def test_live_activation_blocked_without_preconditions(client, engine):
    await login(client)
    st = (await client.get("/api/live/status")).json()
    assert not all(c["ok"] for c in st["checks"])
    assert any(c["name"] == "deployment_allows_live" and not c["ok"] for c in st["checks"])
    r = await client.post("/api/live/authorize", json={"password": PASSWORD}, headers=H)
    assert r.status_code == 409
    r = await client.post("/api/live/activate", json={"password": PASSWORD, "phrase": "ACTIVATE LIVE TRADING"}, headers=H)
    assert r.status_code == 409
    assert engine.state.mode == "paper"


@pytest.mark.safety
async def test_live_requires_all_steps_even_with_flag(engine):
    engine.state.allow_live_deployment = True
    engine.settings = engine.settings.__class__(**{**engine.settings.__dict__, "allow_live": True})
    checks = engine.live_checks()
    assert not all(c["ok"] for c in checks)  # creds, tests, testnet round-trip, acks still missing
    with pytest.raises(PermissionError):
        engine.state.activate_live("ACTIVATE LIVE TRADING", engine.fingerprints())
    ok = [{"name": "x", "ok": True, "detail": ""}]
    engine.state.authorize_live(ok, engine.fingerprints())
    with pytest.raises(PermissionError):
        engine.state.activate_live("activate live", engine.fingerprints())  # wrong phrase
    engine.state.activate_live("ACTIVATE LIVE TRADING", engine.fingerprints())
    assert engine.state.mode == "live" and engine.state.live_auth["first_order_confirmed"] is False


@pytest.mark.safety
async def test_first_live_order_held_for_confirmation(engine):
    from conftest import candidate, run_trade
    engine.state.allow_live_deployment = True
    for m in ("spot", "futures"):
        engine.vault.store_credentials(m, "live", "L" * 64, "M" * 64)
    engine.reset_clients()
    engine.state.authorize_live([{"name": "x", "ok": True, "detail": ""}], engine.fingerprints())
    engine.state.activate_live("ACTIVATE LIVE TRADING", engine.fingerprints())
    engine.pf.ensure_initialised("live")
    engine.trade_client("live", "spot").health.last_ok = __import__("time").time()
    tid = await run_trade(engine, candidate("spot", "BTCUSDT"))
    t = engine.orch.trade(tid)
    assert t["stage"] == "AWAITING_LIVE_CONFIRM", t["reject_reason"]
    assert engine.db.scalar("SELECT COUNT(*) FROM orders") == 0


async def test_resume_requires_password(client, engine):
    await login(client)
    await client.post("/api/emergency/stop", json={"reason": "test"}, headers=H)
    assert engine.state.halted
    assert (await client.post("/api/system/start", headers=H)).status_code == 409
    r = await client.post("/api/emergency/resume", json={"password": "wrong-password", "note": "looks fine"}, headers=H)
    assert r.status_code == 403 and engine.state.halted
    r = await client.post("/api/emergency/resume", json={"password": PASSWORD, "note": "reviewed"}, headers=H)
    assert r.status_code == 200 and not engine.state.halted


async def test_config_validation_and_audit(client, engine):
    await login(client)
    r = await client.put("/api/config/capital", json={"spot_pct": 90}, headers=H)
    assert r.status_code == 400  # must sum to 100
    r = await client.put("/api/config/risk", json={"mandatory_stop_loss": False}, headers=H)
    assert r.status_code == 400
    r = await client.put("/api/config/risk", json={"risk_per_trade_pct": 0.75}, headers=H)
    assert r.status_code == 200
    assert engine.db.scalar("SELECT COUNT(*) FROM audit_log WHERE action='config.risk'") == 1


async def test_state_snapshot_shape(client):
    await login(client)
    s = (await client.get("/api/state")).json()
    assert {"system", "portfolio", "agents", "positions", "pipeline", "watchlist", "risk"} <= set(s)
    assert [a["name"] for a in s["agents"]] == ["scanner", "rugcheck", "sizer", "executor", "logger", "equity"]
    assert s["portfolio"]["capital"]["starting_inr"] == 50000


async def test_unknown_config_key_rejected(client):
    await login(client)
    r = await client.put("/api/config/risk", json={"max_leverag": 10}, headers=H)
    assert r.status_code == 400
