"""Markdown rendering of walk-forward results."""
from __future__ import annotations

import datetime as dt


def _f(v, d=2, pct=False):
    if v is None:
        return "—"
    return f"{v:.{d}f}{'%' if pct else ''}"


def _d(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d")


def render(out: dict) -> str:
    res = out["results"]
    pc = out["promotion_criteria"]
    L = ["# Strategy walk-forward report", "",
         f"Generated {dt.datetime.fromtimestamp(out['generated'], dt.timezone.utc):%Y-%m-%d %H:%M UTC} · "
         f"runtime {out['runtime_s']}s · symbols {', '.join(out['symbols'])} · source: Binance Spot public klines", ""]
    any_r = res[0] if res else None
    if any_r:
        L.append("Data ranges (UTC): " + "; ".join(
            f"{r['timeframe']} {_d(min(v[0] for v in r['data'].values()))} → {_d(max(v[1] for v in r['data'].values()))}"
            for r in {r['timeframe']: r for r in res}.values()))
        L.append("")
    L += ["## Method", "",
          "* Signals use closed candles only; entry at the next bar's open with 3 bps adverse slippage.",
          "* Mirrors the live pipeline: higher-timeframe confirmation (timeframes above the entry timeframe), "
          "minimum score 0.55, and the Sizer gate (net reward:risk after fees and slippage ≥ 1.5).",
          "* Stop first when a bar touches both levels; stop fills at the stop or a worse gap-open.",
          "* Rolling walk-forward: parameters are selected on the train window only (max total net R, ≥ 10 trades) "
          "and scored on the next unseen test window. Windows (train/test bars): "
          + ", ".join(f"{k} {v[0]}/{v[1]}" for k, v in out["windows"].items()) + ".",
          "* Cost scenarios: **spot** (0.10% taker/maker, long-only), **futures** (0.05% taker / 0.02% maker, "
          "long+short, ≈0.01% funding per 8 h), **nocost** (diagnostic only: zero fees, never promotable).",
          "* Results are in R (1R = planned risk incl. costs). Equity statistics assume 0.5% risk per trade.", "",
          "**Promotion criteria (fixed before results were generated, all required):** "
          f"OOS trades ≥ {pc['min_trades']}, profit factor ≥ {pc['min_pf']}, expectancy ≥ {pc['min_expectancy_r']} R, "
          f"positive in ≥ {int(pc['min_positive_fold_ratio'] * 100)}% of traded folds, positive on ≥ "
          f"{pc['min_positive_symbols']} of 5 symbols, max drawdown ≤ {pc['max_drawdown_pct']}%.", ""]
    promoted = [r for r in res if r["promote"] and r["scenario"] != "nocost"]
    L += ["## Verdict", ""]
    if promoted:
        for r in promoted:
            L.append(f"* **PROMOTED** {r['strategy']} {r['timeframe']} ({r['scenario']}): {r['oos']['trades']} OOS trades, "
                     f"PF {_f(r['oos']['pf'])}, expectancy {_f(r['oos']['expectancy_r'], 3)} R")
    else:
        L.append("* **No strategy/timeframe/cost combination met the promotion criteria.** All strategies remain "
                 "available for research but are **disabled for automated testnet/live trading**.")
    L.append("")
    L += ["## Out-of-sample results", "",
          "| Strategy | TF | Scenario | OOS trades | Win % | Breakeven win % | PF | Expectancy R | Net R | Return % | Max DD % | +Folds | +Symbols | Promote |",
          "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|:-:|"]
    for r in sorted(res, key=lambda r: (r["strategy"], r["timeframe"], r["scenario"])):
        o = r["oos"]
        L.append(f"| {r['strategy']} | {r['timeframe']} | {r['scenario']} | {o['trades']} | {_f(o['win_rate'], 1)} | "
                 f"{_f(r['breakeven_win_rate'], 1)} | {_f(o['pf'])} | {_f(o['expectancy_r'], 3)} | {_f(o['net_r'], 1)} | "
                 f"{_f(o['return_pct'], 1)} | {_f(o['max_dd_pct'], 1)} | {r['positive_folds']} | {r['positive_symbols']}/5 | "
                 f"{'✅' if r['promote'] and r['scenario'] != 'nocost' else '❌'} |")
    L += ["", "## Cost drag (net R: nocost → futures → spot)", "",
          "| Strategy | TF | nocost | futures | spot | avg cost per trade (R, spot) |", "|---|---|---:|---:|---:|---:|"]
    idx = {(r["strategy"], r["timeframe"], r["scenario"]): r for r in res}
    for st in out["grids"]:
        for tf in out["windows"]:
            g = lambda sc: idx.get((st, tf, sc))
            if not g("spot"):
                continue
            L.append(f"| {st} | {tf} | {_f(g('nocost')['oos']['net_r'], 1)} | {_f(g('futures')['oos']['net_r'], 1)} | "
                     f"{_f(g('spot')['oos']['net_r'], 1)} | {_f(g('spot')['oos']['avg_cost_r'], 3)} |")
    L += ["", "## Signal funnel (first parameter set, all history, spot scenario)", "",
          "| Strategy | TF | raw signals | blocked: spot long-only | blocked: score < 0.55 | blocked: net R:R < 1.5 |",
          "|---|---|---:|---:|---:|---:|"]
    for st in out["grids"]:
        for tf in out["windows"]:
            r = idx.get((st, tf, "spot"))
            if r:
                f = r["signal_funnel_first_combo"]
                L.append(f"| {st} | {tf} | {f['signals']} | {f['gated_short']} | {f['gated_score']} | {f['gated_rr']} |")
    L += ["", "## Regime breakdown (OOS, futures scenario)", "",
          "| Strategy | TF | trend_up R (n) | trend_down R (n) | range R (n) | high_vol R (n) | low_vol R (n) | LONG R (n) | SHORT R (n) |",
          "|---|---|---|---|---|---|---|---|---|"]
    for st in out["grids"]:
        for tf in out["windows"]:
            r = idx.get((st, tf, "futures"))
            if not r:
                continue
            cell = lambda m: f"{_f(m['net_r'], 1)} ({m['trades']})"
            g, v, d = r["oos_by_regime"], r["oos_by_volatility"], r["oos_by_direction"]
            L.append(f"| {st} | {tf} | {cell(g['trend_up'])} | {cell(g['trend_down'])} | {cell(g['range'])} | "
                     f"{cell(v['high_vol'])} | {cell(v['low_vol'])} | {cell(d['LONG'])} | {cell(d['SHORT'])} |")
    L += ["", "## In-sample vs out-of-sample (overfitting check, futures scenario)", "",
          "| Strategy | TF | best in-sample net R (full history, hindsight) | walk-forward OOS net R |", "|---|---|---:|---:|"]
    for st in out["grids"]:
        for tf in out["windows"]:
            r = idx.get((st, tf, "futures"))
            if r:
                L.append(f"| {st} | {tf} | {_f(r['best_in_sample']['net_r'], 1)} | {_f(r['oos']['net_r'], 1)} |")
    L += ["", "## Concentration (futures scenario)", "",
          "| Strategy | TF | OOS net R | best symbol | its net R | net R without it |", "|---|---|---:|---|---:|---:|"]
    for st in out["grids"]:
        for tf in out["windows"]:
            r = idx.get((st, tf, "futures"))
            if r and r.get("concentration"):
                cc = r["concentration"]
                L.append(f"| {st} | {tf} | {_f(r['oos']['net_r'], 1)} | {cc['top_symbol']} | {_f(cc['top_symbol_net_r'], 1)} | "
                         f"{_f(cc['net_r_excluding_top'], 1)} |")
    L += ["", "## Parameter stability (futures scenario): parameter set chosen in each fold", ""]
    for st in out["grids"]:
        for tf in out["windows"]:
            r = idx.get((st, tf, "futures"))
            if r:
                ps = [",".join(f"{k}={v:g}" for k, v in f["params"].items() if k in out["grids"][st]) if f["params"] else "none"
                      for f in r["folds"]]
                L.append(f"* {st} {tf}: {len(set(ps))} distinct sets over {len(ps)} folds")
    L += ["", "## Per-symbol OOS net R (futures scenario)", "",
          "| Strategy | TF | " + " | ".join(out["symbols"]) + " |", "|---|---|" + "---:|" * len(out["symbols"])]
    for st in out["grids"]:
        for tf in out["windows"]:
            r = idx.get((st, tf, "futures"))
            if r:
                L.append(f"| {st} | {tf} | " + " | ".join(
                    f"{_f(r['oos_by_symbol'][s]['net_r'], 1)} ({r['oos_by_symbol'][s]['trades']})" for s in out["symbols"]) + " |")
    L.append("")
    return "\n".join(L)
