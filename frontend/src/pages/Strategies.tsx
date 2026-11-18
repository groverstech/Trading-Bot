import { AreaSeries, ColorType, createChart, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { Panel } from "../components/ui";
import { num, pct, pnlClass, spct } from "../fmt";

type Strat = { info: string; enabled: boolean; params: Record<string, number> };
type BT = Record<string, any>;
const COLORS = ["#21e6ff", "#a875ff", "#ff9a3d", "#16e08a", "#ffd03a"];

export function Strategies({ toast }: { toast: (m: string, e?: boolean) => void }) {
  const [strats, setStrats] = useState<Record<string, Strat>>({});
  const [draft, setDraft] = useState<Record<string, Strat>>({});
  const [bt, setBt] = useState({ market: "spot", symbol: "BTCUSDT", timeframe: "1h", bars: 1500 });
  const [pick, setPick] = useState<string[]>(["momentum", "breakout", "trend", "volume", "volatility"]);
  const [results, setResults] = useState<BT[]>([]);
  const [source, setSource] = useState("");
  const [busy, setBusy] = useState(false);
  const load = () => api.get<Record<string, Strat>>("/api/strategies").then((s) => { setStrats(s); setDraft(structuredClone(s)); });
  useEffect(() => { load(); }, []);

  const save = async (name: string) => {
    try {
      await api.put("/api/config/strategies", { [name]: { enabled: draft[name].enabled, params: draft[name].params } });
      // note: promotion can only be changed by walk-forward validation (python -m grokbot.research --apply)
      toast(`${name} saved (audited)`); load();
    } catch (e) { toast((e as Error).message, true); }
  };
  const run = async () => {
    setBusy(true);
    try {
      const r = await api.post<{ results: BT[]; data_source: string }>("/api/backtest", { ...bt, strategies: pick });
      setResults(r.results); setSource(r.data_source);
    } catch (e) { toast((e as Error).message, true); } finally { setBusy(false); }
  };

  return (
    <>
      <Research />
      <div className="grid-3" style={{ gridTemplateColumns: "repeat(5, minmax(0,1fr))" }}>
        {Object.entries(draft).map(([name, s]) => (
          <Panel key={name} title={<>{name} <span className={(s as any).promoted ? "badge-promoted" : "badge-research"}>
            {(s as any).promoted ? "· PROMOTED" : "· RESEARCH ONLY"}</span></>} accent={s.enabled ? "var(--green)" : "var(--dim)"}
            right={<label className="check"><input type="checkbox" checked={s.enabled}
              onChange={(e) => setDraft({ ...draft, [name]: { ...s, enabled: e.target.checked } })} />enabled</label>}>
            <div className="note" style={{ minHeight: 48 }}>{s.info}</div>
            <div className="col" style={{ gap: 6, marginTop: 8 }}>
              {Object.entries(s.params).map(([k, v]) => (
                <div key={k} className="row"><span className="muted mono grow" style={{ fontSize: 10.5 }}>{k}</span>
                  <input className="input" style={{ width: 80 }} type="number" step="any" value={v}
                    onChange={(e) => setDraft({ ...draft, [name]: { ...s, params: { ...s.params, [k]: Number(e.target.value) } } })} /></div>
              ))}
            </div>
            <button className="btn primary" style={{ marginTop: 10 }}
              disabled={JSON.stringify(s) === JSON.stringify(strats[name])} onClick={() => save(name)}>Save</button>
          </Panel>
        ))}
      </div>

      <Panel title="Backtest & compare (real Binance history)" accent="var(--purple)">
        <div className="row wrap" style={{ gap: 12 }}>
          <div className="field"><label>Market</label><select className="input" value={bt.market} onChange={(e) => setBt({ ...bt, market: e.target.value })}>
            <option value="spot">Spot</option><option value="futures">USDⓈ-M Futures</option></select></div>
          <div className="field"><label>Symbol</label><input className="input" style={{ width: 130 }} value={bt.symbol}
            onChange={(e) => setBt({ ...bt, symbol: e.target.value.toUpperCase() })} /></div>
          <div className="field"><label>Timeframe</label><select className="input" value={bt.timeframe} onChange={(e) => setBt({ ...bt, timeframe: e.target.value })}>
            {["5m", "15m", "1h", "4h", "1d"].map((t) => <option key={t}>{t}</option>)}</select></div>
          <div className="field"><label>Bars</label><input className="input" type="number" style={{ width: 100 }} value={bt.bars}
            onChange={(e) => setBt({ ...bt, bars: Number(e.target.value) })} /></div>
          <div className="field"><label>Strategies</label><div className="row wrap">{Object.keys(strats).map((n) => (
            <label key={n} className="check"><input type="checkbox" checked={pick.includes(n)}
              onChange={(e) => setPick(e.target.checked ? [...pick, n] : pick.filter((x) => x !== n))} />{n}</label>))}</div></div>
          <span className="grow" />
          <button className="btn go" disabled={busy || !pick.length} onClick={run}>{busy ? "Running…" : "Run backtest"}</button>
        </div>
        <div className="note" style={{ marginTop: 8 }}>
          Signals use closed candles only; entries fill at the <b>next bar open</b>; when a bar touches both stop and target the
          <b> stop is assumed first</b>; taker fees and slippage are charged on both sides; no higher-timeframe filter.
          Results are hypothetical. Historical profitability does not imply future profitability.
        </div>
      </Panel>

      {results.length > 0 && (
        <div className="grid-2" style={{ gridTemplateColumns: "minmax(0,1.2fr) minmax(0,1fr)" }}>
          <Panel title={`Comparison · ${results[0].symbol} ${results[0].timeframe} · ${source}`} flush>
            <table className="t"><thead><tr><th>Strategy</th><th className="r">Trades</th><th className="r">Win%</th><th className="r">PF</th>
              <th className="r">Return</th><th className="r">Max DD</th><th className="r">Avg bars</th><th className="r">Fees</th></tr></thead>
              <tbody>{results.map((r, i) => (
                <tr key={r.strategy}><td style={{ color: COLORS[i % 5] }}>● {r.strategy}</td><td className="r">{r.trades}</td>
                  <td className="r">{r.win_rate == null ? "—" : num(r.win_rate, 1)}</td><td className="r">{r.profit_factor == null ? "—" : num(r.profit_factor)}</td>
                  <td className={`r ${pnlClass(r.net_return_pct)}`}>{spct(r.net_return_pct)}</td><td className="r neg">{pct(r.max_drawdown_pct)}</td>
                  <td className="r">{r.avg_bars_held == null ? "—" : num(r.avg_bars_held, 1)}</td><td className="r">{num(r.fees_paid, 2)}</td></tr>))}</tbody></table>
            <div className="note" style={{ padding: "8px 12px" }}>Starting equity 1,000 (normalised) · risk {results[0].assumptions.risk_pct}% per trade ·
              fee {(results[0].assumptions.fee_rate * 100).toFixed(3)}% · slippage {results[0].assumptions.slippage_bps} bps</div>
          </Panel>
          <Panel title="Backtest equity curves"><BtChart results={results} /></Panel>
        </div>
      )}
    </>
  );
}

function BtChart({ results }: { results: BT[] }) {
  const el = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!el.current) return;
    const c = createChart(el.current, {
      layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: "#7a8696", fontSize: 10 },
      grid: { vertLines: { color: "rgba(28,35,45,.5)" }, horzLines: { color: "rgba(28,35,45,.5)" } }, autoSize: true,
      timeScale: { borderColor: "#1c232d" }, rightPriceScale: { borderColor: "#1c232d" },
    });
    results.forEach((r, i) => {
      const s = c.addSeries(AreaSeries, { lineColor: COLORS[i % 5], topColor: "transparent", bottomColor: "transparent", lineWidth: 2 });
      const seen = new Set<number>();
      s.setData(r.equity_curve.map((p: { t: number; equity: number }) => ({ time: Math.floor(p.t / 1000) as UTCTimestamp, value: p.equity }))
        .filter((p: { time: number }) => !seen.has(p.time) && (seen.add(p.time), true)));
    });
    c.timeScale().fitContent();
    return () => c.remove();
  }, [results]);
  return <div ref={el} className="chart" style={{ height: 300 }} />;
}


type RRow = { strategy: string; timeframe: string; scenario: string; promote: boolean; positive_folds: string; positive_symbols: number;
  breakeven_win_rate: number | null; checks: Record<string, boolean>;
  oos: { trades: number; pf: number | null; expectancy_r: number | null; net_r: number; win_rate: number | null; max_dd_pct: number };
  concentration?: { top_symbol: string; top_symbol_net_r: number; net_r_excluding_top: number } };

function Research() {
  const [d, setD] = useState<{ available: boolean; generated?: number; rows?: RRow[]; criteria?: Record<string, number>;
    paper_research_mode?: boolean; how?: string } | null>(null);
  const [scn, setScn] = useState("futures");
  useEffect(() => { api.get<typeof d>("/api/research").then(setD).catch(() => {}); }, []);
  if (!d) return null;
  if (!d.available) return <Panel title="Walk-forward validation"><div className="note">No walk-forward results yet. Run <b>{d.how}</b>.</div></Panel>;
  const rows = (d.rows ?? []).filter((r) => r.scenario === scn).sort((a, b) => (a.strategy + a.timeframe).localeCompare(b.strategy + b.timeframe));
  const c = d.criteria!;
  return (
    <Panel title="Walk-forward validation · out-of-sample, after costs" accent="var(--yellow)" flush
      right={<div className="seg">{["futures", "spot", "nocost"].map((x) => <button key={x} className={scn === x ? "active" : ""} onClick={() => setScn(x)}>{x}</button>)}</div>}>
      <div className="note" style={{ padding: "8px 12px" }}>
        Promotion requires: ≥{c.min_trades} OOS trades · PF ≥ {c.min_pf} · expectancy ≥ {c.min_expectancy_r}R · ≥{c.min_positive_fold_ratio * 100}% positive folds ·
        ≥{c.min_positive_symbols}/5 positive symbols · max DD ≤ {c.max_drawdown_pct}%. Only promoted strategies can trade on testnet/live.
        {d.paper_research_mode ? <span className="warn"> Paper research mode is ON: unpromoted strategies may forward-test in PAPER only.</span> : null}
        {scn === "nocost" && <b className="warn"> Diagnostic scenario without fees: never promotable.</b>}
      </div>
      <div className="scroll"><table className="t research"><thead><tr><th>Strategy</th><th>TF</th><th className="r">Trades</th><th className="r">Win %</th>
        <th className="r">Breakeven %</th><th className="r">PF</th><th className="r">Exp. R</th><th className="r">Net R</th><th className="r">Max DD</th>
        <th className="r">+Folds</th><th className="r">+Symbols</th><th>Best symbol share</th><th>Failed checks</th><th>Verdict</th></tr></thead>
        <tbody>{rows.map((r) => {
          const failed = Object.entries(r.checks).filter(([, v]) => !v).map(([k]) => k);
          return (<tr key={r.strategy + r.timeframe + r.scenario}>
            <td>{r.strategy}</td><td>{r.timeframe}</td><td className="r">{r.oos.trades}</td><td className="r">{num(r.oos.win_rate, 1)}</td>
            <td className="r muted">{num(r.breakeven_win_rate, 1)}</td><td className="r">{num(r.oos.pf)}</td>
            <td className={`r ${pnlClass(r.oos.expectancy_r)}`}>{num(r.oos.expectancy_r, 3)}</td><td className={`r ${pnlClass(r.oos.net_r)}`}>{num(r.oos.net_r, 1)}</td>
            <td className="r">{pct(r.oos.max_dd_pct, 1)}</td><td className="r">{r.positive_folds}</td><td className="r">{r.positive_symbols}/5</td>
            <td className="muted">{r.concentration ? `${r.concentration.top_symbol.replace("USDT", "")} ${num(r.concentration.top_symbol_net_r, 1)}R (rest ${num(r.concentration.net_r_excluding_top, 1)}R)` : "—"}</td>
            <td className="neg" style={{ whiteSpace: "normal", maxWidth: 260 }}>{failed.join(", ") || "—"}</td>
            <td className={`promote ${r.promote && r.scenario !== "nocost" ? "pos" : "neg"}`}>{r.promote && r.scenario !== "nocost" ? "PROMOTE" : "REJECT"}</td></tr>);
        })}</tbody></table></div>
    </Panel>
  );
}
