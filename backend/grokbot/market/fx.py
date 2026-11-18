"""USD/INR conversion for INR display and the ₹ capital configuration.

Source: open.er-api.com (ExchangeRate-API open access, updated daily).
Binance global has no USDT/INR market, so USDT is treated as 1 USD for the
conversion; this assumption and the rate timestamp are shown in the UI.
The last good rate is persisted so restarts do not lose it.
"""
from __future__ import annotations

import json
import logging
import time

import httpx

from ..db import Database

log = logging.getLogger("grokbot.fx")


class FxService:
    def __init__(self, db: Database, url: str, transport: httpx.AsyncBaseTransport | None = None):
        self.db = db
        self.url = url
        self._transport = transport
        saved = db.meta_get("fx_usd_inr")
        self.state: dict = json.loads(saved) if saved else {
            "rate": None, "source": "open.er-api.com", "provider_updated": None, "fetched_at": None, "error": None}

    @property
    def rate(self) -> float | None:
        return self.state.get("rate")

    async def refresh(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=10, transport=self._transport) as c:
                r = await c.get(self.url)
                r.raise_for_status()
                data = r.json()
            rate = float(data["rates"]["INR"])
            if not 30 < rate < 300:  # sanity bound
                raise ValueError(f"implausible USD/INR rate {rate}")
            self.state = {
                "rate": rate, "source": "open.er-api.com (USD/INR; USDT assumed = 1 USD)",
                "provider_updated": data.get("time_last_update_unix"), "fetched_at": time.time(), "error": None,
            }
            self.db.meta_set("fx_usd_inr", json.dumps(self.state))
        except Exception as e:  # noqa: BLE001
            self.state = {**self.state, "error": str(e)[:200]}
            log.warning("FX refresh failed: %s", e)

    def usdt_to_inr(self, usdt: float) -> float | None:
        return usdt * self.rate if self.rate else None
