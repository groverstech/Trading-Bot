import sys
from dataclasses import replace
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_exchange import FakeBinance  # noqa: E402

from grokbot.engine import Engine  # noqa: E402
from grokbot.settings import load_settings  # noqa: E402

PASSWORD = "test-password-123"


def pytest_configure(config):
    config.addinivalue_line("markers", "safety: critical safety tests required before live readiness")


@pytest.fixture
def fake():
    return FakeBinance()


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("GROK_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("GROK_ALLOW_LIVE", raising=False)
    s = load_settings()
    return replace(s, admin_password_env=PASSWORD)


async def make_engine(settings, fake, prime=True) -> Engine:
    e = Engine(settings, transport=fake.transport())
    e.md.set_symbols(["BTCUSDT", "ETHUSDT"], ["BTCUSDT", "ETHUSDT"])
    e.cfg.update("scanner", {"spot_symbols": ["BTCUSDT", "ETHUSDT"], "futures_symbols": ["BTCUSDT", "ETHUSDT"]}, "test")
    if prime:
        await e.fx.refresh()
        await e.md.load_rules()
        await e.md.refresh_spot()
        await e.md.refresh_futures(True)
        e.spot_data.health.last_ok = e.futures_data.health.last_ok = __import__("time").time()
        e.pf.ensure_initialised(e.state.mode)
    return e


@pytest_asyncio.fixture
async def engine(settings, fake):
    e = await make_engine(settings, fake)
    yield e
    await e.close()


def make_signal(direction="LONG", entry=60000.0, stop_pct=1.0, rr=3.0, strategy="breakout", score=0.7):
    from grokbot.strategy.strategies import Signal
    d = 1 if direction == "LONG" else -1
    stop = entry * (1 - d * stop_pct / 100)
    target = entry + d * rr * abs(entry - stop)
    return Signal(strategy, direction, entry, stop, target, score, ["test signal"], {})


def candidate(market="spot", symbol="BTCUSDT", tf="15m", candle=1, **kw):
    entry = kw.pop("entry", 60000.0 if symbol == "BTCUSDT" else 3000.0)
    return {"market": market, "symbol": symbol, "timeframe": tf, "candle_open": candle,
            "signal": make_signal(entry=entry, **kw)}


async def run_trade(engine, cand):
    tid = engine.orch.create_trade(engine.state.mode, cand)
    if tid:
        await engine.orch.process(tid)
    return tid
