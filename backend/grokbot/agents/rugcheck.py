"""AGENT 2 - RUG CHECK.

Validates every Scanner opportunity before any capital is considered.
Each check yields {name, ok, value, limit, detail}; one failure rejects the
opportunity and the full check list is persisted as the explanation.

Note: for Binance-listed USDT pairs there is no reliable on-chain token
data source in this system, so "token risk" is assessed through market
microstructure (liquidity, depth, spread, abnormal moves, volume anomalies)
and exchange trading status.
"""
from __future__ import annotations

import time

import numpy as np

from ..config_store import ConfigStore
from ..market.data import MarketData, depth_within
from ..strategy import indicators as ind
from .base import Agent


class RugCheckAgent(Agent):
    name = "rugcheck"
    title = "RUG CHECK"

    def __init__(self, logger, md: MarketData, cfg: ConfigStore):
        super().__init__(logger)
        self.md, self.cfg = md, cfg
        self.metrics = {"checked": 0, "approved": 0, "rejected": 0, "top_rejections": {}}

    async def check(self, trade: dict, max_order_notional: float) -> dict:
        rc, risk = self.cfg.rugcheck, self.cfg.risk
        market, symbol, sig = trade["market"], trade["symbol"], trade["signal"]
        self.busy(f"Validating {symbol} ({market})")
        checks: list[dict] = []

        def add(name, ok, value=None, limit=None, detail=""):
            checks.append({"name": name, "ok": bool(ok), "value": value, "limit": limit, "detail": detail})

        rules = self.md.rules[market].get(symbol)
        add("exchange_status", rules is not None and rules.trading, rules.status if rules else "unknown", "TRADING",
            "symbol must be in TRADING status")
        t = self.md.ticker(market, symbol)
        age = self.md.age(market, symbol)
        add("data_fresh", t is not None and age <= risk.stale_data_seconds, round(age, 1), risk.stale_data_seconds,
            "ticker age (s)")
        if t is None:
            return self._verdict(trade, checks)
        add("liquidity_24h", t.quote_volume >= rc.min_quote_volume_24h, round(t.quote_volume),
            rc.min_quote_volume_24h, "24h quote volume (USDT)")
        add("spread", t.spread_bps <= rc.max_spread_bps, round(t.spread_bps, 2), rc.max_spread_bps, "bid-ask spread (bps)")
        add("abnormal_24h_move", abs(t.change_pct) <= rc.max_24h_change_pct, round(t.change_pct, 2),
            rc.max_24h_change_pct, "24h price change (%)")
        try:
            book = await self.md.depth(market, symbol, 100)
            mid = (float(book["bids"][0][0]) + float(book["asks"][0][0])) / 2
            side = "ask" if sig["direction"] == "LONG" else "bid"
            levels = book["asks"] if side == "ask" else book["bids"]
            depth = depth_within(levels, mid, rc.depth_band_pct, side)
            need = max_order_notional * rc.min_depth_multiple
            add("market_depth", depth >= need, round(depth), round(need),
                f"{side} liquidity within ±{rc.depth_band_pct}% vs {rc.min_depth_multiple:g}x max order notional")
            # Book imbalance can flag spoofing / one-sided books.
            bid_d = depth_within(book["bids"], mid, rc.depth_band_pct, "bid")
            ask_d = depth_within(book["asks"], mid, rc.depth_band_pct, "ask")
            imb = max(bid_d, ask_d) / max(min(bid_d, ask_d), 1e-9)
            add("book_imbalance", imb <= 20, round(imb, 1), 20, "max/min side depth ratio")
        except Exception as e:  # noqa: BLE001
            add("market_depth", False, None, None, f"order book unavailable: {e}")
        try:
            c = await self.md.klines(market, symbol, trade["timeframe"], 300)
            a = ind.atr(c.high, c.low, c.close, 14)
            last_rng = c.high[-1] - c.low[-1]
            mult = last_rng / a[-2] if a[-2] and not np.isnan(a[-2]) else 0
            add("abnormal_candle", mult <= rc.max_candle_atr_multiple, round(float(mult), 2),
                rc.max_candle_atr_multiple, "last candle range / ATR")
            z = ind.zscore(c.volume, 50)
            add("volume_anomaly", z <= rc.max_volume_zscore, round(z, 2), rc.max_volume_zscore,
                "volume z-score (extreme spikes can indicate manipulation)")
            gap_ms = int(time.time() * 1000) - int(c.close_time[-1])
            tf_ms = int(c.close_time[-1] - c.open_time[-1]) + 1
            add("candle_data_fresh", gap_ms <= tf_ms + 60_000, round(gap_ms / 1000), round((tf_ms + 60_000) / 1000),
                "seconds since last closed candle")
            zero_vol = int((c.volume[-50:] == 0).sum())
            add("trading_continuity", zero_vol == 0, zero_vol, 0, "zero-volume candles in last 50")
        except Exception as e:  # noqa: BLE001
            add("candle_data", False, None, None, f"klines unavailable: {e}")

        if market == "futures":
            fr = t.funding_rate
            add("funding_rate", abs(fr) <= rc.max_abs_funding_rate, round(fr * 100, 4),
                round(rc.max_abs_funding_rate * 100, 4), "funding rate (% per interval)")
            # Paying funding against the position direction is a cost; flag extreme adverse funding.
            adverse = (fr > 0 and sig["direction"] == "LONG") or (fr < 0 and sig["direction"] == "SHORT")
            if adverse:
                add("funding_cost", abs(fr) <= rc.max_abs_funding_rate / 2, round(fr * 100, 4),
                    round(rc.max_abs_funding_rate * 50, 4), "adverse funding (% per interval)")
            dev = abs(t.mark - t.price) / t.price * 100 if t.price else 999
            add("mark_last_deviation", dev <= rc.max_mark_last_deviation_pct, round(dev, 3),
                rc.max_mark_last_deviation_pct, "mark vs traded price deviation (%)")
            oi = await self.md.open_interest(symbol)
            add("open_interest", oi is not None and oi > 0, oi, ">0", "open interest available")
            # Liquidation risk at max allowed leverage (isolated, approx. 0.5% maintenance margin).
            lev = risk.max_leverage
            entry, stop = sig["entry"], sig["stop"]
            liq = entry * (1 - 1 / lev + 0.005) if sig["direction"] == "LONG" else entry * (1 + 1 / lev - 0.005)
            buffer = ((stop - liq) / entry * 100) if sig["direction"] == "LONG" else ((liq - stop) / entry * 100)
            add("liquidation_buffer", buffer >= rc.min_liquidation_buffer_pct, round(buffer, 2),
                rc.min_liquidation_buffer_pct, f"distance stop→liquidation at {lev}x (% of price)")
        return self._verdict(trade, checks)

    def _verdict(self, trade: dict, checks: list[dict]) -> dict:
        failed = [c for c in checks if not c["ok"]]
        approved = not failed
        self.metrics["checked"] += 1
        self.metrics["approved" if approved else "rejected"] += 1
        for f in failed:
            tr = self.metrics["top_rejections"]
            tr[f["name"]] = tr.get(f["name"], 0) + 1
        reason = None if approved else "; ".join(
            f"{c['name']} ({c['detail']}: {c['value']} vs limit {c['limit']})" for c in failed)
        sym = trade["symbol"]
        self.log("approved" if approved else "rejected",
                 f"Rug Check {'approved' if approved else 'REJECTED'} {sym} {trade['market']}"
                 + ("" if approved else f": {failed[0]['name']} - {failed[0]['detail']}"),
                 trade["id"], {"checks": checks}, "info" if approved else "warning")
        self.done(f"{'APPROVED' if approved else 'REJECTED'} {sym}" + ("" if approved else f" ({failed[0]['name']})"))
        return {"approved": approved, "checks": checks, "reason": reason}
