"""Run walk-forward research:  python -m grokbot.research [--apply]

Downloads history from Binance public market data (cached), runs the
walk-forward grid in parallel, writes JSON + a Markdown report, and with
--apply records promotion decisions in the strategy configuration (the ONLY
way a strategy can become eligible for testnet/live trading).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

from ..exchange.client import BinanceClient
from ..exchange.spot import SpotAdapter
from ..market.data import Candles
from ..settings import load_settings
from ..strategy.backtest import fetch_history
from . import walkforward as wf
from .report import render

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
HISTORY = {"15m": 35040, "1h": 17520, "4h": 6570}   # ~1y, ~2y, ~3y


def _save(c: Candles, path: Path) -> None:
    np.savez(path, ot=c.open_time, o=c.open, h=c.high, l=c.low, c=c.close, v=c.volume, q=c.quote_volume, ct=c.close_time)


def _load(sym: str, tf: str, path: Path) -> Candles:
    z = np.load(path)
    return Candles(sym, tf, z["ot"], z["o"], z["h"], z["l"], z["c"], z["v"], z["q"], z["ct"], time.time())


async def download(cache: Path, base_url: str) -> None:
    ad = SpotAdapter(BinanceClient(base_url, "spot"))
    for tf, bars in HISTORY.items():
        for s in SYMBOLS:
            p = cache / f"{s}_{tf}.npz"
            if p.exists() and time.time() - p.stat().st_mtime < 86400:
                continue
            c = await fetch_history(ad, s, tf, bars)
            _save(c, p)
            print(f"downloaded {s} {tf}: {len(c)} bars", flush=True)
    await ad.data.close()


def _job(args):
    tf, strategy, scenario, cache = args
    data = {s: _load(s, tf, Path(cache) / f"{s}_{tf}.npz") for s in SYMBOLS}
    t0 = time.time()
    r = wf.walk_forward(data, tf, strategy, scenario)
    r["runtime_s"] = round(time.time() - t0, 1)
    r["data"] = {s: [int(c.open_time[0]), int(c.close_time[-1]), len(c)] for s, c in data.items()}
    return r


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="record promotion decisions in the config DB")
    ap.add_argument("--out", default=None)
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    a = ap.parse_args()
    s = load_settings()
    cache = s.data_dir / "history"
    cache.mkdir(parents=True, exist_ok=True)
    asyncio.run(download(cache, s.endpoints.spot_data))
    jobs = [(tf, st, sc, str(cache)) for tf in HISTORY for st in wf.GRIDS for sc in wf.SCENARIOS]
    t0 = time.time()
    results = []
    with ProcessPoolExecutor(a.workers) as ex:
        for r in ex.map(_job, jobs):
            results.append(r)
            o = r["oos"]
            print(f"{r['timeframe']:>3} {r['strategy']:<10} {r['scenario']:<7} OOS trades {o['trades']:>4} "
                  f"netR {o['net_r']:+7.1f} PF {o['pf'] if o['pf'] is None else round(o['pf'], 2)} "
                  f"promote={r['promote']} ({r['runtime_s']}s)", flush=True)
    out = {"generated": time.time(), "runtime_s": round(time.time() - t0, 1), "promotion_criteria": wf.PROMOTION,
           "windows": wf.WINDOWS, "scenarios": wf.SCENARIOS, "grids": wf.GRIDS, "symbols": SYMBOLS,
           "results": results}
    path = Path(a.out or (s.data_dir / "walkforward.json"))
    path.write_text(json.dumps(out, default=str))
    md = render(out)
    docs = Path(__file__).resolve().parents[3] / "docs" / "strategy_report.md"
    docs.write_text(md)
    print(f"wrote {path} and {docs}")
    if a.apply:
        from ..config_store import ConfigStore
        from ..db import Database
        db = Database(s.db_path)
        cfg = ConfigStore(db)
        patch = {}
        for st in wf.GRIDS:
            winners = [r for r in results if r["strategy"] == st and r["promote"] and r["scenario"] != "nocost"]
            if winners:
                best = max(winners, key=lambda r: r["oos"]["net_r"])
                patch[st] = {"promoted": True, "params": best["latest_params"], "promotion": {
                    "timeframes": sorted({r["timeframe"] for r in winners}),
                    "markets": sorted({r["scenario"] for r in winners}), "ts": time.time(),
                    "evidence": {f"{r['timeframe']}/{r['scenario']}": r["oos"] for r in winners}}}
            else:
                patch[st] = {"promoted": False, "promotion": {"ts": time.time(), "reason": "no configuration met all "
                                                              "walk-forward promotion criteria"}}
        cfg.update("strategies", patch, "walk-forward")
        print("promotion decisions recorded:", {k: v["promoted"] for k, v in patch.items()})


if __name__ == "__main__":
    main()
