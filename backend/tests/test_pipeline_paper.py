"""End-to-end paper trading through all six agents, sizing, risk and accounting."""
import time
from decimal import Decimal

import pytest

from conftest import candidate, run_trade
from grokbot.util import D


def ledger_identity(e, mode="paper"):
    s = e.pf.summary(mode)
    assert s["total_equity"] == pytest.approx(s["starting_usdt"] + s["realized_pnl"] + s["unrealized_pnl"], abs=1e-6)
    return s


@pytest.mark.safety
async def test_spot_trade_full_lifecycle(engine, fake):
    tid = await run_trade(engine, candidate("spot", "BTCUSDT"))
    t = engine.orch.trade(tid)
    assert t["stage"] == "OPEN", t["reject_reason"]
    pos = engine.db.one("SELECT * FROM positions WHERE trade_id=?", (tid,))
    # sizing respects risk per trade (0.5% of spot equity, incl. fees+slippage)
    s = ledger_identity(engine)
    assert t["sizing"]["risk_pct"] <= 0.5 + 1e-6
    assert D(pos["qty"]) * D(pos["entry_price"]) <= D(s["spot"]["equity"]) * Decimal("0.5") + 1
    # every agent left a trace under the same trade id
    agents = {r["agent"] for r in engine.db.all("SELECT agent FROM agent_events WHERE trade_id=?", (tid,))}
    assert {"scanner", "rugcheck", "sizer", "risk", "executor", "logger"} <= agents
    # price hits the take-profit -> executor closes
    fake.prices["BTCUSDT"] = float(pos["target_price"]) * 1.001
    await engine.md.refresh_spot()
    await engine.executor.monitor()
    pos = engine.db.one("SELECT * FROM positions WHERE trade_id=?", (tid,))
    assert pos["status"] == "CLOSED" and pos["exit_reason"] == "take_profit"
    assert float(pos["realized_pnl"]) > 0
    s = ledger_identity(engine)
    assert s["realized_pnl"] == pytest.approx(float(pos["realized_pnl"]))
    assert s["fees_paid"] > 0


@pytest.mark.safety
async def test_futures_short_stop_loss_and_leverage_not_capital(engine, fake):
    before = engine.pf.summary("paper")
    tid = await run_trade(engine, candidate("futures", "ETHUSDT", direction="SHORT"))
    t = engine.orch.trade(tid)
    assert t["stage"] == "OPEN", t["reject_reason"]
    s = engine.pf.summary("paper")
    # opening a leveraged position does not create equity; only the fee is paid
    assert s["total_equity"] <= before["total_equity"]
    assert s["futures"]["margin"] > 0
    assert s["futures"]["available"] == pytest.approx(s["futures"]["wallet"] - s["futures"]["margin"])
    pos = engine.db.one("SELECT * FROM positions WHERE trade_id=?", (tid,))
    fake.prices["ETHUSDT"] = float(pos["stop_price"]) * 1.001
    await engine.md.refresh_futures(True)
    await engine.executor.monitor()
    pos = engine.db.one("SELECT * FROM positions WHERE trade_id=?", (tid,))
    assert pos["status"] == "CLOSED" and pos["exit_reason"] == "stop_loss" and float(pos["realized_pnl"]) < 0
    ledger_identity(engine)
    # loss is bounded near the planned risk (plus slippage beyond the stop)
    assert -float(pos["realized_pnl"]) < t["sizing"]["risk_usdt"] * 1.5


async def test_futures_below_exchange_minimum_rejected_by_sizer(engine):
    tid = await run_trade(engine, candidate("futures", "BTCUSDT"))
    t = engine.orch.trade(tid)
    assert t["stage"] == "REJECTED" and t["reject_reason"].startswith("sizer:") and "notional" in t["reject_reason"]


@pytest.mark.safety
async def test_duplicate_signal_processed_once(engine):
    c = candidate("spot", "BTCUSDT")
    tid1 = await run_trade(engine, c)
    tid2 = await run_trade(engine, c)  # same dedupe key (same candle/strategy/direction)
    assert tid1 and tid2 is None
    assert engine.db.scalar("SELECT COUNT(*) FROM orders") == 1


@pytest.mark.safety
async def test_no_second_position_same_symbol(engine):
    await run_trade(engine, candidate("spot", "BTCUSDT", candle=1))
    tid = await run_trade(engine, candidate("spot", "BTCUSDT", candle=2, strategy="momentum"))
    t = engine.orch.trade(tid)
    assert t["stage"] == "REJECTED" and "no_duplicate" in t["reject_reason"]


@pytest.mark.safety
async def test_rugcheck_rejection_never_executes(engine, fake):
    fake.spread["BTCUSDT"] = 120.0  # 20 bps spread
    await engine.md.refresh_spot()
    tid = await run_trade(engine, candidate("spot", "BTCUSDT"))
    t = engine.orch.trade(tid)
    assert t["stage"] == "REJECTED" and t["reject_reason"].startswith("rugcheck:")
    assert engine.db.scalar("SELECT COUNT(*) FROM orders WHERE trade_id=?", (tid,)) == 0
    # even a direct executor call refuses: no approval token
    assert (await engine.executor.execute(t))["ok"] is False


@pytest.mark.safety
async def test_executor_refuses_without_valid_token(engine):
    tid = engine.orch.create_trade("paper", candidate("spot", "BTCUSDT"))
    t = engine.orch.trade(tid)
    sz = await engine.sizer.size("paper", t)
    engine.db.execute("UPDATE trades SET stage='RISK_APPROVED', sizing=?, approval_token=? WHERE id=?",
                      (__import__("json").dumps(sz), "9999999999.forged", tid))
    res = await engine.executor.execute(engine.orch.trade(tid))
    assert res["ok"] is False and "invalid" in res["reason"]
    assert engine.db.scalar("SELECT COUNT(*) FROM orders") == 0


@pytest.mark.safety
async def test_token_bound_to_quantity(engine):
    tok = engine.risk.issue_token("T1", "paper", "0.001", "59000")
    assert engine.risk.verify_token(tok, "T1", "paper", "0.001", "59000")[0]
    assert not engine.risk.verify_token(tok, "T1", "paper", "0.01", "59000")[0]
    assert not engine.risk.verify_token(tok, "T1", "live", "0.001", "59000")[0]
    exp = str(int(time.time()) - 1) + "." + tok.split(".", 1)[1]
    assert not engine.risk.verify_token(exp, "T1", "paper", "0.001", "59000")[0]


@pytest.mark.safety
async def test_stale_data_blocks_entries(engine):
    engine.md.tickers["spot"]["BTCUSDT"].ts -= 120
    tid = await run_trade(engine, candidate("spot", "BTCUSDT"))
    t = engine.orch.trade(tid)
    assert t["stage"] == "REJECTED"


@pytest.mark.safety
async def test_max_positions_enforced(engine):
    engine.cfg.update("risk", {"max_positions": 1}, "test")
    await run_trade(engine, candidate("spot", "BTCUSDT"))
    tid = await run_trade(engine, candidate("spot", "ETHUSDT"))
    assert "max_positions" in engine.orch.trade(tid)["reject_reason"]


async def test_manual_approval_flow(engine):
    engine.cfg.update("automation", {"auto_approve": False}, "test")
    tid = await run_trade(engine, candidate("spot", "BTCUSDT"))
    assert engine.orch.trade(tid)["stage"] == "AWAITING_APPROVAL"
    assert engine.db.scalar("SELECT COUNT(*) FROM orders") == 0
    res = await engine.orch.approve(tid, "test")
    assert res["ok"] and engine.orch.trade(tid)["stage"] == "OPEN"


async def test_partial_fill_on_thin_book(engine, fake):
    fake.depth_qty = 0.0003  # very thin book: 100 levels * 0.0003 BTC
    tid = await run_trade(engine, candidate("spot", "BTCUSDT"))
    t = engine.orch.trade(tid)
    # rug check rejects the thin book before any order is sent
    assert t["stage"] == "REJECTED" and "market_depth" in t["reject_reason"]


async def test_paper_partial_fill_recorded(engine, fake):
    from grokbot.exchange.broker import PaperBroker
    fake.depth_qty = 0.0001
    res = await PaperBroker(engine.md, engine.cfg).market_order("spot", "BTCUSDT", "BUY", D("0.05"), "X")
    assert res.status == "PARTIALLY_FILLED" and res.filled_qty == D("0.01")


async def test_paper_funding_applied_once(engine, fake):
    tid = await run_trade(engine, candidate("futures", "ETHUSDT", direction="LONG"))
    assert engine.orch.trade(tid)["stage"] == "OPEN"
    t = engine.md.tickers["futures"]["ETHUSDT"]
    pos = engine.db.one("SELECT * FROM positions WHERE trade_id=?", (tid,))
    engine.db.execute("UPDATE positions SET opened_at=? WHERE trade_id=?", (pos["opened_at"] - 10, tid))
    settle = int(time.time() * 1000) - 1000
    engine.executor._funding_seen["ETHUSDT"] = (settle, 0.001)
    t.next_funding = settle + 8 * 3600_000
    for _ in range(2):
        engine.executor._paper_funding(engine.db.one("SELECT * FROM positions WHERE trade_id=?", (tid,)), t)
    pos = engine.db.one("SELECT * FROM positions WHERE trade_id=?", (tid,))
    assert float(pos["funding"]) < 0  # long pays positive funding
    assert engine.db.scalar("SELECT COUNT(*) FROM ledger WHERE kind='funding'") == 1
    ledger_identity(engine)
