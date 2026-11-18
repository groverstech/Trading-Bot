"""Limit-order entries: working orders, partial fills, expiry, repricing,
protection sizing, cancellation, halt and restart recovery."""
import json
import time

import pytest

from conftest import candidate, make_engine, run_trade
from grokbot.util import D, client_order_id
from test_exchange_recovery import testnet_engine


def use_limit(e, **kw):
    e.cfg.update("execution", {"entry_order_type": "limit", **kw}, "test")


def trade(e, tid):
    return e.orch.trade(tid)


def pos(e, tid):
    return e.db.one("SELECT * FROM positions WHERE trade_id=?", (tid,))


def identity(e, mode="paper"):
    s = e.pf.summary(mode)
    assert s["total_equity"] == pytest.approx(s["starting_usdt"] + s["realized_pnl"] + s["unrealized_pnl"], abs=1e-6)


async def drop_through(e, fake, sym="BTCUSDT", px=None):
    fake.prices[sym] = px or fake.prices[sym] * 0.9997  # asks now below our resting bid
    await e.md.refresh_spot()
    await e.md.refresh_futures(True)


async def test_paper_limit_rests_then_fills_as_maker(engine, fake):
    use_limit(engine)
    tid = await run_trade(engine, candidate("spot", "BTCUSDT"))
    assert trade(engine, tid)["stage"] == "WORKING"
    assert pos(engine, tid) is None  # nothing filled yet -> no position, no capital moved
    o = engine.db.one("SELECT * FROM orders WHERE trade_id=?", (tid,))
    assert o["type"] == "LIMIT" and D(o["price"]) <= D("60000")
    await drop_through(engine, fake)
    await engine.executor.monitor()
    t = trade(engine, tid)
    assert t["stage"] == "OPEN"
    p = pos(engine, tid)
    assert D(p["qty"]) == D(t["sizing"]["qty"]) and D(p["entry_price"]) == D(o["price"])
    fee = D(p["fees"])
    assert fee == pytest.approx(D(p["qty"]) * D(p["entry_price"]) * D("0.001"), rel=1e-9)  # spot maker
    identity(engine)


@pytest.mark.safety
async def test_paper_partial_fill_then_expiry_keeps_only_filled(engine, fake):
    use_limit(engine, limit_expiry_seconds=10)
    tid = await run_trade(engine, candidate("spot", "BTCUSDT"))
    target = D(trade(engine, tid)["sizing"]["qty"])
    fake.depth_qty = float(target) / 6  # only part of the order can cross
    await drop_through(engine, fake, px=60000 - 7)  # ~1 ask level below our bid
    await engine.executor.monitor()
    p = pos(engine, tid)
    assert p is not None and D(p["qty"]) < target
    assert trade(engine, tid)["stage"] == "WORKING"
    # market moves back above our bid: no more crossing liquidity, then the order expires
    await drop_through(engine, fake, px=60010)
    ex = json.loads(engine.db.one("SELECT execution FROM trades WHERE id=?", (tid,))["execution"])
    ex["first_placed_at"] -= 60
    engine.db.execute("UPDATE trades SET execution=? WHERE id=?", (json.dumps(ex), tid))
    filled_before = D(pos(engine, tid)["qty"])
    await engine.executor.monitor()
    t = trade(engine, tid)
    assert t["stage"] == "OPEN"
    assert D(pos(engine, tid)["qty"]) == filled_before and D(pos(engine, tid)["qty"]) < target
    o = engine.db.one("SELECT status FROM orders WHERE trade_id=? AND purpose='entry'", (tid,))
    assert o["status"] == "CANCELED"
    identity(engine)


async def test_paper_unfilled_limit_expires_cleanly(engine, fake):
    use_limit(engine, limit_expiry_seconds=10, max_reprices=0)
    tid = await run_trade(engine, candidate("spot", "BTCUSDT"))
    ex = json.loads(engine.db.one("SELECT execution FROM trades WHERE id=?", (tid,))["execution"])
    ex["first_placed_at"] -= 60
    engine.db.execute("UPDATE trades SET execution=? WHERE id=?", (json.dumps(ex), tid))
    before = engine.pf.summary("paper")["total_equity"]
    await engine.executor.monitor()
    assert trade(engine, tid)["stage"] == "EXPIRED"
    assert pos(engine, tid) is None
    assert engine.pf.summary("paper")["total_equity"] == pytest.approx(before)


@pytest.mark.safety
async def test_reprice_respects_chase_cap_and_never_duplicates(engine, fake):
    use_limit(engine, reprice_after_seconds=5, max_reprices=3, max_chase_bps=5)
    tid = await run_trade(engine, candidate("spot", "BTCUSDT", entry=60000.0))
    for i in range(4):
        fake.prices["BTCUSDT"] += 20  # market runs away upward
        await engine.md.refresh_spot()
        ex = json.loads(engine.db.one("SELECT execution FROM trades WHERE id=?", (tid,))["execution"])
        ex["placed_at"] -= 10
        engine.db.execute("UPDATE trades SET execution=? WHERE id=?", (json.dumps(ex), tid))
        await engine.executor.monitor()
    orders = engine.db.all("SELECT * FROM orders WHERE trade_id=? ORDER BY created_at", (tid,))
    assert len({o["client_id"] for o in orders}) == len(orders)
    live = [o for o in orders if o["status"] in ("NEW", "PARTIALLY_FILLED", "SUBMITTING")]
    assert len(live) <= 1  # never two working entry orders at once
    assert all(D(o["price"]) <= D("60000") * D("1.0005") + D("0.01") for o in orders)  # chase cap
    assert engine.executor.metrics["repriced"] >= 1


@pytest.mark.safety
async def test_stop_hit_while_working_cancels_entry_first(engine, fake):
    use_limit(engine)
    tid = await run_trade(engine, candidate("spot", "BTCUSDT"))
    target = D(trade(engine, tid)["sizing"]["qty"])
    fake.depth_qty = float(target) / 6
    await drop_through(engine, fake, px=60000 - 7)
    await engine.executor.monitor()
    assert pos(engine, tid) is not None and trade(engine, tid)["stage"] == "WORKING"
    fake.depth_qty = 50
    await drop_through(engine, fake, px=float(pos(engine, tid)["stop_price"]) * 0.999)
    await engine.executor.monitor()
    p = pos(engine, tid)
    assert p["status"] == "CLOSED" and p["exit_reason"] == "stop_loss"
    entry_orders = engine.db.all("SELECT status FROM orders WHERE trade_id=? AND purpose='entry'", (tid,))
    assert all(o["status"] in ("CANCELED", "FILLED") for o in entry_orders)
    identity(engine)


@pytest.mark.safety
async def test_halt_cancels_working_entry(engine, fake):
    use_limit(engine)
    tid = await run_trade(engine, candidate("spot", "BTCUSDT"))
    await engine.orch.halt("manual_emergency_stop", {}, "test")
    assert trade(engine, tid)["stage"] in ("CANCELLED", "EXPIRED")
    assert engine.db.one("SELECT status FROM orders WHERE trade_id=?", (tid,))["status"] == "CANCELED"


@pytest.mark.safety
async def test_testnet_spot_partial_fills_resize_oco(settings, fake):
    e = await testnet_engine(settings, fake)
    use_limit(e)
    tid = await run_trade(e, candidate("spot", "BTCUSDT"))
    assert trade(e, tid)["stage"] == "WORKING"
    cid = client_order_id(tid, "E")
    target = float(trade(e, tid)["sizing"]["qty"])
    q1 = round(target * 0.5, 5)
    fake.fill_resting(cid, q1)
    await e.executor.monitor()
    p = pos(e, tid)
    assert D(p["qty"]) == D(str(q1))
    prot = json.loads(p["protection"])
    assert prot["version"] == 1 and fake.order_lists[prot["list_client_id"]]["params"]["quantity"] == str(D(str(q1)).normalize())
    fake.fill_resting(cid, round(target - q1, 5))
    await e.executor.monitor()
    p = pos(e, tid)
    prot = json.loads(p["protection"])
    assert prot["version"] == 2 and D(p["qty"]) == D(trade(e, tid)["sizing"]["qty"])
    assert fake.order_lists[f"{tid}-P1"]["listOrderStatus"] == "ALL_DONE"  # old OCO cancelled
    assert trade(e, tid)["stage"] == "OPEN"
    await e.close()


@pytest.mark.safety
async def test_testnet_futures_partial_fill_protected_once(settings, fake):
    e = await testnet_engine(settings, fake)
    use_limit(e)
    tid = await run_trade(e, candidate("futures", "ETHUSDT"))
    cid = client_order_id(tid, "E")
    target = float(trade(e, tid)["sizing"]["qty"])
    fake.fill_resting(cid, 0.004 if target > 0.004 else target)
    await e.executor.monitor()
    assert pos(e, tid) is not None
    assert {a["type"] for a in fake.algo.values()} == {"STOP_MARKET", "TAKE_PROFIT_MARKET"}
    n_algo = len(fake.algo)
    fake.fill_resting(cid, round(target - 0.004, 3))
    await e.executor.monitor()
    assert len(fake.algo) == n_algo  # closePosition algos already cover the larger size
    await e.close()


@pytest.mark.safety
async def test_testnet_post_only_cross_is_repriced_not_taken(settings, fake):
    e = await testnet_engine(settings, fake)
    use_limit(e, limit_offset_bps=0)
    # make the computed passive price cross: tiny spread and price moves between pricing and placement
    tid = e.orch.create_trade("testnet", candidate("spot", "BTCUSDT"))
    orig = fake._place_limit

    def crossing(market, q, cid):
        fake.prices["BTCUSDT"] -= 5  # market moves down -> our bid now crosses the ask
        return orig(market, q, cid)
    fake._place_limit = crossing
    await e.orch.process(tid)
    fake._place_limit = orig
    t = trade(e, tid)
    assert t["stage"] == "WORKING"
    assert not [p for p in fake.trades if p["side"] == "BUY"]  # never filled as taker
    await e.close()


@pytest.mark.safety
async def test_restart_with_working_testnet_order_keeps_partial_and_cancels(settings, fake):
    e = await testnet_engine(settings, fake)
    use_limit(e)
    tid = await run_trade(e, candidate("spot", "BTCUSDT"))
    cid = client_order_id(tid, "E")
    target = float(trade(e, tid)["sizing"]["qty"])
    fake.fill_resting(cid, round(target / 2, 5))  # fills while we are down (not yet seen)
    await e.close()
    e2 = await make_engine(settings, fake)
    await e2.executor.recover()
    t = trade(e2, tid)
    assert t["stage"] == "OPEN"
    assert D(pos(e2, tid)["qty"]) == D(str(round(target / 2, 5)))
    assert fake.orders[cid]["status"] == "CANCELED"
    assert json.loads(pos(e2, tid)["protection"])["type"] == "spot-oco"
    await e2.close()



@pytest.mark.safety
async def test_independent_audit_after_full_lifecycles(engine, fake):
    """Closed spot TP, futures SL, partial limit fill + funding: the independent
    auditor recomputes everything from fills/ledger and must agree."""
    from grokbot import audit
    t1 = await run_trade(engine, candidate("spot", "BTCUSDT"))
    fake.prices["BTCUSDT"] = float(pos(engine, t1)["target_price"]) * 1.001
    await engine.md.refresh_spot()
    await engine.executor.monitor()
    t2 = await run_trade(engine, candidate("futures", "ETHUSDT", direction="SHORT"))
    p2 = pos(engine, t2)
    engine.executor._funding_seen["ETHUSDT"] = (int(time.time() * 1000) - 1000, 0.0003)
    engine.db.execute("UPDATE positions SET opened_at=opened_at-100 WHERE trade_id=?", (t2,))
    tk = engine.md.tickers["futures"]["ETHUSDT"]
    tk.next_funding = int(time.time() * 1000) + 8 * 3600_000
    engine.executor._paper_funding(pos(engine, t2), tk)
    fake.prices["ETHUSDT"] = float(p2["stop_price"]) * 1.001
    await engine.md.refresh_futures(True)
    await engine.executor.monitor()
    use_limit(engine)
    t3 = await run_trade(engine, candidate("spot", "ETHUSDT", candle=7))
    target = D(trade(engine, t3)["sizing"]["qty"])
    fake.depth_qty = float(target) / 5
    fake.prices["ETHUSDT"] = 3000 - 0.4
    await engine.md.refresh_spot()
    await engine.executor.monitor()
    assert pos(engine, t1)["status"] == "CLOSED" and pos(engine, t2)["status"] == "CLOSED"
    assert float(pos(engine, t2)["funding"]) > 0  # short receives positive funding
    assert pos(engine, t3) is not None
    checks, _ = audit.run(engine.db, "paper")
    bad = [c for c in checks if not c["ok"]]
    assert not bad, bad
