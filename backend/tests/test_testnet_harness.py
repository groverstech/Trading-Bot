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
