"""Authoritative classification of Binance hosts and of the signed endpoints
this application is allowed to call.

* Test-environment URLs must point at known TESTNET hosts; live URLs at known
  PRODUCTION hosts. Settings are validated at startup, so an environment
  variable typo can never route "testnet" orders to production.
* Credentials are only ever attached to requests for hosts of the matching
  environment (testnet keys -> testnet hosts, live keys -> production hosts).
* Signed requests are restricted to an explicit ALLOWLIST. Withdrawal,
  transfer, sub-account, lending and similar endpoints are not implemented
  and are additionally refused by pattern.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

TESTNET_HOSTS = {
    "testnet.binance.vision", "stream.testnet.binance.vision", "ws-api.testnet.binance.vision",
    "testnet.binancefuture.com", "fstream.binancefuture.com", "stream.binancefuture.com",
}
PRODUCTION_HOSTS = {
    "api.binance.com", "api1.binance.com", "api2.binance.com", "api3.binance.com", "api4.binance.com",
    "api-gcp.binance.com", "data-api.binance.vision", "data-stream.binance.vision", "stream.binance.com",
    "fapi.binance.com", "fstream.binance.com", "www.binance.com",
}

# (method, path) pairs that may be sent WITH credentials. Everything else is refused.
ALLOWED_SIGNED = {
    # spot
    ("GET", "/api/v3/account"), ("GET", "/api/v3/account/commission"), ("GET", "/api/v3/order"),
    ("POST", "/api/v3/order"), ("DELETE", "/api/v3/order"), ("GET", "/api/v3/openOrders"),
    ("GET", "/api/v3/allOrders"), ("GET", "/api/v3/myTrades"), ("POST", "/api/v3/orderList/oco"),
    ("GET", "/api/v3/orderList"), ("DELETE", "/api/v3/orderList"),
    ("GET", "/sapi/v1/account/apiRestrictions"),          # read-only permission check (live keys)
    # usd-m futures
    ("GET", "/fapi/v3/account"), ("GET", "/fapi/v3/balance"), ("GET", "/fapi/v3/positionRisk"),
    ("GET", "/fapi/v1/positionSide/dual"), ("GET", "/fapi/v1/multiAssetsMargin"), ("GET", "/fapi/v1/commissionRate"),
    ("POST", "/fapi/v1/leverage"), ("POST", "/fapi/v1/marginType"), ("GET", "/fapi/v1/order"),
    ("POST", "/fapi/v1/order"), ("DELETE", "/fapi/v1/order"), ("GET", "/fapi/v1/openOrders"),
    ("GET", "/fapi/v1/userTrades"), ("GET", "/fapi/v1/income"), ("POST", "/fapi/v1/algoOrder"),
    ("GET", "/fapi/v1/algoOrder"), ("DELETE", "/fapi/v1/algoOrder"), ("GET", "/fapi/v1/openAlgoOrders"),
    ("POST", "/fapi/v1/listenKey"), ("PUT", "/fapi/v1/listenKey"),
}
FORBIDDEN = re.compile(r"withdraw|transfer|/capital/|/asset/|sub-?account|lending|loan|/margin/|/convert|"
                       r"/pay/|/c2c/|/giftcard|/staking|/simple-earn|/futures/transfer|/wapi/", re.I)


class EndpointError(RuntimeError):
    pass


def host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def is_testnet(url: str) -> bool:
    return host(url) in TESTNET_HOSTS


def is_production(url: str) -> bool:
    return host(url) in PRODUCTION_HOSTS


def require(url: str, env: str, what: str) -> None:
    ok = is_testnet(url) if env == "testnet" else is_production(url)
    if not ok:
        raise EndpointError(f"{what} = {url!r} is not a known Binance {env.upper()} host "
                            f"({'testnet' if env == 'testnet' else 'production'} hosts only)")


def check_signed(method: str, path: str) -> None:
    if FORBIDDEN.search(path):
        raise EndpointError(f"refused: {method} {path} (withdrawal/transfer-type endpoints are never called)")
    if (method, path) not in ALLOWED_SIGNED:
        raise EndpointError(f"refused: {method} {path} is not on the signed-endpoint allowlist")
