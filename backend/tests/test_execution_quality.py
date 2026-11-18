"""Exit execution quality (stop/target fills vs levels), conservative paper
exits, and verification of exchange-side protection."""
import json

import pytest

from conftest import candidate, run_trade
from grokbot.util import D
from test_exchange_recovery import testnet_engine


def pos(e, tid):
    return e.db.one("SELECT * FROM positions WHERE trade_id=?", (tid,))


@pytest.mark.safety
async def test_eth_case_mark_triggers_while_book_lags_never_better_than_stop(engine, fake):
    """Replay of the Phase-2 ETH validation trade: SHORT futures, the MARK price
    crosses the stop while the order book still trades below it. Paper must not
    credit a fill better than the stop, and must record the trigger forensics."""
    tid = await run_trade(engine, candidate("futures", "ETHUSDT", direction="SHORT"))
    p = pos(engine, tid)
    stop = D(p["stop_price"])
    await engine.md.refresh_futures(True)
    t = engine.md.tickers["futures"]["ETHUSDT"]
    t.mark = float(stop) * 1.0003          # mark beyond the stop
    t.price = float(stop) * 0.998          # traded book still 0.2% below the stop
    fake.prices["ETHUSDT"] = float(stop) * 0.998
    await engine.executor.monitor()
    p = pos(engine, tid)
    assert p["status"] == "CLOSED" and p["exit_reason"] == "stop_loss"
    assert D(p["exit_price"]) >= stop                      # not better than the stop for a short
    meta = json.loads(p["exit_meta"])
    assert meta["trigger_source"] == "mark" and D(meta["book_fill"]) < stop   # raw book fill kept for audit
    assert meta["slippage_vs_level_bps"] >= 0
    ev = engine.db.one("SELECT * FROM agent_events WHERE trade_id=? AND event='exit_execution'", (tid,))
    assert ev and "vs level" in ev["message"]


async def test_contract_price_working_type_uses_last_price(engine, fake):
    engine.cfg.update("execution", {"stop_working_type": "CONTRACT_PRICE"}, "test")
    tid = await run_trade(engine, candidate("futures", "ETHUSDT", direction="SHORT"))
    stop = D(pos(engine, tid)["stop_price"])
    await engine.md.refresh_futures(True)
    t = engine.md.tickers["futures"]["ETHUSDT"]
    t.mark, t.price = float(stop) * 1.001, float(stop) * 0.999   # mark beyond, last not
    await engine.executor.monitor()
    assert pos(engine, tid)["status"] == "OPEN"                   # last price has not reached the stop


async def test_slippage_anomaly_flagged(engine, fake):
    engine.cfg.update("execution", {"exit_slippage_alert_bps": 5}, "test")
    tid = await run_trade(engine, candidate("spot", "BTCUSDT"))
    stop = float(pos(engine, tid)["stop_price"])
    fake.prices["BTCUSDT"] = stop * 0.99                          # gap 1% through the stop
    await engine.md.refresh_spot()
    await engine.executor.monitor()
    ev = engine.db.one("SELECT * FROM agent_events WHERE trade_id=? AND event='exit_execution'", (tid,))
    assert ev["level"] == "warning" and "ANOMALY" in ev["message"]
    assert json.loads(pos(engine, tid)["exit_meta"])["slippage_vs_level_bps"] > 90


@pytest.mark.safety
async def test_verify_protection_spot_and_futures(settings, fake):
    e = await testnet_engine(settings, fake)
    ts = await run_trade(e, candidate("spot", "BTCUSDT"))
    tf = await run_trade(e, candidate("futures", "ETHUSDT", direction="SHORT"))
    b = e.broker("testnet")
    rs = await b.verify_protection(pos(e, ts))
    rf = await b.verify_protection(pos(e, tf))
    assert rs["ok"], rs
    assert rf["ok"], rf
    await e.close()


@pytest.mark.safety
async def test_missing_futures_protection_is_detected_and_replaced(settings, fake):
    e = await testnet_engine(settings, fake)
    tid = await run_trade(e, candidate("futures", "ETHUSDT", direction="SHORT"))
    fake.algo[f"{tid}-SL"]["algoStatus"] = "CANCELED"             # stop-loss vanished on the exchange
    p = pos(e, tid)
    res = await e.executor.verify_protection(p, e.broker("testnet"))
    assert res["ok"] is False and "stop-loss missing" in res["detail"]
    assert e.db.scalar("SELECT COUNT(*) FROM incidents WHERE kind='protection_missing'") == 1
    again = await e.broker("testnet").verify_protection(pos(e, tid))
    assert again["ok"], again                                      # re-placed
    prot = json.loads(pos(e, tid)["protection"])
    assert prot["version"] == 2 and prot["sl"] == f"{tid}-SL2"     # fresh client ids, no reuse
    await e.close()


@pytest.mark.safety
async def test_executed_stop_is_not_mistaken_for_missing_protection(settings, fake):
    """Regression (found by the testnet lifecycle test): after the exchange fires
    the stop, a protection check must NOT re-place orders on the flat position."""
    e = await testnet_engine(settings, fake)
    tid = await run_trade(e, candidate("futures", "ETHUSDT", direction="SHORT"))
    fake.trigger_futures_stop("ETHUSDT", tid)                    # exchange executes the stop
    res = await e.executor.verify_protection(pos(e, tid), e.broker("testnet"))   # check runs first
    assert res["ok"] is None and "closed on exchange" in res["detail"]
    assert not [k for k in fake.algo if k.endswith("SL2")]       # nothing re-placed
    assert e.db.scalar("SELECT COUNT(*) FROM incidents WHERE kind='protection_missing'") == 0
    e.executor._last_exchange_check.clear()
    await e.executor.monitor()
    p = pos(e, tid)
    assert p["status"] == "CLOSED"
    x = e.db.one("SELECT * FROM orders WHERE trade_id=? AND purpose='exit'", (tid,))
    assert x and x["type"] == "EXCHANGE_PROTECTION" and x["status"] == "FILLED"
    from grokbot import audit
    assert not [c for c in audit.run(e.db, "testnet")[0] if not c["ok"]]
    await e.close()
