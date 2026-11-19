"""Low-level Binance REST client shared by the Spot and Futures adapters.

Responsibilities:
* HMAC-SHA256 request signing (credentials never leave this object).
* Server time synchronisation (``timestamp`` drift protection).
* Request-weight tracking via ``X-MBX-USED-WEIGHT-1M`` and back-off on
  HTTP 429 / 418 using ``Retry-After``.
* Retry policy: idempotent GETs are retried on network errors / 5xx.
  Order-placing POSTs are NEVER blindly retried - an ambiguous outcome is
  surfaced as ``UnknownOrderState`` so the caller reconciles by client id.
* Connectivity health statistics used by the risk engine.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx

log = logging.getLogger("grokbot.exchange")


class BinanceError(Exception):
    def __init__(self, status: int, code: int | None, msg: str, path: str = ""):
        super().__init__(f"[{status}] {code}: {msg} ({path})")
        self.status, self.code, self.msg, self.path = status, code, msg, path


class UnknownOrderState(Exception):
    """Order request outcome unknown (timeout / connection drop after send)."""


class RateLimited(Exception):
    pass


@dataclass
class Health:
    last_ok: float = 0.0
    last_error: float = 0.0
    last_error_msg: str = ""
    consecutive_errors: int = 0
    latency_ms: float = 0.0
    used_weight: int = 0
    weight_limit: int = 1200
    banned_until: float = 0.0
    time_offset_ms: int = 0
    requests: int = 0
    errors: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "last_ok": self.last_ok, "last_error": self.last_error,
            "last_error_msg": self.last_error_msg, "consecutive_errors": self.consecutive_errors,
            "latency_ms": round(self.latency_ms, 1), "used_weight": self.used_weight,
            "weight_limit": self.weight_limit, "banned_until": self.banned_until,
            "time_offset_ms": self.time_offset_ms, "requests": self.requests,
            "connected": self.connected,
        }

    @property
    def connected(self) -> bool:
        return self.last_ok > 0 and self.consecutive_errors < 3 and time.time() > self.banned_until


class BinanceClient:
    def __init__(self, base_url: str, kind: str, api_key: str | None = None,
                 api_secret: str | None = None, weight_limit: int | None = None,
                 timeout: float = 10.0, transport: httpx.AsyncBaseTransport | None = None, env: str | None = None,
                 readonly: bool = False):
        assert kind in ("spot", "futures")
        self.base_url = base_url.rstrip("/")
        self.kind = kind
        if api_key or api_secret:
            # Credentials may only travel to hosts of their own environment.
            from ..endpoints import require
            if env not in ("testnet", "live"):
                raise ValueError("credentialed client requires env='testnet' or 'live'")
            require(base_url, env, f"{kind} trading endpoint")
        self.env = env
        self.readonly = readonly   # read-only clients refuse every non-GET request (used by the preflight)
        self._key = api_key
        self._secret = api_secret.encode() if api_secret else None
        self.health = Health(weight_limit=weight_limit or (6000 if kind == "spot" else 2400))
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=timeout, transport=transport,
                                       headers={"User-Agent": "grok-bot-terminal/1.0"})
        self._time_synced_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def has_credentials(self) -> bool:
        return bool(self._key and self._secret)

    @property
    def time_path(self) -> str:
        return "/api/v3/time" if self.kind == "spot" else "/fapi/v1/time"

    async def close(self) -> None:
        await self._http.aclose()

    async def sync_time(self) -> int:
        t0 = time.time()
        data = await self.request("GET", self.time_path, weight=1)
        t1 = time.time()
        local_mid = int((t0 + t1) / 2 * 1000)
        self.health.time_offset_ms = int(data["serverTime"]) - local_mid
        self._time_synced_at = t1
        return self.health.time_offset_ms

    def _timestamp(self) -> int:
        return int(time.time() * 1000) + self.health.time_offset_ms

    def _sign(self, params: dict) -> str:
        query = urlencode(params)
        sig = hmac.new(self._secret, query.encode(), hashlib.sha256).hexdigest()
        return f"{query}&signature={sig}"

    async def _respect_limits(self, weight: int) -> None:
        h = self.health
        now = time.time()
        if now < h.banned_until:
            raise RateLimited(f"rate limited until {h.banned_until:.0f}")
        # Keep a 15% headroom on the per-minute request weight.
        if h.used_weight + weight > h.weight_limit * 0.85:
            wait = 60 - (now % 60) + 0.5
            log.warning("%s weight %s/%s near limit, waiting %.1fs", self.kind, h.used_weight, h.weight_limit, wait)
            await asyncio.sleep(wait)
            h.used_weight = 0

    async def request(self, method: str, path: str, params: dict | None = None, *,
                      signed: bool = False, weight: int = 1, retries: int | None = None,
                      order: bool = False, api_key_only: bool = False) -> dict | list:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if retries is None:
            retries = 0 if (order or method != "GET") else 3
        if self.readonly and method != "GET":
            from ..endpoints import EndpointError
            raise EndpointError(f"read-only client refused {method} {path}")
        if signed or api_key_only:
            from ..endpoints import check_signed
            check_signed(method, path)
        if signed:
            if not self.has_credentials:
                raise BinanceError(0, None, "API credentials not configured", path)
            if time.time() - self._time_synced_at > 600:
                await self.sync_time()
        attempt = 0
        while True:
            await self._respect_limits(weight)
            headers = {}
            if signed:
                params["timestamp"] = self._timestamp()
                params.setdefault("recvWindow", 5000)
                body = self._sign(params)
                headers["X-MBX-APIKEY"] = self._key
            else:
                body = urlencode(params)
                if api_key_only:  # e.g. listenKey endpoints: API key header, no signature
                    if not self._key:
                        raise BinanceError(0, None, "API key not configured", path)
                    headers["X-MBX-APIKEY"] = self._key
            url = f"{path}?{body}" if body else path
            t0 = time.time()
            self.health.requests += 1
            try:
                resp = await self._http.request(method, url, headers=headers)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
                self._record_error(f"{type(e).__name__}: {e}")
                if order:
                    # The order may or may not have reached the matching engine.
                    raise UnknownOrderState(str(e)) from e
                if attempt < retries:
                    attempt += 1
                    await asyncio.sleep(min(2 ** attempt * 0.5, 8))
                    continue
                raise BinanceError(0, None, f"network error: {e}", path) from e
            self.health.latency_ms = (time.time() - t0) * 1000
            w = resp.headers.get("x-mbx-used-weight-1m") or resp.headers.get("X-MBX-USED-WEIGHT-1M")
            if w:
                self.health.used_weight = int(w)
            if resp.status_code in (418, 429):
                retry_after = float(resp.headers.get("Retry-After", "60"))
                self.health.banned_until = time.time() + retry_after
                self._record_error(f"HTTP {resp.status_code} rate limit, retry after {retry_after}s")
                raise RateLimited(f"HTTP {resp.status_code}; retry after {retry_after}s")
            if resp.status_code >= 500:
                self._record_error(f"HTTP {resp.status_code}")
                if order:
                    raise UnknownOrderState(f"HTTP {resp.status_code} from exchange")
                if attempt < retries:
                    attempt += 1
                    await asyncio.sleep(min(2 ** attempt * 0.5, 8))
                    continue
                raise BinanceError(resp.status_code, None, resp.text[:200], path)
            try:
                data = resp.json()
            except ValueError:
                data = {"msg": resp.text[:200]}
            if resp.status_code >= 400:
                code = data.get("code") if isinstance(data, dict) else None
                msg = data.get("msg", "") if isinstance(data, dict) else str(data)
                if code == -1021 and attempt < 1:  # timestamp outside recvWindow
                    await self.sync_time()
                    attempt += 1
                    continue
                # 4xx are definitive answers from the exchange, so connectivity is fine.
                self._record_ok()
                raise BinanceError(resp.status_code, code, msg, path)
            self._record_ok()
            return data

    def _record_ok(self) -> None:
        self.health.last_ok = time.time()
        self.health.consecutive_errors = 0

    def _record_error(self, msg: str) -> None:
        h = self.health
        h.last_error, h.last_error_msg = time.time(), msg
        h.consecutive_errors += 1
        log.warning("%s %s error: %s", self.kind, self.base_url, msg)
