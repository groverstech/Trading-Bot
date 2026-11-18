"""Exchange (testnet) execution, reconciliation, restart recovery and emergency halt."""
import pytest

from conftest import candidate, make_engine, run_trade
from grokbot.util import client_order_id

KEY, SECRET = "A" * 64, "B" * 64


async def testnet_engine(settings, fake):
    e = await make_engine(settings, fake)
    for m in ("spot", "futures"):
        e.vault.store_credentials(m, "testnet", KEY, SECRET)
    e.reset_clients()
    e.state.set_mode("testnet", "test")
    e.pf.ensure_initialised("testnet")
    return e


@pytest.mark.safety
async def test_testnet_spot_entry_places_oco_protection(settings, fake):
    e = await testnet_engine(settings, fake)
    tid = await run_trade(e, candidate("spot", "BTCUSDT"))
    t = e.orch.trade(tid)
    assert t["stage"] == "OPEN", t["reject_reason"]
    posts = [c for c in fake.calls if c[0] == "POST"]
    assert any(p[1] == "/api/v3/order" and p[2]["newClientOrderId"] == client_order_id(tid, "E") for p in posts)
    oco = [p for p in posts if p[1] == "/api/v3/orderList/oco"]
    assert oco and oco[0][2]["aboveType"] == "LIMIT_MAKER" and oco[0][2]["belowType"] == "STOP_LOSS_LIMIT"
    assert all("withdraw" not in c[1] for c in fake.calls)
    await e.close()


@pytest.mark.safety
async def test_testnet_futures_uses_algo_conditional_orders(settings, fake):
    e = await testnet_engine(settings, fake)
    tid = await run_trade(e, candidate("futures", "ETHUSDT", direction="SHORT"))
    assert e.orch.trade(tid)["stage"] == "OPEN"
    assert {a["type"] for a in fake.algo.values()} == {"STOP_MARKET", "TAKE_PROFIT_MARKET"}
    assert all(a["closePosition"] == "true" and a["side"] == "BUY" for a in fake.algo.values())
    assert not any(c[1] == "/fapi/v1/order" and c[2].get("type") in ("STOP_MARKET", "TAKE_PROFIT_MARKET")
                   for c in fake.calls)
    # exchange-side stop fires -> reconciled into a closed position
    fake.trigger_futures_stop("ETHUSDT", tid)
    e.executor._last_exchange_check.clear()
    await e.executor.monitor()
    pos = e.db.one("SELECT * FROM positions WHERE trade_id=?", (tid,))
    assert pos["status"] == "CLOSED"
    await e.close()


@pytest.mark.safety
async def test_order_timeout_reconciled_never_resubmitted(settings, fake, monkeypatch):
    e = await testnet_engine(settings, fake)
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    fake.fail_next_order_timeout = True
    tid = await run_trade(e, candidate("spot", "BTCUSDT"))
    assert e.orch.trade(tid)["stage"] == "OPEN"
    entry_posts = [c for c in fake.calls if c[0] == "POST" and c[1] == "/api/v3/order"]
    assert len(entry_posts) == 1  # found by client id, not sent twice
    await e.close()


async def _no_sleep(*_a, **_k):
    return None


async def test_exchange_rejection_marks_trade_failed(settings, fake):
    e = await testnet_engine(settings, fake)
    fake.reject_next_order = (-2010, "Account has insufficient balance for requested action.")
    tid = await run_trade(e, candidate("spot", "BTCUSDT"))
    t = e.orch.trade(tid)
    assert t["stage"] == "FAILED" and "insufficient balance" in t["reject_reason"]
    assert e.db.scalar("SELECT COUNT(*) FROM positions") == 0
    await e.close()


async def test_hedge_mode_account_refused(settings, fake):
    e = await testnet_engine(settings, fake)
    fake.hedge_mode = True
    tid = await run_trade(e, candidate("futures", "ETHUSDT"))
    t = e.orch.trade(tid)
    assert t["stage"] == "FAILED" and "Hedge Mode" in t["reject_reason"]
    await e.close()


@pytest.mark.safety
async def test_restart_recovers_filled_order_and_abandons_unsent(settings, fake):
    e = await testnet_engine(settings, fake)
    # Trade A: order reached the exchange, then the process died before recording it.
    ta = e.orch.create_trade("testnet", candidate("spot", "BTCUSDT"))
    a = e.orch.trade(ta)
    sz = await e.sizer.size("testnet", a)
    import json
    e.db.execute("UPDATE trades SET stage='EXECUTING', sizing=? WHERE id=?", (json.dumps(sz), ta))
    cid = client_order_id(ta, "E")
    e.db.execute("INSERT INTO orders(client_id,trade_id,mode,market,symbol,side,type,purpose,qty,status,created_at,updated_at)"
                 " VALUES(?,?,?,?,?,?,?,?,?,?,0,0)", (cid, ta, "testnet", "spot", "BTCUSDT", "BUY", "MARKET", "entry", sz["qty"], "SUBMITTING"))
    fake._fill("spot", "BTCUSDT", "BUY", float(sz["qty"]), cid)
    # Trade B: interrupted before submission. Trade C: approved but not yet executing.
    tb = e.orch.create_trade("testnet", candidate("spot", "ETHUSDT"))
    e.db.execute("UPDATE trades SET stage='EXECUTING' WHERE id=?", (tb,))
    tc = e.orch.create_trade("testnet", candidate("futures", "ETHUSDT", candle=9))
    e.db.execute("UPDATE trades SET stage='RISK_APPROVED' WHERE id=?", (tc,))
    e.state.set_running(True, "test")
    await e.close()

    e2 = Engine2 = await make_engine(settings, fake)
    assert e2.state.running is False and e2.state.mode == "testnet"
    await e2.executor.recover()
    e2.orch._expire_stale(on_startup=True)
    assert e2.orch.trade(ta)["stage"] == "OPEN"
    assert e2.orch.trade(tb)["stage"] == "ABANDONED"
    assert e2.orch.trade(tc)["stage"] == "EXPIRED"
    # recovery located the order by client id; nothing was (re)submitted
    assert len([c for c in fake.calls if c[0] == "POST" and c[1] == "/api/v3/order"]) == 0
    await Engine2.close()


@pytest.mark.safety
async def test_live_authorization_revoked_on_restart(settings, fake):
    e = await make_engine(settings, fake)
    e.state._set("live_auth", {"status": "active", "first_order_confirmed": True})
    e.state._set("mode", "live")
    await e.close()
    e2 = await make_engine(settings, fake, prime=False)
    assert e2.state.mode == "paper" and e2.state.live_auth["status"] == "inactive"
    assert e2.db.scalar("SELECT COUNT(*) FROM live_auth_log WHERE event='revoked'") >= 1
    await e2.close()


@pytest.mark.safety
async def test_emergency_halt_keep_protected(engine, fake):
    tid = await run_trade(engine, candidate("spot", "BTCUSDT"))
    engine.cfg.update("automation", {"auto_approve": False}, "test")
    pending = await run_trade(engine, candidate("spot", "ETHUSDT"))
    assert engine.orch.trade(pending)["stage"] == "AWAITING_APPROVAL"
    res = await engine.orch.halt("manual_emergency_stop", {}, "test")
    assert res["cancelled"] == 1 and res["actions"][0]["action"] == "kept_protected"
    assert engine.orch.trade(pending)["stage"] == "CANCELLED"
    assert engine.db.one("SELECT status FROM positions WHERE trade_id=?", (tid,))["status"] == "OPEN"
    # no new trades while halted
    t3 = await run_trade(engine, candidate("spot", "ETHUSDT", candle=5))
    assert engine.orch.trade(t3)["stage"] == "CANCELLED"
    assert engine.db.scalar("SELECT COUNT(*) FROM incidents WHERE kind='manual_emergency_stop'") == 1


@pytest.mark.safety
async def test_emergency_flatten_does_not_close_blindly_when_exchange_down(engine, fake):
    tid = await run_trade(engine, candidate("spot", "BTCUSDT"))
    engine.cfg.update("risk", {"emergency_policy": "flatten"}, "test")
    engine.md.tickers["spot"]["BTCUSDT"].ts -= 300  # stale data / outage
    res = await engine.orch.halt("manual_emergency_stop", {}, "test")
    assert res["actions"][0]["action"] == "kept"
    assert engine.db.one("SELECT status FROM positions WHERE trade_id=?", (tid,))["status"] == "OPEN"


@pytest.mark.safety
async def test_emergency_flatten_closes_when_exchange_up(engine, fake):
    tid = await run_trade(engine, candidate("spot", "BTCUSDT"))
    engine.cfg.update("risk", {"emergency_policy": "flatten"}, "test")
    res = await engine.orch.halt("manual_emergency_stop", {}, "test")
    assert res["actions"][0]["action"] == "closed"
    assert engine.db.one("SELECT status FROM positions WHERE trade_id=?", (tid,))["status"] == "CLOSED"


@pytest.mark.safety
async def test_daily_loss_breach_triggers_halt(engine, fake):
    tid = await run_trade(engine, candidate("spot", "BTCUSDT"))
    assert engine.orch.trade(tid)["stage"] == "OPEN"
    engine.pf.risk_state("paper")  # establishes day-start equity
    # simulate a large realized loss posted to the ledger
    engine.pf.post("paper", "spot", "trade", -20, None, "test loss")  # ~3.4% of equity
    await engine.equity.periodic("paper")
    assert engine.state.halted and engine.state.halt["kind"] == "daily_loss_limit"
    engine.orch.resume("test", "acknowledged")
    assert not engine.state.halted
    await engine.equity.periodic("paper")
    assert not engine.state.halted  # baseline reset after explicit resume


@pytest.mark.safety
async def test_market_data_outage_blocks_trading(engine, fake, monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    fake.fail_market_data = True
    for _ in range(3):
        try:
            await engine.md.refresh_spot()
        except Exception:
            pass
    tid = await run_trade(engine, candidate("spot", "BTCUSDT"))
    assert engine.orch.trade(tid)["stage"] in ("REJECTED", "FAILED")
    assert engine.db.scalar("SELECT COUNT(*) FROM orders") == 0
