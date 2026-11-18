import { Fragment, useEffect, useState } from "react";
import { api } from "../api";
import { EquityChart } from "../components/charts";
import { Panel } from "../components/ui";
import { duration, num, pct, pnlClass, signed, spct } from "../fmt";
import type { Mode } from "../types";

type Stats = Record<string, number | null>;

function StatGrid({ s, title }: { s: Stats; title: string }) {
  const rows: [string, string, number | null | undefined][] = [
    ["Trades", String(s.trades ?? 0), null], ["Wins / losses", `${s.wins ?? 0} / ${s.losses ?? 0}`, null],
    ["Win rate", s.win_rate == null ? "—" : pct(s.win_rate, 1), null], ["Net P&L", signed(s.net_pnl, 4), s.net_pnl],
    ["Profit factor", s.profit_factor == null ? "—" : num(s.profit_factor), null],
    ["Avg win", s.avg_win == null ? "—" : signed(s.avg_win, 4), s.avg_win], ["Avg loss", s.avg_loss == null ? "—" : signed(s.avg_loss, 4), s.avg_loss],
    ["Expectancy", s.expectancy == null ? "—" : signed(s.expectancy, 4), s.expectancy],
    ["Largest win", s.largest_win == null ? "—" : signed(s.largest_win, 4), s.largest_win],
    ["Largest loss", s.largest_loss == null ? "—" : signed(s.largest_loss, 4), s.largest_loss],
    ["Avg holding", s.avg_holding_s == null ? "—" : duration(s.avg_holding_s), null],
    ["Fees", num(s.fees, 4), null], ["Slippage", num(s.slippage, 4), null], ["Funding", signed(s.funding, 4), s.funding],
  ];
  return (
    <Panel title={title}>
      <div className="kv">{rows.map(([k, v, c]) => <Fragment key={k}><span>{k}</span><span className={pnlClass(c ?? 0)}>{v}</span></Fragment>)}</div>
    </Panel>
  );
}

export function Analytics({ mode: current }: { mode: Mode }) {
  const [mode, setMode] = useState<Mode>(current);
  const [a, setA] = useState<Record<string, any> | null>(null);
  useEffect(() => {
    const load = () => api.get<Record<string, any>>(`/api/analytics?mode=${mode}`).then(setA).catch(() => {});
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, [mode]);
  return (
    <>
      <div className="row">
        <div className="seg">{(["paper", "testnet", "live"] as Mode[]).map((m) => (
          <button key={m} className={mode === m ? "active" : ""} onClick={() => setMode(m)}>{m}</button>))}</div>
        <span className={`banner ${mode === "live" ? "red" : mode === "testnet" ? "orange" : "cyan"}`} style={{ margin: 0, flex: 1 }}>
          {mode === "paper" ? "SIMULATED RESULTS: paper trading on real market data with simulated fills. Not real performance." :
            mode === "testnet" ? "TESTNET RESULTS: Binance test environment; test funds have no value and testnet liquidity differs from production." :
              "LIVE RESULTS: real-money trading on Binance."}
          {" "}All statistics are computed only from recorded trades and equity snapshots for this mode.
        </span>
      </div>
      {a && (
        <>
          <div className="perf" style={{ gridTemplateColumns: "repeat(6, minmax(0,1fr))" }}>
            <div><label>Starting equity</label><b>{num(a.starting_equity)}</b><small>USDT</small></div>
            <div><label>Current equity</label><b className="cyan">{num(a.current_equity)}</b><small>last snapshot</small></div>
            <div><label>Total return</label><b className={pnlClass(a.total_return_pct)}>{spct(a.total_return_pct)}</b><small>&nbsp;</small></div>
            <div><label>Max drawdown</label><b className="neg">{pct(a.max_drawdown_pct)}</b><small>current {pct(a.current_drawdown_pct)}</small></div>
            <div><label>Sharpe</label><b>{a.sharpe == null ? "—" : num(a.sharpe)}</b><small title={a.sharpe_note}>{a.sharpe_note}</small></div>
            <div><label>Closed trades</label><b>{a.consolidated.trades}</b><small>{a.consolidated.trades < 30 && a.consolidated.trades > 0 ? "sample too small to judge" : " "}</small></div>
          </div>
          <div className="grid-3">
            <StatGrid s={a.consolidated} title="Consolidated" />
            <StatGrid s={a.spot} title="Spot" />
            <StatGrid s={a.futures} title="USDⓈ-M Futures" />
          </div>
          <div className="grid-2" style={{ height: 320 }}>
            <Panel title="Equity curve"><EquityChart mode={mode} refreshKey={0} /></Panel>
            <Panel title="Strategy performance" flush>
              {Object.keys(a.by_strategy).length ? (
                <table className="t"><thead><tr><th>Strategy</th><th className="r">Trades</th><th className="r">Win%</th><th className="r">PF</th><th className="r">Net P&L</th><th className="r">Fees</th></tr></thead>
                  <tbody>{Object.entries(a.by_strategy as Record<string, Stats>).map(([k, s]) => (
                    <tr key={k}><td>{k}</td><td className="r">{s.trades}</td><td className="r">{s.win_rate == null ? "—" : num(s.win_rate, 1)}</td>
                      <td className="r">{s.profit_factor == null ? "—" : num(s.profit_factor)}</td><td className={`r ${pnlClass(s.net_pnl)}`}>{signed(s.net_pnl, 4)}</td>
                      <td className="r">{num(s.fees, 4)}</td></tr>))}</tbody></table>
              ) : <div className="empty">No closed trades in this mode yet.</div>}
            </Panel>
          </div>
          <div className="grid-3">
            <Panel title="Daily returns" flush><Returns rows={a.daily_returns} /></Panel>
            <Panel title="Weekly returns" flush><Returns rows={a.weekly_returns} /></Panel>
            <Panel title="Monthly returns" flush><Returns rows={a.monthly_returns} /></Panel>
          </div>
          <div className="grid-2">
            <Panel title="Pipeline outcomes (opportunities by final stage)">
              <div className="kv">{Object.entries(a.pipeline as Record<string, number>).map(([k, v]) => <Fragment key={k}><span>{k}</span><span>{v}</span></Fragment>)}</div>
            </Panel>
            <Panel title="Agent activity & rejections">
              <div className="kv">
                {Object.entries(a.agent_activity as Record<string, number>).map(([k, v]) => <Fragment key={k}><span>{k} events</span><span>{v.toLocaleString()}</span></Fragment>)}
                {(a.rejections as { agent: string; event: string; n: number }[]).map((r) => <Fragment key={r.agent + r.event}><span>{r.agent} · {r.event}</span><span className="neg">{r.n}</span></Fragment>)}
              </div>
            </Panel>
          </div>
        </>
      )}
    </>
  );
}

function Returns({ rows }: { rows: { period: string; equity: number; return_pct: number }[] }) {
  if (!rows?.length) return <div className="empty">No data yet.</div>;
  return (
    <div className="scroll" style={{ maxHeight: 220 }}>
      <table className="t"><thead><tr><th>Period</th><th className="r">Equity</th><th className="r">Return</th></tr></thead>
        <tbody>{[...rows].reverse().map((r) => (<tr key={r.period}><td>{r.period}</td><td className="r">{num(r.equity)}</td>
          <td className={`r ${pnlClass(r.return_pct)}`}>{spct(r.return_pct, 3)}</td></tr>))}</tbody></table>
    </div>
  );
}
