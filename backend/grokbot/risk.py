"""Central risk management and authorization layer.

``validate`` runs every portfolio-level check on a sized trade. Only if all
pass does it issue an approval token: an HMAC over (trade id, mode, qty,
stop, expiry) signed with a key derived from the master key. The Executor
refuses to place any order without a valid, unexpired token whose fields
match the trade, so no agent can bypass this layer.
"""
from __future__ import annotations

import time
from decimal import Decimal

from .config_store import ConfigStore
from .db import Database
from .exchange.client import BinanceClient
from .market.data import MarketData
from .portfolio import Portfolio
from .security import Vault
from .state import SystemState
from .util import D

IN_FLIGHT = ("RISK_APPROVED", "AWAITING_APPROVAL", "AWAITING_LIVE_CONFIRM", "EXECUTING", "WORKING")
TOKEN_TTL = 120


class RiskEngine:
    def __init__(self, db: Database, cfg: ConfigStore, md: MarketData, pf: Portfolio, state: SystemState,
                 vault: Vault, trade_clients):
        self.db, self.cfg, self.md, self.pf, self.state, self.vault = db, cfg, md, pf, state, vault
        self.trade_clients = trade_clients  # callable(mode, market) -> BinanceClient | None
        self.balance_fn = None              # async callable(mode) -> exchange balances (set by engine)
        self.metrics = {"validated": 0, "approved": 0, "rejected": 0, "top_rejections": {}}

    # ---------------------------------------------------------- tokens
    @staticmethod
    def _payload(trade_id: str, mode: str, qty: str, stop: str, exp: int) -> str:
        return f"{trade_id}|{mode}|{qty}|{stop}|{exp}"

    def issue_token(self, trade_id: str, mode: str, qty: str, stop: str) -> str:
        exp = int(time.time()) + TOKEN_TTL
        return f"{exp}.{self.vault.sign(self._payload(trade_id, mode, qty, stop, exp))}"

    def verify_token(self, token: str | None, trade_id: str, mode: str, qty: str, stop: str) -> tuple[bool, str]:
        if not token or "." not in token:
            return False, "missing approval token"
        exp_s, sig = token.split(".", 1)
        exp = int(exp_s)
        if time.time() > exp:
            return False, "approval token expired"
        if not self.vault.verify(self._payload(trade_id, mode, qty, stop, exp), sig):
            return False, "approval token invalid or trade parameters altered"
        return True, "ok"

    # ------------------------------------------------------- breaches
    def breach_status(self, mode: str) -> dict:
        s = self.pf.summary(mode)
        rs = self.pf.risk_state(mode, s["total_equity"])
        r = self.cfg.risk
        rs["daily_loss_breached"] = rs["daily_pnl_pct"] <= -r.max_daily_loss_pct
        rs["drawdown_breached"] = rs["drawdown_pct"] >= r.max_drawdown_pct
        rs["limits"] = {"max_daily_loss_pct": r.max_daily_loss_pct, "max_drawdown_pct": r.max_drawdown_pct}
        return rs

    def exchange_ok(self, mode: str, market: str) -> tuple[bool, str]:
        data_client: BinanceClient = self.md.adapters[market].data
        if not data_client.health.connected:
            return False, f"{market} market data connection down ({data_client.health.last_error_msg})"
        if mode in ("testnet", "live"):
            tc = self.trade_clients(mode, market)
            if tc is None or not tc.has_credentials:
                return False, f"no {mode} {market} API credentials"
            if tc.health.requests and not tc.health.connected:
                return False, f"{mode} {market} trading connection down ({tc.health.last_error_msg})"
        return True, "ok"

    # ------------------------------------------------------- validation
    async def validate(self, mode: str, trade: dict, sizing: dict, sizer) -> dict:
        r = self.cfg.risk
        market, symbol = trade["market"], trade["symbol"]
        checks: list[dict] = []

        def add(name, ok, detail):
            checks.append({"name": name, "ok": bool(ok), "detail": detail})

        add("not_halted", not self.state.halted, self.state.halt.get("kind") or "trading enabled")
        if mode == "live":
            add("live_authorized", self.state.live_authorized(), self.state.live_auth.get("status"))
        ok, why = self.exchange_ok(mode, market)
        add("exchange_connectivity", ok, why)
        if mode in ("testnet", "live"):
            src = self.md.adapters[market].data.base_url
            is_test = "testnet" in src
            add("data_source_matches_mode", is_test == (mode == "testnet"),
                f"{market} market data from {src} ({'testnet' if is_test else 'production'}) for {mode} orders")
        age = self.md.age(market, symbol)
        add("fresh_market_data", age <= r.stale_data_seconds, f"price age {age:.1f}s (max {r.stale_data_seconds}s)")
        sig_age = time.time() - trade["created_at"]
        add("signal_fresh", sig_age <= r.signal_ttl_seconds, f"signal age {sig_age:.0f}s (max {r.signal_ttl_seconds:.0f}s)")
        entry, stop = D(sizing["entry"]), D(sizing["stop"])
        direction = trade["direction"]
        add("stop_loss_set", (direction == "LONG" and stop < entry) or (direction == "SHORT" and stop > entry),
            f"stop {stop} vs entry {entry}")
        # Executable price must not have run away from the signal price.
        sig_entry = D(trade["signal"]["entry"]) if isinstance(trade.get("signal"), dict) else entry
        sig_stop = D(trade["signal"]["stop"]) if isinstance(trade.get("signal"), dict) else stop
        planned = abs(sig_entry - sig_stop)
        adverse = (sig_entry - entry) if direction == "SHORT" else (entry - sig_entry)
        add("entry_drift", planned > 0 and adverse <= planned * D("0.25"),
            f"executable {entry} vs signal {sig_entry}: adverse drift {adverse / planned * 100 if planned else 0:.0f}% "
            f"of stop distance (max 25%)")
        add("min_reward_risk", sizing["rr_net"] >= r.min_reward_risk, f"net R:R {sizing['rr_net']:.2f} (min {r.min_reward_risk})")
        add("risk_per_trade", sizing["risk_pct"] <= r.max_risk_per_trade_pct + 1e-9,
            f"{sizing['risk_pct']:.3f}% of {market} equity (max {r.max_risk_per_trade_pct}%)")
        add("leverage", sizing["leverage"] <= r.max_leverage, f"{sizing['leverage']}x (max {r.max_leverage}x)")
        if market == "spot":
            add("spot_long_only", direction == "LONG", "spot trades must be long")

        opens = self.pf.open_positions(mode)
        inflight = self.db.all(
            f"SELECT id, market, symbol FROM trades WHERE mode=? AND id<>? AND stage IN ({','.join('?' * len(IN_FLIGHT))})",
            (mode, trade["id"], *IN_FLIGHT))
        n = len(opens) + len(inflight)
        add("max_positions", n < r.max_positions, f"{n} open/in-flight (max {r.max_positions})")
        dup = [p for p in opens if p["market"] == market and p["symbol"] == symbol] + \
              [t for t in inflight if t["market"] == market and t["symbol"] == symbol]
        add("no_duplicate", not dup, "existing position/in-flight trade on same market+symbol" if dup else "none")

        b = self.breach_status(mode)
        add("daily_loss_limit", not b["daily_loss_breached"],
            f"daily P&L {b['daily_pnl_pct']:.2f}% (limit -{r.max_daily_loss_pct}%)")
        add("drawdown_limit", not b["drawdown_breached"], f"drawdown {b['drawdown_pct']:.2f}% (limit {r.max_drawdown_pct}%)")

        s = self.pf.summary(mode)
        m_eq = Decimal(str(s[market]["equity"]))
        exposure = sum((D(p["qty"]) * D(p["entry_price"]) for p in opens if p["market"] == market), D(0))
        new_exp = exposure + D(sizing["notional"])
        lim = m_eq * D(r.max_exposure_pct) / 100
        add("max_exposure", new_exp <= lim, f"{market} gross exposure {new_exp:.2f} / limit {lim:.2f} USDT")
        corr = await sizer.correlated(mode, market, symbol, direction)
        corr_exp = sum((D(c["notional"]) for c in corr), D(0)) + D(sizing["notional"])
        total_eq = Decimal(str(s["spot"]["equity"] + s["futures"]["equity"]))
        clim = total_eq * D(r.max_correlated_exposure_pct) / 100
        add("correlated_exposure", corr_exp <= clim,
            f"correlated exposure {corr_exp:.2f} / limit {clim:.2f} USDT ({len(corr)} correlated)")
        if market == "spot":
            avail = D(s["spot"]["available"])
            add("capital_available", D(sizing["notional"]) * D("1.002") <= avail,
                f"needs {sizing['notional']:.2f}, available {avail:.2f} USDT")
        else:
            avail = D(s["futures"]["available"])
            add("margin_available", D(sizing["margin"]) <= avail,
                f"margin {sizing['margin']:.2f}, available {avail:.2f} USDT")

        if mode in ("testnet", "live") and self.balance_fn is not None:
            # The configured ₹ budget is not an exchange deposit: verify real free balance too.
            try:
                bal = await self.balance_fn(mode)
                if market == "spot":
                    free = D((bal.get("spot") or {}).get("USDT", {}).get("free", "0"))
                    need = D(sizing["notional"]) * D("1.002")
                else:
                    free = D((bal.get("futures") or {}).get("USDT", {}).get("available", "0"))
                    need = D(sizing["margin"]) * D("1.05")
                add("exchange_balance", free >= need, f"Binance {mode} {market} free {free:.2f} USDT, needs {need:.2f}")
            except Exception as e:  # noqa: BLE001
                add("exchange_balance", False, f"could not verify exchange balance: {e}")

        failed = [c for c in checks if not c["ok"]]
        self.metrics["validated"] += 1
        if failed:
            self.metrics["rejected"] += 1
            for f in failed:
                self.metrics["top_rejections"][f["name"]] = self.metrics["top_rejections"].get(f["name"], 0) + 1
            return {"approved": False, "checks": checks,
                    "reason": "; ".join(f"{c['name']}: {c['detail']}" for c in failed)}
        self.metrics["approved"] += 1
        token = self.issue_token(trade["id"], mode, sizing["qty"], sizing["stop"])
        return {"approved": True, "checks": checks, "token": token}
