"""AGENT 3 - SIZER.

Deterministic position sizing (Decimal arithmetic, no AI involvement):

  equity_m        = current equity of the market's allocation (spot or futures)
  risk_budget     = equity_m * risk_per_trade_pct
  unit_loss       = |entry - stop| + entry * (2 * taker_fee + est_slippage)
  qty_risk        = risk_budget / unit_loss
  vol_factor      = min(1, median ATR% (100 bars) / current ATR%)
  corr_factor     = 0.5 if a correlated same-direction position is open, else 1
  qty             = qty_risk * vol_factor * corr_factor, then capped by
                    max_position_pct, available cash (spot) or
                    available margin * max_leverage (futures), then rounded
                    DOWN to the exchange step size.
  leverage        = smallest integer <= max_leverage such that
                    margin = notional / leverage fits available futures cash.

Rejects if the result is below exchange minimum quantity / notional, if the
net reward/risk after costs is below the configured minimum, or if the
liquidation price would be hit before the stop.
"""
from __future__ import annotations

import math
from decimal import Decimal

import numpy as np

from ..config_store import ConfigStore
from ..market.data import MarketData
from ..portfolio import Portfolio
from ..strategy import indicators as ind
from ..util import D, ZERO
from .base import Agent

MAINT_MARGIN_RATE = Decimal("0.005")  # conservative tier-1 approximation for liquidation estimate


def liquidation_price(entry: Decimal, leverage: int, direction: str, mmr: Decimal = MAINT_MARGIN_RATE,
                      maint_amount: Decimal = Decimal(0)) -> Decimal:
    """Isolated-margin liquidation price for a single one-way position, from
    Binance's formula LP = (WB + cum - side*Q*EP) / (Q*MMR - side*Q) with the
    isolated wallet balance WB = Q*EP/L (fees and accrued funding ignored):

        long : LP = EP * (1 - 1/L) / (1 - MMR)      (+ cum/Q terms if cum > 0)
        short: LP = EP * (1 + 1/L) / (1 + MMR)

    MMR defaults to 0.5%, at or above Binance's lowest bracket for the default
    pairs, which places the estimate slightly closer to entry (conservative).
    Binance's own figure is authoritative."""
    inv = D(1) / D(leverage)
    if direction == "LONG":
        return (entry * (1 - inv) - maint_amount) / (1 - mmr) if maint_amount else entry * (1 - inv) / (1 - mmr)
    return (entry * (1 + inv) + maint_amount) / (1 + mmr) if maint_amount else entry * (1 + inv) / (1 + mmr)


class SizerAgent(Agent):
    name = "sizer"
    title = "SIZER"

    def __init__(self, logger, md: MarketData, cfg: ConfigStore, portfolio: Portfolio):
        super().__init__(logger)
        self.md, self.cfg, self.pf = md, cfg, portfolio
        self.metrics = {"sized": 0, "rejected": 0, "avg_risk_usdt": 0.0}

    async def correlated(self, mode: str, market: str, symbol: str, direction: str) -> list[dict]:
        """Open positions whose 1h returns correlate above threshold with ``symbol``
        in the same direction (or opposite direction with negative correlation)."""
        out = []
        thr = self.cfg.risk.correlation_threshold
        opens = [p for p in self.pf.open_positions(mode) if p["symbol"] != symbol or p["market"] != market]
        if not opens:
            return out
        base = await self.md.klines(market, symbol, "1h", 120)
        rb = np.diff(np.log(base.close))
        for p in opens:
            try:
                other = await self.md.klines(p["market"], p["symbol"], "1h", 120)
            except Exception:  # noqa: BLE001
                continue
            ro = np.diff(np.log(other.close))
            n = min(len(rb), len(ro), 100)
            if n < 30:
                continue
            rho = float(np.corrcoef(rb[-n:], ro[-n:])[0, 1])
            same = p["direction"] == direction
            if (same and rho >= thr) or (not same and rho <= -thr) or p["symbol"] == symbol:
                out.append({"trade_id": p["trade_id"], "symbol": p["symbol"], "market": p["market"],
                            "rho": round(rho, 3), "notional": float(D(p["qty"]) * D(p["entry_price"]))})
        return out

    async def size(self, mode: str, trade: dict) -> dict:
        r, fees = self.cfg.risk, self.cfg.fees
        market, symbol, sig = trade["market"], trade["symbol"], trade["signal"]
        self.busy(f"Sizing {symbol} {sig['direction']}")
        direction = sig["direction"]
        entry, stop, target = D(sig["entry"]), D(sig["stop"]), D(sig["target"])
        rules = self.md.rules[market].get(symbol)
        summary = self.pf.summary(mode)
        m = summary[market]
        equity = D(m["equity"])
        calc: dict = {"market": market, "equity": float(equity)}

        def reject(reason: str) -> dict:
            self.metrics["rejected"] += 1
            self.log("rejected", f"Sizer rejected {symbol}: {reason}", trade["id"], {"calc": calc}, "warning")
            self.done(f"REJECTED {symbol}: {reason[:60]}")
            return {"approved": False, "reason": reason, "calc": calc}

        if rules is None:
            return reject("exchange rules unavailable")
        stop_dist = abs(entry - stop)
        if stop_dist <= 0 or (direction == "LONG" and stop >= entry) or (direction == "SHORT" and stop <= entry):
            return reject("invalid stop-loss placement")
        if equity <= 0:
            return reject(f"no equity allocated to {market}")
        taker = D(fees.spot_taker if market == "spot" else fees.futures_taker)
        t = self.md.ticker(market, symbol)
        spread_frac = D(t.spread_bps / 1e4) if t and t.spread_bps != float("inf") else D("0.001")
        slip = spread_frac / 2 + D(fees.paper_extra_slippage_bps) / D(10_000)
        risk_budget = equity * D(r.risk_per_trade_pct) / 100
        unit_loss = stop_dist + entry * (2 * taker + slip)
        qty = risk_budget / unit_loss
        calc.update(risk_budget=float(risk_budget), stop_distance=float(stop_dist),
                    stop_pct=float(stop_dist / entry * 100), unit_loss=float(unit_loss),
                    fee_rate=float(taker), est_slippage_frac=float(slip), qty_risk=float(qty))

        # Volatility adjustment
        c = await self.md.klines(market, symbol, trade["timeframe"], 300)
        atr_pct = ind.atr(c.high, c.low, c.close, 14) / c.close * 100
        cur = atr_pct[-1]
        med = float(np.nanmedian(atr_pct[-100:]))
        vol_factor = D(min(1.0, med / cur)) if cur and not math.isnan(cur) and med > 0 else D(1)
        qty *= vol_factor
        calc.update(atr_pct=float(cur), atr_pct_median=med, vol_factor=float(vol_factor))

        # Correlation adjustment
        corr = await self.correlated(mode, market, symbol, direction)
        corr_factor = D("0.5") if corr else D(1)
        qty *= corr_factor
        calc.update(correlated=corr, corr_factor=float(corr_factor))

        # Caps
        max_notional = equity * D(r.max_position_pct) / 100
        caps = {"max_position_pct": max_notional}
        if market == "spot":
            avail = D(m["available"])
            caps["available_cash"] = avail / (1 + taker) * D("0.995")
        else:
            avail = D(m["available"])
            caps["available_margin_x_max_leverage"] = avail * D(r.max_leverage) * D("0.95")
        cap_notional = min(caps.values())
        if qty * entry > cap_notional:
            qty = cap_notional / entry
            calc["capped_by"] = min(caps, key=caps.get)
        calc["caps"] = {k: float(v) for k, v in caps.items()}

        qty = rules.round_qty(qty)
        notional = qty * entry
        calc.update(qty=float(qty), notional=float(notional))
        errs = rules.validate(qty, entry)
        if errs:
            return reject("; ".join(errs) + f" (risk budget {risk_budget:.2f} USDT allows {notional:.2f} USDT notional)")

        leverage, margin, liq = 1, notional, None
        if market == "futures":
            leverage = max(1, math.ceil(float(notional / (avail * D("0.95"))))) if avail > 0 else r.max_leverage + 1
            if leverage > r.max_leverage:
                return reject(f"requires {leverage}x leverage > max {r.max_leverage}x")
            margin = notional / D(leverage)
            liq = liquidation_price(entry, leverage, direction)
            buf = D(self.cfg.rugcheck.min_liquidation_buffer_pct) / 100 * entry
            if (direction == "LONG" and stop <= liq + buf) or (direction == "SHORT" and stop >= liq - buf):
                return reject(f"stop {stop:.6g} beyond liquidation {liq:.6g} at {leverage}x")
        risk_usdt = qty * unit_loss
        reward = abs(target - entry) * qty - qty * entry * (2 * taker + slip)
        rr_net = reward / risk_usdt if risk_usdt > 0 else ZERO
        risk_pct = risk_usdt / equity * 100
        calc.update(leverage=leverage, margin=float(margin), liquidation_price=float(liq) if liq else None,
                    risk_usdt=float(risk_usdt), risk_pct=float(risk_pct), reward_usdt=float(reward),
                    rr_net=float(rr_net), est_fees=float(qty * entry * 2 * taker))
        if rr_net < D(r.min_reward_risk):
            return reject(f"net reward/risk {rr_net:.2f} after costs below minimum {r.min_reward_risk}")
        self.metrics["sized"] += 1
        n = self.metrics["sized"]
        self.metrics["avg_risk_usdt"] = round((self.metrics["avg_risk_usdt"] * (n - 1) + float(risk_usdt)) / n, 4)
        result = {"approved": True, "qty": str(qty), "entry": str(entry), "stop": str(stop), "target": str(target),
                  "notional": float(notional), "leverage": leverage, "margin": float(margin),
                  "liquidation_price": float(liq) if liq else None, "risk_usdt": float(risk_usdt),
                  "risk_pct": float(risk_pct), "rr_net": float(rr_net), "calc": calc}
        self.log("sized", f"Sizer: {symbol} qty {qty} (≈{notional:.2f} USDT, risk {risk_usdt:.2f} USDT = "
                 f"{risk_pct:.2f}%, {leverage}x, net R:R {rr_net:.2f})", trade["id"], result)
        self.done(f"{symbol} {qty} @ {leverage}x, risk {risk_pct:.2f}%")
        return result
