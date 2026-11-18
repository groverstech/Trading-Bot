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
      <div className="grid-3" style={{ gridTemplateColumns: "repeat(5, minmax(0,1fr))" }}>
        {Object.entries(draft).map(([name, s]) => (
          <Panel key={name} title={name} accent={s.enabled ? "var(--green)" : "var(--dim)"}
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
