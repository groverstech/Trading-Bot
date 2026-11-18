"""End-of-test procedure: identify, reconcile, verify, report, clean up."""
import pytest

from conftest import candidate, run_trade
from grokbot import session_end
from test_exchange_recovery import testnet_engine


@pytest.mark.safety
async def test_report_policy_changes_nothing_and_states_exposure(engine, fake):
    await run_trade(engine, candidate("spot", "BTCUSDT"))
    rep = await session_end.finalize(engine, "paper", "report", "test")
    assert len(rep["after"]["positions"]) == 1 and not rep["actions"]
    assert "1 open position(s)" in rep["statement"]
    assert rep["protection"][0]["ok"] is True  # paper: local monitor


@pytest.mark.safety
async def test_close_all_closes_and_cancels_pending(engine, fake):
    await run_trade(engine, candidate("spot", "BTCUSDT"))
    engine.cfg.update("execution", {"entry_order_type": "limit"}, "test")
    await run_trade(engine, candidate("futures", "ETHUSDT", candle=3))   # working limit entry
    assert engine.db.scalar("SELECT COUNT(*) FROM trades WHERE stage='WORKING'") == 1
    rep = await session_end.finalize(engine, "paper", "close_all", "test")
    assert rep["after"]["positions"] == [] and rep["after"]["working_entries"] == [] and rep["after"]["pending_orders"] == []
    assert "0 open position(s), 0 working entry order(s), 0 pending order(s)" in rep["statement"]
    assert any(a["action"] == "closed" for a in rep["actions"])


@pytest.mark.safety
async def test_close_all_never_closes_blindly_on_stale_data(engine, fake):
    tid = await run_trade(engine, candidate("spot", "BTCUSDT"))
    engine.md.tickers["spot"]["BTCUSDT"].ts -= 600
    rep = await session_end.finalize(engine, "paper", "close_all", "test")
    assert rep["actions"][0]["action"] == "KEPT" and "stale" in rep["actions"][0]["detail"]
    assert engine.db.one("SELECT status FROM positions WHERE trade_id=?", (tid,))["status"] == "OPEN"
    assert "1 open position(s)" in rep["statement"]


@pytest.mark.safety
async def test_testnet_reconciliation_and_flat_statement(settings, fake):
    e = await testnet_engine(settings, fake)
    await run_trade(e, candidate("futures", "ETHUSDT", direction="SHORT"))
    fake.positions["BTCUSDT"] = 0.002          # exchange holds a position the system does not know
    rep = await session_end.finalize(e, "testnet", "report", "test")
    assert any("BTCUSDT" in i and "not tracked" in i for i in rep["reconciliation"])
    assert rep["protection"][0]["ok"]
    fake.positions["BTCUSDT"] = 0
    rep2 = await session_end.finalize(e, "testnet", "close_all", "test")
    assert rep2["after"]["positions"] == []
    assert rep2["exchange_after"]["futures_positions"] == [] and rep2["exchange_after"]["futures_open_algo"] == []
    assert "exchange reports 0 futures position(s)" in rep2["statement"]
    await e.close()


@pytest.mark.safety
async def test_paper_downtime_replay_applies_stop_touched_while_down(engine, fake):
    """Paper positions are unprotected while the process is down; on restart
    the stop that would have fired is applied from 1m candles."""
    import time
    from fake_exchange import synthetic_klines
    tid = await run_trade(engine, candidate("spot", "BTCUSDT"))
    p = engine.db.one("SELECT * FROM positions WHERE trade_id=?", (tid,))
    engine.db.execute("UPDATE positions SET opened_at=? WHERE trade_id=?", (time.time() - 3600, tid))
    engine.db.meta_set("executor_heartbeat", str(time.time() - 3600))
    now_ms = int(time.time() * 1000) // 60_000 * 60_000
    kl = synthetic_klines(60, 60000, 0, 60_000, now_ms, vol=0.0001)
    kl[30][3] = f"{float(p['stop_price']) * 0.999:.2f}"     # minute 30: low pierces the stop
    fake.klines_override[("BTCUSDT", "1m")] = kl
    notes = await engine.executor.paper_downtime_reconcile()
    q = engine.db.one("SELECT * FROM positions WHERE trade_id=?", (tid,))
    assert q["status"] == "CLOSED" and q["exit_reason"] == "stop_loss_during_downtime", notes
    assert float(q["exit_price"]) <= float(p["stop_price"])   # never better than the stop
    assert engine.db.scalar("SELECT COUNT(*) FROM incidents WHERE kind='paper_downtime_exit'") == 1
    from grokbot import audit
    assert not [c for c in audit.run(engine.db, "paper")[0] if not c["ok"]]
