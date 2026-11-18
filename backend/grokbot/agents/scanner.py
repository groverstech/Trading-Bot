"""AGENT 1 - SCANNER.

Continuously scans the configured Spot and USDⓈ-M Futures pairs on the
configured entry timeframes, using higher timeframes for trend confirmation.
Runs every enabled strategy on CLOSED candles and ranks resulting signals.
It never invents opportunities: if no strategy's conditions are met, the
cycle ends with explicit no-trade decisions.
"""
from __future__ import annotations

import asyncio
import time
from collections import Counter

from ..config_store import ConfigStore
from ..market.data import TF_SECONDS, MarketData
from ..strategy import indicators as ind
from ..strategy.strategies import evaluate
from .base import Agent


class ScannerAgent(Agent):
    name = "scanner"
    title = "SCANNER"

    def __init__(self, logger, md: MarketData, cfg: ConfigStore, mode_fn=None):
        super().__init__(logger)
        self.md, self.cfg, self.mode_fn = md, cfg, mode_fn
        self.metrics = {"cycles": 0, "signals": 0, "no_trade": 0, "markets_scanned": 0, "last_cycle_ms": 0}
        self.board: dict[str, dict] = {}   # per market:symbol summary for the watchlist
        self.no_trade_reasons: Counter = Counter()
        self._seen: set[str] = set()

    async def _scan_one(self, market: str, symbol: str, tf: str, sem: asyncio.Semaphore) -> list[dict]:
        sc = self.cfg.scanner
        enabled = self.tradable()
        async with sem:
            c = await self.md.klines(market, symbol, tf, 300)
            htf = {}
            # Confirmation only from timeframes ABOVE the entry timeframe (mirrors research).
            for ctf in [x for x in sc.confirm_timeframes if TF_SECONDS[x] > TF_SECONDS[tf]]:
                hc = await self.md.klines(market, symbol, ctf, 250)
                htf[ctf] = ind.trend_direction(hc.close)
        sigs, nots = evaluate(c, enabled, htf, allow_short=(market == "futures"))
        # A signal is only actionable shortly after its candle closed. After a
        # restart (or a slow cycle) the last closed candle can be old and the
        # market far from its close price, so such signals are discarded.
        if len(c):
            age = time.time() - c.close_time[-1] / 1000
            max_age = min(max(120.0, 2.0 * self.cfg.scanner.interval_seconds), TF_SECONDS[tf] * 0.5)
            if sigs and age > max_age:
                from ..strategy.strategies import NoTrade
                nots += [NoTrade(sg.strategy, f"stale signal: candle closed {age:.0f}s ago (max {max_age:.0f}s)") for sg in sigs]
                self.bump("stale_signals_dropped", len(sigs))
                sigs = []
        mode = self.mode_fn() if self.mode_fn else "paper"
        if not (mode == "paper" and self.cfg.automation.paper_research_mode):
            kept = []
            for sg in sigs:  # promoted strategies trade only where they were validated
                pr = self.cfg.strategies[sg.strategy].promotion or {}
                if tf in pr.get("timeframes", []) and market in pr.get("markets", []):
                    kept.append(sg)
                else:
                    from ..strategy.strategies import NoTrade
                    nots.append(NoTrade(sg.strategy, f"not validated for {market} {tf}"))
            sigs = kept
        for n in nots:
            self.no_trade_reasons[f"{n.strategy}: {n.reason.split('(')[0].strip()}"] += 1
        self.metrics["no_trade"] += len(nots)
        r = ind.rsi(c.close)[-1] if len(c) > 15 else float("nan")
        vm = ind.sma(c.volume, 20)
        self.board[f"{market}:{symbol}"] = {
            "market": market, "symbol": symbol, "timeframe": tf, "rsi": None if r != r else round(float(r), 1),
            "trend": {k: v for k, v in htf.items()},
            "vol_ratio": round(float(c.volume[-1] / vm[-2]), 2) if len(c) > 21 and vm[-2] else None,
            "signals": [s.strategy + ":" + s.direction for s in sigs], "candle": int(c.open_time[-1]) if len(c) else None,
            "ts": time.time()}
        out = []
        for s in sigs:
            out.append({"market": market, "symbol": symbol, "timeframe": tf, "signal": s,
                        "candle_open": int(c.open_time[-1])})
        return out

    def tradable(self) -> dict[str, dict]:
        """Strategies allowed to generate trades in the current mode.

        Testnet/live: enabled AND promoted by walk-forward validation.
        Paper: the same, unless paper_research_mode is on, in which case
        enabled-but-unpromoted strategies may forward-test on paper only."""
        mode = self.mode_fn() if self.mode_fn else "paper"
        research = mode == "paper" and self.cfg.automation.paper_research_mode
        return {n: s.params for n, s in self.cfg.strategies.items() if s.enabled and (s.promoted or research)}

    async def scan(self) -> list[dict]:
        sc = self.cfg.scanner
        jobs = []
        sem = asyncio.Semaphore(4)
        for market in sc.markets:
            syms = sc.spot_symbols if market == "spot" else sc.futures_symbols
            for sym in syms:
                if sym not in self.md.rules[market]:
                    continue  # not listed on this market (or rules not loaded yet)
                for tf in sc.timeframes:
                    jobs.append((market, sym, tf))
        if not self.tradable():
            self.done("No tradable strategies (none promoted)", activity="Idle: no promoted strategies")
            self.log("scan_skipped", "No strategy is both enabled and promoted for this mode; nothing to scan",
                     level="warning")
            return []
        self.busy(f"Scanning {len(jobs)} market/timeframe combinations")
        t0 = time.time()
        results = await asyncio.gather(*(self._scan_one(m, s, tf, sem) for m, s, tf in jobs), return_exceptions=True)
        cands, failures = [], []
        for job, r in zip(jobs, results):
            if isinstance(r, Exception):
                failures.append(f"{job[0]}:{job[1]}:{job[2]} {r}")
            else:
                cands.extend(r)
        cands = [c for c in cands if c["signal"].score >= sc.min_score]
        below = sum(1 for r in results if not isinstance(r, Exception) for c in r) - len(cands)
        cands.sort(key=lambda c: c["signal"].score, reverse=True)
        cands = cands[: sc.max_signals_per_cycle]
        self.metrics["cycles"] += 1
        self.metrics["markets_scanned"] = len(jobs)
        self.metrics["signals"] += len(cands)
        self.metrics["last_cycle_ms"] = int((time.time() - t0) * 1000)
        self.metrics["top_no_trade_reasons"] = self.no_trade_reasons.most_common(5)
        summary = (f"Scanned {len(jobs)} markets: {len(cands)} opportunit{'y' if len(cands) == 1 else 'ies'}"
                   + (f", {below} below min score {sc.min_score}" if below else "")
                   + (f", {len(failures)} data errors" if failures else ""))
        self.log("scan_cycle", summary, data={"failures": failures[:10], "jobs": len(jobs)},
                 level="warning" if failures else "info")
        if failures and len(failures) == len(jobs) and jobs:
            self.fail(f"all scans failed: {failures[0]}")
        else:
            self.done("No trade - conditions not met" if not cands else
                      f"{cands[0]['symbol']} {cands[0]['signal'].direction} ({cands[0]['signal'].strategy})",
                      activity="Waiting for next scan cycle")
        return cands
