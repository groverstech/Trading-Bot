"""The testnet harness itself is exercised against the mock exchange so that
its signed path is known to work before it is pointed at the real testnets."""
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
    steps = await tv.run_market(market, "BTCUSDT", "K" * 64, "S" * 64)
    status = {s["step"]: s["status"] for s in steps}
    failed = {k: v for k, v in status.items() if v == "FAIL"}
    assert not failed, [s for s in steps if s["status"] == "FAIL"]
    assert any("stop-loss + take-profit" in k for k in status)
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
    rc = tv.preflight()
    out = capsys.readouterr().out
    assert rc == 1 and "GROK_TESTNET_SPOT_KEY/_SECRET: missing" in out and "GROK_PROFILE=testnet" in out
