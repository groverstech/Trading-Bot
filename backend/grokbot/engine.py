"""Engine container: constructs and wires every component."""
from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

from .agents.base import EventBus
from .agents.equity import EquityAgent
from .agents.executor import ExecutorAgent
from .agents.logger import LoggerAgent
from .agents.rugcheck import RugCheckAgent
from .agents.scanner import ScannerAgent
from .agents.sizer import SizerAgent
from .config_store import ConfigStore
from .db import Database
from .exchange.broker import ExchangeBroker, PaperBroker
from .exchange.client import BinanceClient, BinanceError
from .exchange.futures import FuturesAdapter
from .exchange.spot import SpotAdapter
from .market.data import MarketData
from .market.fx import FxService
from .orchestrator import Orchestrator
from .portfolio import Portfolio
from .risk import RiskEngine
from .security import Auth, Vault
from .settings import Settings
from .state import SystemState


class Engine:
    def __init__(self, settings: Settings, transport=None):
        self.settings = s = settings
        self.db = Database(s.db_path)
        self.cfg = ConfigStore(self.db)
        self.vault = Vault(self.db, s.data_dir, s.master_key_env)
        self.auth = Auth(self.db, s.session_hours, s.admin_password_env)
        self.state = SystemState(self.db, s.allow_live)
        self.bus = EventBus()
        self._transport = transport
        ep = s.endpoints
        self.spot_data = BinanceClient(ep.spot_data, "spot", transport=transport)
        self.futures_data = BinanceClient(ep.futures_data, "futures", transport=transport)
        self.md = MarketData(SpotAdapter(self.spot_data), FuturesAdapter(self.futures_data), s.futures_data_is_testnet)
        self.fx = FxService(self.db, s.fx_url, transport=transport)
        self.pf = Portfolio(self.db, self.md, self.cfg, self.fx)
        self._trade_clients: dict[tuple[str, str], BinanceClient] = {}
        self._brokers: dict[str, object] = {"paper": PaperBroker(self.md, self.cfg)}
        self.risk = RiskEngine(self.db, self.cfg, self.md, self.pf, self.state, self.vault, self.trade_client)
        self.risk.balance_fn = self._cached_balances
        self._bal_cache: dict[str, tuple[float, dict]] = {}
        self.logger = LoggerAgent(self.db, self.bus, lambda: self.state.mode)
        self.scanner = ScannerAgent(self.logger, self.md, self.cfg)
        self.rugcheck = RugCheckAgent(self.logger, self.md, self.cfg)
        self.sizer = SizerAgent(self.logger, self.md, self.cfg, self.pf)
        self.orch = Orchestrator(self)
        self.executor = ExecutorAgent(self.logger, self.db, self.md, self.cfg, self.pf, self.risk, self.state,
                                      self.broker, self._on_change)
        self.equity = EquityAgent(self.logger, self.db, self.pf, self.risk, self.orch.on_breach)
        self.agents = [self.scanner, self.rugcheck, self.sizer, self.executor, self.logger, self.equity]

    def _on_change(self, mode: str, reason: str) -> None:
        self.equity.update(mode, reason)
        self.bus.publish({"type": "portfolio"})

    # ----------------------------------------------------- trading clients
    def _base(self, env: str, market: str) -> str:
        ep = self.settings.endpoints
        return {("testnet", "spot"): ep.spot_testnet, ("live", "spot"): ep.spot_live,
                ("testnet", "futures"): ep.futures_testnet, ("live", "futures"): ep.futures_live}[(env, market)]

    def trade_client(self, env: str, market: str) -> BinanceClient | None:
        if env not in ("testnet", "live"):
            return None
        key = (env, market)
        if key not in self._trade_clients:
            creds = self.vault.get_credentials(market, env)
            if not creds:
                return None
            self._trade_clients[key] = BinanceClient(self._base(env, market), market, creds[0], creds[1],
                                                     transport=self._transport)
        return self._trade_clients[key]

    def reset_clients(self) -> None:
        self._trade_clients.clear()
        for k in ("testnet", "live"):
            self._brokers.pop(k, None)
        self.state.check_live_integrity(self.fingerprints())

    def broker(self, mode: str):
        if mode not in self._brokers:
            spot = SpotAdapter(self.spot_data, self.trade_client(mode, "spot"))
            fut = FuturesAdapter(self.futures_data, self.trade_client(mode, "futures"))
            self._brokers[mode] = ExchangeBroker(mode, spot, fut, self.md)
        return self._brokers[mode]

    async def _cached_balances(self, mode: str) -> dict:
        hit = self._bal_cache.get(mode)
        if hit and time.time() - hit[0] < 5:
            return hit[1]
        bal = await self.broker(mode).balances()
        self._bal_cache[mode] = (time.time(), bal)
        return bal

    def fingerprints(self) -> dict:
        return {m: self.vault.fingerprint(m, "live") for m in ("spot", "futures")}

    # ---------------------------------------------------- credentials
    async def verify_credentials(self, venue: str, env: str) -> dict:
        self.reset_clients()
        client = self.trade_client(env, venue)
        if client is None:
            return {"ok": False, "error": "no credentials stored"}
        out: dict = {"venue": venue, "env": env, "ts": time.time(), "checks": []}

        def chk(name, ok, detail):
            out["checks"].append({"name": name, "ok": bool(ok), "detail": detail})

        try:
            await client.sync_time()
            chk("connectivity", True, f"{client.base_url} reachable, clock offset {client.health.time_offset_ms} ms")
            if venue == "spot":
                acct = await SpotAdapter(self.spot_data, client).account()
                chk("can_trade", acct.get("canTrade"), f"canTrade={acct.get('canTrade')}")
                chk("can_withdraw_flag", True, f"account canWithdraw={acct.get('canWithdraw')} (account-level)")
                out["balances"] = [b for b in acct.get("balances", []) if float(b["free"]) + float(b["locked"]) > 0][:30]
            else:
                fa = FuturesAdapter(self.futures_data, client)
                acct = await fa.account()
                chk("can_trade", True, "futures account accessible")
                hedge = await fa.position_mode()
                chk("one_way_mode", not hedge, "One-way position mode required" if hedge else "One-way mode")
                out["balances"] = [{"asset": a["asset"], "wallet": a["walletBalance"], "available": a["availableBalance"]}
                                   for a in acct.get("assets", []) if float(a.get("walletBalance", 0)) > 0]
            if env == "live":
                restr = await SpotAdapter(self.spot_data, BinanceClient(
                    self.settings.endpoints.spot_live, "spot", *self.vault.get_credentials(venue, env),
                    transport=self._transport)).api_restrictions()
                chk("withdrawals_disabled", not restr.get("enableWithdrawals"),
                    "API key must NOT have withdrawal permission" if restr.get("enableWithdrawals") else "withdrawals disabled")
                if venue == "futures":
                    chk("futures_enabled", restr.get("enableFutures"), f"enableFutures={restr.get('enableFutures')}")
                else:
                    chk("spot_enabled", restr.get("enableSpotAndMarginTrading"),
                        f"enableSpotAndMarginTrading={restr.get('enableSpotAndMarginTrading')}")
                chk("ip_restricted", restr.get("ipRestrict"), "IP whitelist recommended" if not restr.get("ipRestrict") else "IP restricted")
                # IP restriction is recommended, not mandatory.
                out["checks"][-1]["ok"] = True
                out["checks"][-1]["advisory"] = not restr.get("ipRestrict")
            else:
                chk("withdrawals_disabled", True, "not checkable on testnet (testnet funds have no value)")
        except (BinanceError, Exception) as e:  # noqa: BLE001
            chk("api_call", False, str(e)[:300])
        out["ok"] = all(c["ok"] for c in out["checks"])
        self.vault.set_verification(venue, env, out)
        self.state.audit("operator", "credentials_verified", {"venue": venue, "env": env, "ok": out["ok"]})
        return out

    # ----------------------------------------------------- live readiness
    def risk_config_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.cfg.risk.model_dump(), sort_keys=True).encode()).hexdigest()[:16]

    def live_checks(self) -> list[dict]:
        checks = []

        def add(name, ok, detail):
            checks.append({"name": name, "ok": bool(ok), "detail": detail})

        add("deployment_allows_live", self.settings.allow_live,
            "GROK_ALLOW_LIVE=1 set" if self.settings.allow_live else "set GROK_ALLOW_LIVE=1 in the deployment to permit live trading")
        creds = {c["id"]: c for c in self.vault.list_credentials()}
        for m in self.cfg.scanner.markets:
            c = creds.get(f"{m}:live")
            v = (c or {}).get("verification") or {}
            add(f"live_{m}_credentials_verified", c and c.get("verified_at") and v.get("ok"),
                "verified" if c and v.get("ok") else "store and verify live API keys")
            wd = next((x for x in v.get("checks", []) if x["name"] == "withdrawals_disabled"), None)
            add(f"live_{m}_withdrawals_disabled", wd and wd["ok"], wd["detail"] if wd else "not verified")
        rc = self.db.meta_get("live_risk_confirmed")
        add("risk_config_confirmed", rc == self.risk_config_hash(),
            "current risk configuration confirmed" if rc == self.risk_config_hash() else "confirm the current risk configuration")
        st = json.loads(self.db.meta_get("safety_selftest") or "{}")
        fresh = st.get("passed") and time.time() - st.get("ts", 0) < 86400
        add("safety_tests_passed", fresh, st.get("summary", "run the safety self-test") if st else "run the safety self-test")
        n = int(self.db.scalar("SELECT COUNT(*) FROM positions WHERE mode='testnet' AND status='CLOSED'") or 0)
        add("testnet_round_trip", n >= 1, f"{n} completed testnet trade(s); at least 1 required")
        ack = json.loads(self.db.meta_get("live_jurisdiction_ack") or "{}")
        add("jurisdiction_acknowledged", ack.get("ack"), "operator confirmed Binance eligibility, product availability and tax obligations"
            if ack.get("ack") else "acknowledge eligibility / regulatory / tax review")
        add("not_halted", not self.state.halted, "no active halt" if not self.state.halted else "resolve the active halt")
        return checks

    async def run_selftest(self) -> dict:
        root = Path(__file__).resolve().parents[1]
        t0 = time.time()
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pytest", "-q", "-m", "safety", "-p", "no:cacheprovider", str(root / "tests"),
            cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out, _ = await proc.communicate()
        text = out.decode(errors="replace")
        last = [l for l in text.strip().splitlines() if l.strip()][-1:] or ["no output"]
        res = {"passed": proc.returncode == 0, "ts": time.time(), "duration_s": round(time.time() - t0, 1),
               "summary": last[0][:200], "output_tail": text[-3000:]}
        self.db.meta_set("safety_selftest", json.dumps(res))
        self.state.audit("operator", "safety_selftest", {k: res[k] for k in ("passed", "summary")})
        return res

    async def close(self) -> None:
        await self.orch.stop()
        for c in [self.spot_data, self.futures_data, *self._trade_clients.values()]:
            await c.close()
        self.db.close()
