"""Process-level settings, read once from environment variables.

Runtime-tunable trading configuration (risk limits, strategies, allocation...)
lives in the database via ``config_store`` so every change can be audited.
Settings here are deployment concerns: paths, endpoints, network binding and
the deployment-level live-trading kill switch.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Endpoints:
    """Base URLs for each Binance venue/environment.

    Market data (public) and trading (signed) URLs are kept separate because
    Binance publishes a market-data-only mirror for Spot (data-api.binance.vision)
    that is usable from more locations than api.binance.com.
    """

    spot_data: str
    spot_live: str
    spot_testnet: str
    futures_data: str
    futures_live: str
    futures_testnet: str
    spot_ws: str = "wss://data-stream.binance.vision"
    futures_ws: str = "wss://fstream.binance.com"
    futures_ws_testnet: str = "wss://fstream.binancefuture.com"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    db_path: Path
    host: str
    port: int
    endpoints: Endpoints
    fx_url: str
    # Deployment-level kill switch. Unless this is exactly "1", live mode can
    # never be armed, regardless of what is stored in the database.
    allow_live: bool
    master_key_env: str | None
    admin_password_env: str | None
    session_hours: int
    frontend_dist: Path
    futures_data_is_testnet: bool = field(default=False)
    enable_streams: bool = True
    session_idle_minutes: int = 120
    profile: str = ""
    cookie_secure: bool = False


def load_settings() -> Settings:
    root = Path(__file__).resolve().parents[2]
    data_dir = Path(_env("GROK_DATA_DIR", str(root / "data")))
    data_dir.mkdir(parents=True, exist_ok=True)
    # GROK_PROFILE=testnet points ALL market data and streams at the Binance test
    # environments, so testnet orders are priced from testnet books.
    testnet = _env("GROK_PROFILE", "") == "testnet"
    if testnet:
        os.environ.setdefault("GROK_SPOT_DATA_URL", "https://testnet.binance.vision")
        os.environ.setdefault("GROK_SPOT_WS_URL", "wss://stream.testnet.binance.vision")
        os.environ.setdefault("GROK_FUTURES_DATA_URL", "https://testnet.binancefuture.com")
        os.environ.setdefault("GROK_FUTURES_WS_URL", "wss://fstream.binancefuture.com")
    futures_data = _env("GROK_FUTURES_DATA_URL", "https://fapi.binance.com")
    s = Settings(
        data_dir=data_dir,
        db_path=Path(_env("GROK_DB_PATH", str(data_dir / "grokbot.db"))),
        host=_env("GROK_HOST", "127.0.0.1"),
        port=int(_env("GROK_PORT", "8800")),
        endpoints=Endpoints(
            spot_data=_env("GROK_SPOT_DATA_URL", "https://data-api.binance.vision"),
            spot_live=_env("GROK_SPOT_LIVE_URL", "https://api.binance.com"),
            spot_testnet=_env("GROK_SPOT_TESTNET_URL", "https://testnet.binance.vision"),
            futures_data=futures_data,
            futures_live=_env("GROK_FUTURES_LIVE_URL", "https://fapi.binance.com"),
            futures_testnet=_env("GROK_FUTURES_TESTNET_URL", "https://testnet.binancefuture.com"),
            spot_ws=_env("GROK_SPOT_WS_URL", "wss://data-stream.binance.vision"),
            futures_ws=_env("GROK_FUTURES_WS_URL", "wss://fstream.binancefuture.com" if "testnet" in futures_data
                            else "wss://fstream.binance.com"),
            futures_ws_testnet=_env("GROK_FUTURES_TESTNET_WS_URL", "wss://fstream.binancefuture.com"),
        ),
        enable_streams=_env("GROK_STREAMS", "1") == "1",
        fx_url=_env("GROK_FX_URL", "https://open.er-api.com/v6/latest/USD"),
        allow_live=_env("GROK_ALLOW_LIVE", "0") == "1",
        profile=_env("GROK_PROFILE", ""),
        master_key_env=os.environ.get("GROK_MASTER_KEY"),
        admin_password_env=os.environ.get("GROK_ADMIN_PASSWORD"),
        session_hours=int(_env("GROK_SESSION_HOURS", "12")),
        session_idle_minutes=int(_env("GROK_SESSION_IDLE_MINUTES", "120")),
        cookie_secure=_env("GROK_COOKIE_SECURE", "0") == "1",
        frontend_dist=Path(_env("GROK_FRONTEND_DIST", str(root / "frontend" / "dist"))),
        futures_data_is_testnet=__import__("grokbot.endpoints", fromlist=["is_testnet"]).is_testnet(futures_data),
    )
    return validate(s)


def validate(s: Settings) -> Settings:
    """Refuse to start with an unsafe endpoint / mode combination."""
    from .endpoints import EndpointError, is_testnet, require
    ep = s.endpoints
    require(ep.spot_testnet, "testnet", "GROK_SPOT_TESTNET_URL")
    require(ep.futures_testnet, "testnet", "GROK_FUTURES_TESTNET_URL")
    require(ep.futures_ws_testnet, "testnet", "GROK_FUTURES_TESTNET_WS_URL")
    require(ep.spot_live, "live", "GROK_SPOT_LIVE_URL")
    require(ep.futures_live, "live", "GROK_FUTURES_LIVE_URL")
    if s.profile == "testnet":
        for name, url in (("GROK_SPOT_DATA_URL", ep.spot_data), ("GROK_FUTURES_DATA_URL", ep.futures_data),
                          ("GROK_SPOT_WS_URL", ep.spot_ws), ("GROK_FUTURES_WS_URL", ep.futures_ws)):
            require(url, "testnet", name)
        if s.allow_live:
            raise EndpointError("GROK_ALLOW_LIVE=1 is not permitted with GROK_PROFILE=testnet")
    for name, url in (("GROK_SPOT_DATA_URL", ep.spot_data), ("GROK_FUTURES_DATA_URL", ep.futures_data)):
        if not (is_testnet(url) or require_quiet(url)):
            raise EndpointError(f"{name} = {url!r} is not a known Binance host")
    return s


def require_quiet(url: str) -> bool:
    from .endpoints import is_production
    return is_production(url)
