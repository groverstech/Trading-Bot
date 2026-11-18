"""Operator validation trades in PAPER mode on genuine market data.

    python -m grokbot.paper_validation [--minutes 60]

Pushes clearly-labelled VALIDATION trades (strategy="validation"; not
strategy signals) through the unchanged production pipeline (Rug Check ->
Sizer -> central Risk -> Executor) in a dedicated paper database, then lets
the Executor's monitor manage them on live prices until stop-loss or
take-profit triggers. Stop/target distances are tighter than the strategies
use so that exits occur within the session, while still passing the Sizer's
net reward:risk >= 1.5 gate after fees. Purpose: end-to-end evidence that
fills, fees, slippage, SL/TP exits, ledger, equity and audit trail work.
"""
from __future__ import annotations

import argparse
import asyncio
import time

from .engine import Engine
from .settings import load_settings
from .strategy.strategies import Signal

PLANS = [  # market, symbol, direction, stop %, target %
    ("spot", "BTCUSDT", "LONG", 0.45, 1.35),
    ("futures", "ETHUSDT", "SHORT", 0.35, 0.90),
]


async def main_async(minutes: float, plans=None, cleanup: str = "close_all") -> None:
    plans = plans or PLANS
    e = Engine(load_settings())
    await e.orch.start()  # market data, WebSocket streams, monitor loop (scanner stays stopped)
    try:
        await asyncio.sleep(8)
        e.pf.ensure_initialised("paper")
        e.equity.update("paper", "validation start")
        tids = []
        for market, sym, d, sp, tp in plans:
            t = e.md.ticker(market, sym)
            px = (t.bid + t.ask) / 2
            k = 1 if d == "LONG" else -1
            sig = Signal("validation", d, px, px * (1 - k * sp / 100), px * (1 + k * tp / 100), 1.0,
                         [f"operator validation trade: stop {sp}%, target {tp}% (not a strategy signal)"], {})
            tid = e.orch.create_trade("paper", {"market": market, "symbol": sym, "timeframe": "1m",
                                                "candle_open": int(time.time()), "signal": sig})
            await e.orch.process(tid)
            tr = e.orch.trade(tid)
            print(f"{sym} {market} {d}: {tr['stage']} {tr['reject_reason'] or ''}", flush=True)
            tids.append(tid)
        deadline = time.time() + minutes * 60
        while time.time() < deadline:
            open_ = e.db.all(f"SELECT trade_id FROM positions WHERE status!='CLOSED' AND trade_id IN ({','.join('?' * len(tids))})", tids)
            if not open_:
                break
            await asyncio.sleep(10)
        for tid in tids:
            p = e.db.one("SELECT symbol, status, exit_reason, realized_pnl FROM positions WHERE trade_id=?", (tid,))
            print(tid, p, flush=True)
        # Never leave unmonitored exposure behind: explicit end-of-test procedure.
        from . import session_end
        rep = await session_end.finalize(e, "paper", cleanup, "validation-test")
        print(session_end.render(rep), flush=True)
    finally:
        await e.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--cleanup", choices=["close_all", "cancel_pending", "report"], default="close_all",
                    help="end-of-test policy applied when the run ends")
    ap.add_argument("--plan", action="append",
                    help="market:SYMBOL:LONG|SHORT:stop_pct:target_pct (repeatable), e.g. futures:BTCUSDT:SHORT:0.35:0.9")
    a = ap.parse_args()
    plans = [(m, s, d, float(sp), float(tp)) for m, s, d, sp, tp in (x.split(":") for x in a.plan)] if a.plan else None
    asyncio.run(main_async(a.minutes, plans, a.cleanup))


if __name__ == "__main__":
    main()
