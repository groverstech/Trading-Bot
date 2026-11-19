"""Phase 3 readiness: testnet/production isolation, credential handling,
forbidden endpoints and live-mode lockout."""
import pytest

from grokbot.endpoints import EndpointError, check_signed
from grokbot.exchange.client import BinanceClient
from grokbot.settings import load_settings


def _env(monkeypatch, tmp_path, **kv):
    monkeypatch.setenv("GROK_DATA_DIR", str(tmp_path))
    for k in ("GROK_PROFILE", "GROK_ALLOW_LIVE", "GROK_SPOT_TESTNET_URL", "GROK_FUTURES_TESTNET_URL",
              "GROK_SPOT_DATA_URL", "GROK_FUTURES_DATA_URL", "GROK_SPOT_WS_URL", "GROK_FUTURES_WS_URL",
              "GROK_SPOT_LIVE_URL", "GROK_FUTURES_LIVE_URL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in kv.items():
        monkeypatch.setenv(k, v)


@pytest.mark.safety
@pytest.mark.parametrize("var,val", [
    ("GROK_SPOT_TESTNET_URL", "https://api.binance.com"),
    ("GROK_FUTURES_TESTNET_URL", "https://fapi.binance.com"),
    ("GROK_FUTURES_TESTNET_URL", "https://testnet.binancefuture.com.evil.example"),
    ("GROK_SPOT_LIVE_URL", "https://testnet.binance.vision"),
])
def test_misrouted_endpoints_refuse_to_start(monkeypatch, tmp_path, var, val):
    _env(monkeypatch, tmp_path, **{var: val})
    with pytest.raises(EndpointError):
        load_settings()


@pytest.mark.safety
def test_testnet_profile_isolates_all_market_data(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, GROK_PROFILE="testnet")
    s = load_settings()
    from grokbot.endpoints import is_testnet
    ep = s.endpoints
    assert all(is_testnet(u) for u in (ep.spot_data, ep.futures_data, ep.spot_ws, ep.futures_ws,
                                       ep.spot_testnet, ep.futures_testnet))
    _env(monkeypatch, tmp_path, GROK_PROFILE="testnet", GROK_SPOT_DATA_URL="https://data-api.binance.vision")
    with pytest.raises(EndpointError):
        load_settings()


@pytest.mark.safety
def test_testnet_profile_cannot_enable_live(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, GROK_PROFILE="testnet", GROK_ALLOW_LIVE="1")
    with pytest.raises(EndpointError, match="not permitted"):
        load_settings()


@pytest.mark.safety
def test_credentials_only_travel_to_matching_environment():
    with pytest.raises(EndpointError):
        BinanceClient("https://api.binance.com", "spot", "K" * 64, "S" * 64, env="testnet")
    with pytest.raises(EndpointError):
        BinanceClient("https://testnet.binance.vision", "spot", "K" * 64, "S" * 64, env="live")
    with pytest.raises(ValueError):
        BinanceClient("https://testnet.binance.vision", "spot", "K" * 64, "S" * 64)  # env must be explicit
    BinanceClient("https://testnet.binance.vision", "spot", "K" * 64, "S" * 64, env="testnet")


@pytest.mark.safety
@pytest.mark.parametrize("method,path", [
    ("POST", "/sapi/v1/capital/withdraw/apply"), ("POST", "/sapi/v1/asset/transfer"),
    ("POST", "/sapi/v1/futures/transfer"), ("POST", "/sapi/v1/sub-account/transfer/subToMaster"),
    ("POST", "/sapi/v1/margin/loan"), ("GET", "/api/v3/rateLimit/order"),
])
def test_forbidden_and_unlisted_signed_endpoints_refused(method, path):
    with pytest.raises(EndpointError):
        check_signed(method, path)


@pytest.mark.safety
async def test_client_refuses_withdrawal_before_any_network_call():
    import httpx
    calls = []
    c = BinanceClient("https://testnet.binance.vision", "spot", "K" * 64, "S" * 64, env="testnet",
                      transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200, json={})))
    with pytest.raises(EndpointError):
        await c.request("POST", "/sapi/v1/capital/withdraw/apply", {"coin": "USDT"}, signed=True)
    assert calls == []
    await c.close()


@pytest.mark.safety
def test_codebase_has_no_withdrawal_or_transfer_calls():
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parents[1] / "grokbot"
    hits = []
    for f in root.rglob("*.py"):
        if f.name == "endpoints.py":
            continue
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if re.search(r'["\']/(sapi|wapi)/v\d/(capital|asset|sub-account|futures/transfer|margin)', line):
                hits.append(f"{f.name}:{i}")
    assert hits == []


@pytest.mark.safety
async def test_same_key_cannot_be_stored_as_testnet_and_live(engine):
    engine.vault.store_credentials("spot", "live", "L" * 64, "M" * 64)
    with pytest.raises(ValueError, match="must be different"):
        engine.vault.store_credentials("spot", "testnet", "L" * 64, "Z" * 64)


@pytest.mark.safety
async def test_testnet_profile_never_builds_live_client(engine):
    from dataclasses import replace
    engine.vault.store_credentials("spot", "live", "L" * 64, "M" * 64)
    engine.settings = replace(engine.settings, profile="testnet")
    engine.reset_clients()
    assert engine.trade_client("live", "spot") is None
