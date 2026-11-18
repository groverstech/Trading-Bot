"""Runtime configuration with validation and an audit trail.

Every section is a pydantic model. Updates are validated, persisted, and
recorded in ``audit_log`` with old/new values, so all configuration changes
are traceable.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .db import Database, dumps, loads
from .util import now

class Strict(BaseModel):
    """Unknown keys are rejected so a typo can never be silently ignored."""
    model_config = ConfigDict(extra="forbid")


DEFAULT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT",
]
TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d"]


class CapitalCfg(Strict):
    starting_inr: float = Field(50_000, gt=0)
    spot_pct: float = Field(40, ge=0, le=100)
    futures_pct: float = Field(30, ge=0, le=100)
    reserve_pct: float = Field(30, ge=0, le=100)

    @model_validator(mode="after")
    def _sum(self):
        total = self.spot_pct + self.futures_pct + self.reserve_pct
        if abs(total - 100) > 1e-6:
            raise ValueError(f"spot + futures + reserve must equal 100% (got {total})")
        return self


class RiskCfg(Strict):
    risk_per_trade_pct: float = Field(0.5, gt=0, le=5)
    max_risk_per_trade_pct: float = Field(1.0, gt=0, le=5)
    max_daily_loss_pct: float = Field(2.0, gt=0, le=50)
    max_drawdown_pct: float = Field(5.0, gt=0, le=90)
    max_leverage: int = Field(2, ge=1, le=20)
    max_positions: int = Field(3, ge=1, le=50)
    # Gross notional of open positions as % of the allocated equity of that market.
    max_exposure_pct: float = Field(150, gt=0, le=1000)
    # Notional of positions highly correlated with a new trade, % of allocated equity.
    max_correlated_exposure_pct: float = Field(60, gt=0, le=1000)
    correlation_threshold: float = Field(0.8, gt=0, le=1)
    min_reward_risk: float = Field(1.5, gt=0, le=20)
    max_position_pct: float = Field(50, gt=0, le=100)  # single position notional / allocation
    margin_mode: Literal["ISOLATED", "CROSSED"] = "ISOLATED"
    stale_data_seconds: float = Field(20, gt=1, le=600)
    signal_ttl_seconds: float = Field(180, gt=5, le=3600)
    # What to do with open positions when a halt triggers.
    # keep_protected: leave positions with their stop-loss / take-profit in place
    # flatten: close positions at market, but only if the exchange is reachable.
    emergency_policy: Literal["keep_protected", "flatten"] = "keep_protected"
    mandatory_stop_loss: bool = True

    @model_validator(mode="after")
    def _limits(self):
        if self.risk_per_trade_pct > self.max_risk_per_trade_pct:
            raise ValueError("risk_per_trade_pct cannot exceed max_risk_per_trade_pct")
        if not self.mandatory_stop_loss:
            raise ValueError("stop-loss is mandatory and cannot be disabled")
        return self


class RugCheckCfg(Strict):
    min_quote_volume_24h: float = Field(20_000_000, ge=0)
    max_spread_bps: float = Field(8, gt=0)
    # Liquidity within +/- depth_band_pct of mid must be >= this multiple of order notional
    depth_band_pct: float = Field(0.5, gt=0, le=10)
    min_depth_multiple: float = Field(25, ge=1)
    max_candle_atr_multiple: float = Field(4.0, gt=0)
    max_volume_zscore: float = Field(10.0, gt=0)
    max_24h_change_pct: float = Field(25, gt=0)
    max_abs_funding_rate: float = Field(0.001, gt=0)  # 0.1% per funding interval
    max_mark_last_deviation_pct: float = Field(0.5, gt=0)
    min_liquidation_buffer_pct: float = Field(1.0, ge=0)  # beyond the stop, % of price


class FeesCfg(Strict):
    spot_taker: float = Field(0.001, ge=0, le=0.01)
    spot_maker: float = Field(0.001, ge=0, le=0.01)
    futures_taker: float = Field(0.0005, ge=0, le=0.01)
    futures_maker: float = Field(0.0002, ge=0, le=0.01)
    # Added on top of the slippage simulated by walking the real order book.
    paper_extra_slippage_bps: float = Field(2, ge=0, le=200)


class StrategyCfg(Strict):
    enabled: bool = True
    params: dict[str, float] = {}


def _default_strategies() -> dict[str, StrategyCfg]:
    return {
        "momentum": StrategyCfg(params={"rsi_len": 14, "rsi_long": 55, "rsi_short": 45, "atr_stop": 1.5, "rr": 2.0}),
        "breakout": StrategyCfg(params={"lookback": 20, "vol_mult": 1.5, "atr_stop": 1.2, "rr": 2.0}),
        "trend": StrategyCfg(params={"adx_min": 20, "atr_stop": 1.5, "rr": 2.5}),
        "volume": StrategyCfg(params={"vol_mult": 2.5, "close_pos": 0.7, "atr_stop": 1.3, "rr": 2.0}),
        "volatility": StrategyCfg(params={"squeeze_pct": 20, "expansion": 1.3, "atr_stop": 1.5, "rr": 2.0}),
    }


class ScannerCfg(Strict):
    interval_seconds: int = Field(60, ge=10, le=3600)
    markets: list[Literal["spot", "futures"]] = ["spot", "futures"]
    spot_symbols: list[str] = DEFAULT_SYMBOLS
    futures_symbols: list[str] = DEFAULT_SYMBOLS
    timeframes: list[str] = ["15m"]
    confirm_timeframes: list[str] = ["1h", "4h"]
    min_score: float = Field(0.55, ge=0, le=1)
    max_signals_per_cycle: int = Field(3, ge=1, le=20)

    @field_validator("timeframes", "confirm_timeframes")
    @classmethod
    def _tf(cls, v: list[str]):
        bad = [t for t in v if t not in TIMEFRAMES]
        if bad:
            raise ValueError(f"unsupported timeframes {bad}; allowed {TIMEFRAMES}")
        return v

    @field_validator("spot_symbols", "futures_symbols")
    @classmethod
    def _sym(cls, v: list[str]):
        out = []
        for s in v:
            s = s.strip().upper()
            if not s.isalnum() or not s.endswith("USDT"):
                raise ValueError(f"invalid symbol {s!r}: only USDT-quoted pairs are supported")
            out.append(s)
        return out


class ScheduleCfg(Strict):
    enabled: bool = False
    days: list[int] = [0, 1, 2, 3, 4, 5, 6]  # Monday=0, UTC
    start_utc: str = "00:00"
    end_utc: str = "23:59"


class AutomationCfg(Strict):
    # Automated approval: if False, risk-approved trades wait for manual approval.
    auto_approve: bool = True
    enabled_agents: dict[str, bool] = {"scanner": True, "executor": True}
    schedule: ScheduleCfg = ScheduleCfg()


SECTIONS: dict[str, type[BaseModel]] = {
    "capital": CapitalCfg,
    "risk": RiskCfg,
    "rugcheck": RugCheckCfg,
    "fees": FeesCfg,
    "scanner": ScannerCfg,
    "automation": AutomationCfg,
}


class ConfigStore:
    def __init__(self, db: Database):
        self.db = db
        self._cache: dict[str, Any] = {}
        for name, model in SECTIONS.items():
            raw = db.scalar("SELECT value FROM config WHERE key=?", (name,))
            obj = model.model_validate(loads(raw)) if raw else model()
            self._cache[name] = obj
            if raw is None:
                self._write(name, obj)
        raw = db.scalar("SELECT value FROM config WHERE key='strategies'")
        strategies = _default_strategies()
        if raw:
            for k, v in loads(raw).items():
                if k in strategies:
                    strategies[k] = StrategyCfg.model_validate(v)
        self._cache["strategies"] = strategies
        if raw is None:
            self._write_strategies()

    # -- typed accessors -------------------------------------------------
    @property
    def capital(self) -> CapitalCfg: return self._cache["capital"]
    @property
    def risk(self) -> RiskCfg: return self._cache["risk"]
    @property
    def rugcheck(self) -> RugCheckCfg: return self._cache["rugcheck"]
    @property
    def fees(self) -> FeesCfg: return self._cache["fees"]
    @property
    def scanner(self) -> ScannerCfg: return self._cache["scanner"]
    @property
    def automation(self) -> AutomationCfg: return self._cache["automation"]
    @property
    def strategies(self) -> dict[str, StrategyCfg]: return self._cache["strategies"]

    def as_dict(self) -> dict[str, Any]:
        out = {k: v.model_dump() for k, v in self._cache.items() if k != "strategies"}
        out["strategies"] = {k: v.model_dump() for k, v in self.strategies.items()}
        return out

    # -- updates -----------------------------------------------------------
    def update(self, section: str, patch: dict[str, Any], actor: str) -> dict[str, Any]:
        if section == "strategies":
            return self._update_strategies(patch, actor)
        if section not in SECTIONS:
            raise KeyError(f"unknown config section {section!r}")
        old = self._cache[section]
        merged = {**old.model_dump(), **patch}
        new = SECTIONS[section].model_validate(merged)
        with self.db.tx():
            self._write(section, new)
            self.audit(actor, f"config.{section}", {"old": old.model_dump(), "new": new.model_dump()})
        self._cache[section] = new
        return new.model_dump()

    def _update_strategies(self, patch: dict[str, Any], actor: str) -> dict[str, Any]:
        current = self.strategies
        updated = dict(current)
        for name, val in patch.items():
            if name not in current:
                raise KeyError(f"unknown strategy {name!r}")
            merged = current[name].model_dump()
            merged.update({k: v for k, v in val.items() if k != "params"})
            merged["params"] = {**merged["params"], **val.get("params", {})}
            updated[name] = StrategyCfg.model_validate(merged)
        with self.db.tx():
            self._cache["strategies"] = updated
            self._write_strategies()
            self.audit(actor, "config.strategies", {
                "old": {k: v.model_dump() for k, v in current.items()},
                "new": {k: v.model_dump() for k, v in updated.items()},
            })
        return {k: v.model_dump() for k, v in updated.items()}

    def _write(self, name: str, obj: BaseModel) -> None:
        self.db.execute(
            "INSERT INTO config(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (name, obj.model_dump_json(), now()),
        )

    def _write_strategies(self) -> None:
        self.db.execute(
            "INSERT INTO config(key,value,updated_at) VALUES('strategies',?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (dumps({k: v.model_dump() for k, v in self.strategies.items()}), now()),
        )

    def audit(self, actor: str, action: str, detail: Any) -> None:
        self.db.execute(
            "INSERT INTO audit_log(ts,actor,action,detail) VALUES(?,?,?,?)",
            (now(), actor, action, dumps(detail)),
        )
