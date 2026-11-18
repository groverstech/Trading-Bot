import { useMemo, useRef } from "react";
import { api } from "../api";
import { ago, clock, compact, num, pct, pnlClass, price, signed, spct, usdt } from "../fmt";
import type { AgentCard, AgentEvent, PipelineRow, Position, Snapshot, StageState, Ticker } from "../types";
import { AGENT_COLORS, Dir, Meter, Mkt, Panel } from "./ui";

/* ------------------------------------------------------------------ PERFORMANCE */
export function PerfStrip({ s }: { s: Snapshot }) {
  const p = s.portfolio;
  const perf = s.performance;
  const items: [string, React.ReactNode, React.ReactNode?][] = [
    ["Starting capital", <>{num(p.starting_usdt)}</>, p.capital ? `₹${p.capital.starting_inr.toLocaleString("en-IN")} @ ${num(p.capital.usd_inr, 2)}` : "not initialised"],
    ["Current balance", num(p.available_total), "available cash + free margin"],
    ["Total equity", <span className="cyan">{num(p.total_equity)}</span>, p.inr.total_equity ? `≈ ₹${Math.round(p.inr.total_equity).toLocaleString("en-IN")}` : "USDT"],
    ["Total P&L", <span className={pnlClass(p.total_pnl)}>{signed(p.total_pnl)}</span>,
      p.starting_usdt ? spct((p.total_pnl / p.starting_usdt) * 100) : ""],
    ["Realized P&L", <span className={pnlClass(p.realized_pnl)}>{signed(p.realized_pnl)}</span>, `fees ${num(p.fees_paid)}`],
    ["Unrealized P&L", <span className={pnlClass(p.unrealized_pnl)}>{signed(p.unrealized_pnl)}</span>, `funding ${signed(p.funding_net, 4)}`],
    ["Winning trades", <span className="pos">{perf.wins}</span>, `of ${perf.closed} closed`],
    ["Losing trades", <span className="neg">{perf.losses}</span>, ""],
    ["Win rate", perf.win_rate === null ? "—" : pct(perf.win_rate, 1), perf.closed < 20 && perf.closed > 0 ? "small sample" : ""],
    ["Active positions", perf.active_positions, `max ${s.risk.config.max_positions}`],
  ];
  return (
    <div className="perf">
      {items.map(([l, v, sub]) => (
        <div key={l}><label>{l}</label><b>{v}</b><small>{sub || " "}</small></div>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ AGENTS */
const AGENT_KPIS: Record<string, (m: Record<string, any>) => [string, string][]> = {
  scanner: (m) => [["Cycles", String(m.cycles ?? 0)], ["Signals", String(m.signals ?? 0)], ["Markets", String(m.markets_scanned ?? 0)]],
  rugcheck: (m) => [["Checked", String(m.checked ?? 0)], ["Pass", String(m.approved ?? 0)], ["Reject", String(m.rejected ?? 0)]],
  sizer: (m) => [["Sized", String(m.sized ?? 0)], ["Reject", String(m.rejected ?? 0)], ["Avg risk", num(m.avg_risk_usdt ?? 0)]],
  executor: (m) => [["Orders", String(m.orders ?? 0)], ["Filled", String(m.filled ?? 0)], ["Closed", String(m.closed ?? 0)]],
  logger: (m) => [["Events", compact(Number(m.events ?? 0))], ["Trades", String(m.trades_recorded ?? 0)], ["Store", "SQLite"]],
  equity: (m) => [["Win%", m.win_rate == null ? "—" : num(m.win_rate, 1)], ["PF", m.profit_factor == null ? "—" : num(m.profit_factor)],
    ["MaxDD", m.max_dd_pct == null ? "—" : pct(m.max_dd_pct)]],
};

export function AgentCards({ agents, onOpen }: { agents: AgentCard[]; onOpen: (a: string) => void }) {
  return (
    <div className="agents">
      {agents.map((a, i) => (
        <div key={a.name} className={`agent ${a.name}`} onClick={() => onOpen(a.name)} title="Open agent activity & logs">
          <div className="agent-top">
            <span><span className="agent-idx">0{i + 1}</span><span className="agent-name">{a.title}</span></span>
            <span className={`status ${a.status}`}><span className="dot" />{a.status}</span>
          </div>
          <div className="agent-act">{a.activity}</div>
          <div className="agent-dec">Last: <b>{a.last_decision}</b></div>
          <div className="agent-foot">
            {AGENT_KPIS[a.name](a.metrics as Record<string, any>).map(([l, v]) => (
              <div key={l}><label>{l}</label><span>{v}</span></div>
            ))}
          </div>
          <div className="row" style={{ marginTop: 6, justifyContent: "space-between" }}>
            <span className="dim mono" style={{ fontSize: 9.5 }}>{a.ops.toLocaleString()} ops{a.errors ? ` · ${a.errors} err` : ""}</span>
            <span className="dim mono" style={{ fontSize: 9.5 }}>{ago(a.last_ts)}</span>
          </div>
        </div>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ PIPELINE */
const STAGES: [keyof PipelineRow["stages"], string][] = [
  ["scanner", "SCAN"], ["rugcheck", "RUG"], ["sizer", "SIZE"], ["risk", "RISK"], ["executor", "EXEC"], ["logger", "LOG"], ["equity", "EQTY"],
];
const ICON: Record<StageState, string> = { approved: "✓", rejected: "✕", failed: "!", pending: "·", working: "◌", waiting: "⏸", skipped: "–" };

export function Pipeline({ rows, onOpen }: { rows: PipelineRow[]; onOpen: (id: string) => void }) {
  return (
    <div className="scroll" style={{ flex: 1, minHeight: 0 }}>
      <div className="pipe-head">
        <span>Time</span><span>Opportunity</span>
        {STAGES.map(([, l]) => <span key={l}>{l}</span>)}
      </div>
      {rows.length === 0 && (
        <div className="empty">No opportunities yet. The scanner only proposes trades when strategy conditions are met.</div>
      )}
      {rows.map((r) => (
        <div key={r.id} className="pipe-row" onClick={() => onOpen(r.id)}>
          <span className="dim">{clock(r.ts)}</span>
          <span className="pipe-sym">
            <b>{r.symbol} <Dir d={r.direction} /></b>
            <small>{r.market === "futures" ? "PERP" : "SPOT"} · {r.strategy} · {r.timeframe} · {num(r.score, 2)}</small>
          </span>
          {STAGES.map(([k, l]) => (
            <span className="stage" key={k}>
              <span className={`chip ${r.stages[k]}`} title={`${l}: ${r.stages[k]}`}>{ICON[r.stages[k]]}</span>
            </span>
          ))}
          <span className="pipe-detail">
            {r.reason ? <span className="neg">{r.stage}: {r.reason}</span> :
              <>
                {r.stage} · entry {price(r.entry)} · SL {price(r.stop)} · TP {price(r.target)}
                {r.qty ? ` · qty ${r.qty} (${num(r.notional)} USDT, risk ${num(r.risk_usdt)}${r.leverage && r.market === "futures" ? `, ${r.leverage}x` : ""})` : ""}
                {r.fill_price ? ` · filled @ ${price(Number(r.fill_price))}` : ""}
              </>}
          </span>
        </div>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ POSITIONS */
export function PositionsTable({ rows, closed, onOpen, toast }: {
  rows: Position[]; closed?: boolean; onOpen: (id: string) => void; toast?: (m: string, e?: boolean) => void;
}) {
  if (!rows.length) return <div className="empty">{closed ? "No closed trades yet." : "No open positions."}</div>;
  return (
    <div className="scroll">
      <table className="t">
        <thead><tr>
          <th>Pair</th><th>Mkt</th><th>Dir</th><th className="r">Entry</th><th className="r">{closed ? "Exit" : "Price"}</th>
          <th className="r">Size</th><th className="r">Stop</th><th className="r">Target</th><th className="r">Lev</th>
          <th className="r">Margin</th><th className="r">{closed ? "Net P&L" : "uP&L"}</th><th>{closed ? "Reason" : "Status"}</th><th />
        </tr></thead>
        <tbody>
          {rows.map((p) => {
            const v = closed ? p.realized : p.unrealized;
            return (
              <tr key={p.trade_id} className="click" onClick={() => onOpen(p.trade_id)}>
                <td><b>{p.symbol}</b> {p.mode !== "paper" && <span className="tag">{p.mode}</span>}</td>
                <td><Mkt m={p.market} /></td><td><Dir d={p.direction} /></td>
                <td className="r">{price(p.entry)}</td>
                <td className="r">{price(closed ? p.exit_price : p.price)}</td>
                <td className="r">{p.qty} <span className="dim">({num(p.notional)})</span></td>
                <td className="r neg">{price(p.stop)}</td><td className="r pos">{price(p.target)}</td>
                <td className="r">{p.market === "futures" ? `${p.leverage}x` : "—"}</td>
                <td className="r">{p.market === "futures" ? num(p.margin) : "—"}</td>
                <td className={`r ${pnlClass(v)}`}>{signed(v, 3)}{!closed && <span className="dim"> {spct(p.pnl_pct)}</span>}</td>
                <td>{closed ? <span className="muted">{p.exit_reason?.replace(/_/g, " ")}</span> :
                  <span className={p.price_age > 15 ? "warn" : "pos"}>{p.status}{p.price_age > 15 ? " · stale" : ""}</span>}</td>
                <td>{!closed && toast && p.status === "OPEN" && (
                  <button className="btn sm danger" onClick={async (e) => {
                    e.stopPropagation();
                    if (!confirm(`Close ${p.symbol} ${p.direction} at market?`)) return;
                    try { await api.post(`/api/positions/${p.trade_id}/close`); toast(`Closing ${p.symbol}`); }
                    catch (err) { toast((err as Error).message, true); }
                  }}>Close</button>)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/* ------------------------------------------------------------------ ACTIVITY */
export function ActivityLog({ events, onOpen, filter }: { events: AgentEvent[]; onOpen?: (id: string) => void; filter?: string }) {
  const firstId = useRef<number | undefined>(undefined);
  const list = useMemo(() => (filter ? events.filter((e) => e.agent === filter) : events), [events, filter]);
  const fresh = firstId.current;
  firstId.current = list[0]?.id;
  if (!list.length) return <div className="empty">No agent activity recorded yet.</div>;
  return (
    <div className="log scroll" style={{ flex: 1, minHeight: 0 }}>
      {list.map((e, i) => (
        <div key={`${e.id ?? i}-${e.ts}`} className={`log-row ${e.level} ${fresh !== undefined && e.id && e.id > fresh ? "fresh" : ""}`}
          style={{ cursor: e.trade_id && onOpen ? "pointer" : undefined }}
          onClick={() => e.trade_id && onOpen?.(e.trade_id)} title={e.message}>
          <span className="ts">{clock(e.ts)}</span>
          <span className="ag" style={{ color: AGENT_COLORS[e.agent] }}>{e.agent}</span>
          <span className="msg">{e.message}</span>
        </div>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ RISK */
export function RiskPanel({ s }: { s: Snapshot }) {
  const r = s.risk;
  const c = r.config as Record<string, number>;
  const p = s.portfolio;
  const lossPct = Math.max(0, -(r.daily_pnl_pct ?? 0));
  const exposure = s.positions.filter((x) => x.mode === s.system.mode).reduce((a, x) => a + x.notional, 0);
  const allocated = p.spot.equity + p.futures.equity;
  const top = Object.entries(r.engine.top_rejections).sort((a, b) => b[1] - a[1]).slice(0, 3);
  return (
    <div className="col" style={{ gap: 10 }}>
      <Meter label="Daily loss" value={lossPct} limit={c.max_daily_loss_pct} fmt={(v) => pct(v)} />
      <Meter label="Drawdown" value={r.drawdown_pct ?? 0} limit={c.max_drawdown_pct} fmt={(v) => pct(v)} />
      <Meter label="Positions" value={s.performance.active_positions} limit={c.max_positions} fmt={(v) => String(v)} />
      <Meter label="Gross exposure" value={exposure} limit={(allocated * c.max_exposure_pct) / 100} fmt={(v) => num(v, 0)} />
      <div className="kv">
        <span>Risk / trade</span><span>{pct(c.risk_per_trade_pct)} <span className="dim">(max {pct(c.max_risk_per_trade_pct)})</span></span>
        <span>Max leverage</span><span>{c.max_leverage}x · {String(r.config.margin_mode)}</span>
        <span>Min R:R (net)</span><span>{c.min_reward_risk}</span>
        <span>Daily P&L</span><span className={pnlClass(r.daily_pnl)}>{signed(r.daily_pnl)} ({spct(r.daily_pnl_pct)})</span>
        <span>Peak equity</span><span>{num(r.peak_equity)}</span>
        <span>Emergency policy</span><span>{String(r.config.emergency_policy).replace("_", " ")}</span>
        <span>Risk approvals</span><span><span className="pos">{r.engine.approved}</span> / <span className="neg">{r.engine.rejected}</span> rejected</span>
      </div>
      {top.length > 0 && (
        <div className="note" style={{ fontSize: 10.5 }}>Top risk rejections: {top.map(([k, v]) => `${k} (${v})`).join(", ")}</div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ ALLOCATION */
export function AllocationPanel({ s }: { s: Snapshot }) {
  const p = s.portfolio;
  return (
    <div className="kv">
      <span>Spot equity</span><span>{usdt(p.spot.equity)}</span>
      <span className="dim">↳ cash / holdings</span><span className="dim">{num(p.spot.cash)} / {num(p.spot.holdings)}</span>
      <span>Futures equity</span><span>{usdt(p.futures.equity)}</span>
      <span className="dim">↳ wallet / margin</span><span className="dim">{num(p.futures.wallet)} / {num(p.futures.margin)}</span>
      <span>Reserve (unallocated)</span><span>{usdt(p.reserve.balance)}</span>
      <span>Allocated (spot+fut)</span><span>{usdt(p.allocated_total)}</span>
      <span>Available</span><span>{usdt(p.available_total)}</span>
      <span>Reserved in trades</span><span>{usdt(p.reserved_total)}</span>
      <span>USD/INR</span><span title={p.fx.source}>{num(p.fx.usd_inr, 3)} <span className="dim">· {ago(p.fx.fetched_at)}</span></span>
    </div>
  );
}

/* ------------------------------------------------------------------ WATCHLIST */
export function Watchlist({ rows, selected, onSelect }: { rows: Ticker[]; selected: string; onSelect: (k: string) => void }) {
  const sorted = [...rows].sort((a, b) => (a.market === b.market ? a.symbol.localeCompare(b.symbol) : a.market === "spot" ? -1 : 1));
  return (
    <div className="scroll" style={{ flex: 1, minHeight: 0 }}>
      <table className="t">
        <thead><tr><th>Pair</th><th className="r">Price</th><th className="r">24h</th><th className="r">RSI</th><th className="r">Spr</th></tr></thead>
        <tbody>
          {sorted.map((t) => {
            const k = `${t.market}:${t.symbol}`;
            return (
              <tr key={k} className={`click watch-row ${selected === k ? "sel" : ""}`} onClick={() => onSelect(k)}
                title={`Vol ${compact(t.quote_volume)} USDT${t.market === "futures" ? ` · funding ${(t.funding_rate * 100).toFixed(4)}%` : ""}${t.signals.length ? ` · signals: ${t.signals.join(", ")}` : ""}`}>
                <td><b>{t.symbol.replace("USDT", "")}</b> <span className={`tag ${t.market}`}>{t.market === "futures" ? "P" : "S"}</span>
                  {t.signals.length > 0 && <span className="cyan"> ●</span>}</td>
                <td className="r">{price(t.price)}</td>
                <td className={`r ${pnlClass(t.change_pct)}`}>{spct(t.change_pct)}</td>
                <td className={`r ${t.rsi && t.rsi > 70 ? "neg" : t.rsi && t.rsi < 30 ? "pos" : "muted"}`}>{t.rsi ?? "—"}</td>
                <td className={`r ${t.spread_bps > 8 ? "warn" : "muted"}`}>{num(t.spread_bps, 1)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function PendingOrders({ rows }: { rows: Record<string, any>[] }) {
  if (!rows.length) return <div className="empty">No pending or unresolved orders.</div>;
  return (
    <table className="t"><thead><tr><th>Time</th><th>Client ID</th><th>Pair</th><th>Side</th><th>Purpose</th><th className="r">Qty</th><th>Status</th><th>Error</th></tr></thead>
      <tbody>{rows.map((o) => (
        <tr key={o.client_id}><td>{clock(o.created_at)}</td><td className="muted">{o.client_id}</td><td>{o.symbol}</td><td>{o.side}</td>
          <td>{o.purpose}</td><td className="r">{o.qty}</td><td className="warn">{o.status}</td><td className="neg">{o.error ?? ""}</td></tr>
      ))}</tbody></table>
  );
}

export function Panelled(props: Parameters<typeof Panel>[0]) {
  return <Panel {...props} />;
}
