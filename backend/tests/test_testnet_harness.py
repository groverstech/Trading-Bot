"""The testnet harness itself is exercised against the mock exchange so that
its signed path is known to work before it is pointed at the real testnets."""
import json

import pytest

from fake_exchange import FakeBinance
from grokbot import testnet_validate as tv


@pytest.mark.parametrize("market", ["spot", "futures"])
async def test_harness_signed_flow_against_mock(market, monkeypatch):
    fake = FakeBinance()
    monkeypatch.setattr(tv, "TRANSPORT", fake.transport())

    async def no_stream(client, sink):  # no websocket in unit tests
        sink.append({"e": "ORDER_TRADE_UPDATE"})
    monkeypatch.setattr(tv, "_user_stream", no_stream)
    steps = await tv.run_market(market, "BTCUSDT", "K" * 64, "S" * 64, trigger_wait=0.01)
    status = {s["step"]: s["status"] for s in steps}
    failed = {k: v for k, v in status.items() if v == "FAIL"}
    assert not failed, [s for s in steps if s["status"] == "FAIL"]
    assert any("IMMEDIATE exchange-side SL/TP" in k for k in status)
    assert all("withdraw" not in c[1] for c in fake.calls)
    # duplicate client id was really rejected by the (mock) exchange, cancel confirmed
    assert any(c[0] == "DELETE" for c in fake.calls)


async def test_harness_without_keys_skips_signed_steps(monkeypatch):
    fake = FakeBinance()
    monkeypatch.setattr(tv, "TRANSPORT", fake.transport())
    steps = await tv.run_market("futures", "BTCUSDT", None, None)
    assert sum(s["status"] == "SKIPPED" for s in steps) >= 10
    assert not any(c[0] == "POST" for c in fake.calls)


async def _pipeline(monkeypatch, tmp_path, market, trigger):
    import asyncio
    monkeypatch.setenv("GROK_PROFILE", "testnet")
    monkeypatch.setenv("GROK_STREAMS", "0")
    monkeypatch.setenv("GROK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GROK_ADMIN_PASSWORD", "x" * 12)
    fake = FakeBinance()
    monkeypatch.setattr(tv, "TRANSPORT", fake.transport())

    async def fire():
        for _ in range(200):
            await asyncio.sleep(0.05)
            if trigger(fake):
                return
    task = asyncio.create_task(fire())
    steps, metrics = await tv.run_pipeline(market, "ETHUSDT" if market == "futures" else "BTCUSDT", "K" * 64, "S" * 64,
                                           direction="SHORT" if market == "futures" else "LONG",
                                           wait_minutes=0.5, db_dir=str(tmp_path / "pipe"))
    task.cancel()
    return fake, steps, metrics


@pytest.mark.safety
async def test_futures_lifecycle_exchange_stop_reconciled_and_flat(monkeypatch, tmp_path):
    def trigger(fake):
        ids = [k for k in fake.algo if k.endswith("-SL")]
        if ids and fake.positions.get("ETHUSDT"):
            fake.trigger_futures_stop("ETHUSDT", ids[0][:-3])
            return True
        return False
    fake, steps, m = await _pipeline(monkeypatch, tmp_path, "futures", trigger)
    status = {s["step"]: s["status"] for s in steps}
    assert not [s for s in steps if s["status"] == "FAIL"], [s for s in steps if s["status"] == "FAIL"]
    assert any(s["step"].startswith("8.") and "stop_loss" in s["note"] for s in steps)
    assert any(s["step"].startswith("9.") for s in steps)
    assert m["stop_loss_exits"] == 1 and m["open_positions"] == [] and m["pending_orders"] == []
    assert m["nature"].startswith("EXCHANGE")
    assert fake.positions.get("ETHUSDT", 0) == 0
    assert "7. exchange-side SL/TP present and valid (queried from the exchange)" in status


@pytest.mark.safety
async def test_spot_lifecycle_take_profit_on_exchange(monkeypatch, tmp_path):
    def trigger(fake):
        if fake.order_lists:
            lid = next(iter(fake.order_lists))
            if fake.order_lists[lid]["listOrderStatus"] == "EXECUTING":
                fake.trigger_spot_oco(lid, "tp")
                return True
        return False
    fake, steps, m = await _pipeline(monkeypatch, tmp_path, "spot", trigger)
    assert not [s for s in steps if s["status"] == "FAIL"], [s for s in steps if s["status"] == "FAIL"]
    assert m["take_profit_exits"] == 1 and m["realized_pnl_usdt"] > 0
    assert m["open_positions"] == [] and m["pending_orders"] == []


async def test_lifecycle_without_trigger_is_cleaned_up(monkeypatch, tmp_path):
    fake, steps, m = await _pipeline(monkeypatch, tmp_path, "futures", lambda f: False)
    step10 = next(s for s in steps if s["step"].startswith("10."))
    assert step10["status"] == "PASS" and "0 open position(s)" in step10["note"]
    assert fake.positions.get("ETHUSDT", 0) == 0 and not [a for a in fake.algo.values() if a["algoStatus"] == "NEW"]
    assert m["other_exits"] == 1  # closed by the end-of-test policy


def test_preflight_reports_missing_prereqs(monkeypatch, capsys):
    for k in ("GROK_TESTNET_SPOT_KEY", "GROK_TESTNET_FUTURES_KEY", "GROK_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(tv, "TRANSPORT", FakeBinance().transport())
    rc = tv.preflight(run_tests=False)
    out = capsys.readouterr().out
    assert rc == 1 and "set GROK_TESTNET_SPOT_KEY/_SECRET" in out and "FAIL  GROK_PROFILE=testnet" in out



@pytest.mark.safety
@pytest.mark.parametrize("market", ["spot", "futures"])
async def test_harness_never_leaves_exposure_and_keeps_preexisting_holdings(market, monkeypatch, tmp_path):
    """Adapter flow incl. partial probe and SL/TP trigger tests (not triggered in the
    mock) must end flat, and spot must never sell the 0.5 BTC held before the run."""
    monkeypatch.setenv("GROK_DATA_DIR", str(tmp_path))
    fake = FakeBinance()
    monkeypatch.setattr(tv, "TRANSPORT", fake.transport())

    async def no_stream(client, sink):
        sink.append({"e": "ORDER_TRADE_UPDATE"})
    monkeypatch.setattr(tv, "_user_stream", no_stream)
    steps = await tv.run_market(market, "BTCUSDT", "K" * 64, "S" * 64, trigger_wait=0.01)
    trig = [s for s in steps if s["step"].startswith("EXCHANGE-TRIGGERED")]
    assert len(trig) == 2 and all(s["response"]["flat_after"] for s in trig), trig
    assert fake.positions.get("BTCUSDT", 0) == 0
    if market == "spot":
        assert abs(fake.spot_bal["BTC"] - 0.5) < 1e-6        # pre-existing holding untouched
    assert not [o for o in fake.orders.values() if o["status"] in ("NEW", "PARTIALLY_FILLED")]
    assert not [a for a in fake.algo.values() if a.get("algoStatus") == "NEW"]


@pytest.mark.safety
async def test_harness_refuses_preexisting_futures_exposure(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_DATA_DIR", str(tmp_path))
    fake = FakeBinance()
    fake.positions["BTCUSDT"] = 0.01                       # someone's existing position
    monkeypatch.setattr(tv, "TRANSPORT", fake.transport())
    steps = await tv.run_market("futures", "BTCUSDT", "K" * 64, "S" * 64, trigger_wait=0.01)
    assert any(s["step"].startswith("baseline") and s["status"] == "FAIL" for s in steps)
    assert not [c for c in fake.calls if c[0] == "POST" and c[1] == "/fapi/v1/order"]   # nothing traded
    assert fake.positions["BTCUSDT"] == 0.01                # and nothing touched


@pytest.mark.safety
async def test_guard_cleans_interrupted_run(monkeypatch, tmp_path):
    """Simulates an interruption that left a protected-or-not position and orders:
    the guard cancels run orders and flattens only run-created exposure."""
    monkeypatch.setenv("GROK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GROK_TESTNET_FUTURES_KEY", "K" * 64)
    monkeypatch.setenv("GROK_TESTNET_FUTURES_SECRET", "S" * 64)
    monkeypatch.setenv("GROK_TESTNET_SPOT_KEY", "K" * 64)
    monkeypatch.setenv("GROK_TESTNET_SPOT_SECRET", "S" * 64)
    fake = FakeBinance()
    monkeypatch.setattr(tv, "TRANSPORT", fake.transport())
    tv.BASELINES.clear()
    tv.BASELINES.update({"futures": {"symbol": "BTCUSDT", "base0": "0", "tag": "t"},
                         "spot": {"symbol": "BTCUSDT", "base0": "0.5", "tag": "t"}})
    fake.positions["BTCUSDT"] = 0.003                        # left open by the interrupted run
    fake.algo["t-SL"] = {"symbol": "BTCUSDT", "clientAlgoId": "t-SL", "algoStatus": "NEW", "type": "STOP_MARKET"}
    fake.spot_bal["BTC"] = 0.5012                            # run bought 0.0012 BTC
    res = await tv.guard()
    assert res["futures"]["clean"] and res["spot"]["clean"], json.dumps(res)
    assert fake.positions["BTCUSDT"] == 0 and fake.algo["t-SL"]["algoStatus"] == "CANCELED"
    assert abs(fake.spot_bal["BTC"] - 0.5) < 1e-6
    assert not (tmp_path / "LEFTOVER_EXPOSURE.json").exists()
    tv.BASELINES.clear()


@pytest.mark.safety
async def test_guard_writes_leftover_marker_when_exchange_unreachable(monkeypatch, tmp_path):
    import httpx
    monkeypatch.setenv("GROK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GROK_TESTNET_FUTURES_KEY", "K" * 64)
    monkeypatch.setenv("GROK_TESTNET_FUTURES_SECRET", "S" * 64)
    monkeypatch.setattr(tv, "TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(503, text="down")))
    monkeypatch.setattr("asyncio.sleep", _nosleep)
    tv.BASELINES.clear()
    tv.BASELINES["futures"] = {"symbol": "BTCUSDT", "base0": "0", "tag": "t"}
    res = await tv.guard()
    assert res["futures"]["clean"] is False and (tmp_path / "LEFTOVER_EXPOSURE.json").exists()
    tv.BASELINES.clear()


async def _nosleep(*a, **k):
    return None
