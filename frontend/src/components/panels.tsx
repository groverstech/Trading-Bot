import { useMemo, useRef, useState } from "react";
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

const ROLE: Record<string, string> = {
  scanner: "finds opportunities", rugcheck: "filters unsafe markets", sizer: "sizes risk & margin",
  executor: "places & manages orders", logger: "records every decision", equity: "tracks portfolio",
};

function ratio(a: AgentCard): [number | null, string] {
  const m = a.metrics as Record<string, number>;
  const r = (n: number, d: number) => (d > 0 ? n / d : null);
  switch (a.name) {
    case "scanner": return [r(m.signals ?? 0, (m.signals ?? 0) + (m.no_trade ?? 0)), "signal rate"];
    case "rugcheck": return [r(m.approved ?? 0, m.checked ?? 0), "pass rate"];
    case "sizer": return [r(m.sized ?? 0, (m.sized ?? 0) + (m.rejected ?? 0)), "accept rate"];
    case "executor": return [r(m.filled ?? 0, m.orders ?? 0), "fill rate"];
    case "equity": return [m.win_rate == null ? null : m.win_rate / 100, "win rate"];
    default: return [null, "persisted"];
  }
}

function decisionTone(d: string) {
  if (/REJECT|FAIL|ERROR|refused/i.test(d)) return "neg";
  if (/FILLED|APPROVED|CLOSED .*\+|^[A-Z]+ .* @ /.test(d)) return "pos";
  if (/No trade|No tradable/i.test(d)) return "muted";
  return "";
}

export function AgentCards({ agents, onOpen }: { agents: AgentCard[]; onOpen: (a: string) => void }) {
  return (
    <div className="agents">
      {agents.map((a, i) => {
        const [rv, rl] = ratio(a);
        return (
        <div key={a.name} className={`agent ${a.name} st-${a.status}`} onClick={() => onOpen(a.name)} title={`${a.title}: ${ROLE[a.name]} · click for activity & logs`}>
          <div className="agent-top">
            <span><span className="agent-idx">0{i + 1}</span><span className="agent-name">{a.title}</span></span>
            <span className={`status ${a.status}`}><span className="dot" />{a.status}</span>
          </div>
          <div className="agent-role">{ROLE[a.name]}</div>
          <div className="agent-act">{a.activity}</div>
          <div className="agent-dec">Last: <b className={decisionTone(a.last_decision)}>{a.last_decision}</b></div>
          <div className="agent-foot">
            {AGENT_KPIS[a.name](a.metrics as Record<string, any>).map(([l, v]) => (
              <div key={l}><label>{l}</label><span>{v}</span></div>
            ))}
          </div>
          <div className="agent-ratio" title={rl}>
            <i style={{ width: rv == null ? "0%" : `${Math.round(rv * 100)}%` }} />
          </div>
          <div className="row" style={{ marginTop: 5, justifyContent: "space-between" }}>
            <span className="dim mono" style={{ fontSize: 9.5 }}>{rl} {rv == null ? "—" : `${Math.round(rv * 100)}%`} · {a.ops.toLocaleString()} ops{a.errors ? ` · ${a.errors} err` : ""}</span>
            <span className="dim mono" style={{ fontSize: 9.5 }}>{ago(a.last_ts)}</span>
          </div>
          {i < agents.length - 1 && <span className="flow" aria-hidden>›</span>}
        </div>
        );
      })}
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
          <th className="r">Margin</th>{!closed && <th>SL ⟷ TP</th>}<th className="r">{closed ? "Net P&L" : "uP&L"}</th><th>{closed ? "Reason" : "Status"}</th><th />
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
                {!closed && <td><RangeBar p={p} /></td>}
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

function RangeBar({ p }: { p: Position }) {
  const span = p.target - p.stop;
  const at = span ? (p.price - p.stop) / span : 0.5;
  const ent = span ? (p.entry - p.stop) / span : 0.5;
  const clamp = (x: number) => Math.min(1, Math.max(0, x)) * 100;
  return (
    <span className="rangebar" title={`stop ${price(p.stop)} · entry ${price(p.entry)} · now ${price(p.price)} · target ${price(p.target)}`}>
      <i className="ent" style={{ left: `${clamp(ent)}%` }} />
      <i className={`now ${at >= ent ? "up" : "dn"}`} style={{ left: `${clamp(at)}%` }} />
    </span>
  );
}

export function WorkingOrders({ rows, onOpen }: { rows: Snapshot["working"]; onOpen: (id: string) => void }) {
  if (!rows.length) return <div className="empty">No working limit orders.</div>;
  return (
    <div className="scroll">
      <table className="t"><thead><tr><th>Pair</th><th>Mkt</th><th>Dir</th><th className="r">Limit</th><th className="r">Signal entry</th>
        <th className="r">Target qty</th><th className="r">Age</th><th className="r">Reprices</th><th>Client ID</th></tr></thead>
        <tbody>{rows.map((w) => (
          <tr key={w.id} className="click" onClick={() => onOpen(w.id)}>
            <td><b>{w.symbol}</b></td><td><Mkt m={w.market} /></td><td><Dir d={w.direction} /></td>
            <td className="r cyan">{price(Number(w.execution.price))}</td><td className="r">{price(w.entry)}</td>
            <td className="r">{w.execution.target_qty}</td>
            <td className="r">{Math.max(0, Math.round(Date.now() / 1000 - w.execution.first_placed_at))}s</td>
            <td className="r">{w.execution.reprices}</td><td className="muted">{w.execution.working_cid}</td>
          </tr>))}</tbody></table>
    </div>
  );
}

const FEED_AGENTS = ["scanner", "rugcheck", "sizer", "risk", "executor", "logger", "equity"];

export function ActivityPanel({ events, onOpen }: { events: AgentEvent[]; onOpen: (id: string) => void }) {
  const [only, setOnly] = useState<string | null>(null);
  const [hideScan, setHideScan] = useState(true);
  const [paused, setPaused] = useState<AgentEvent[] | null>(null);
  const src = paused ?? events;
  const list = src.filter((e) => (!only || e.agent === only) && (!hideScan || (e.event !== "scan_cycle" && e.event !== "equity_updated")));
  return (
    <div className="col" style={{ gap: 0, flex: 1, minHeight: 0 }}>
      <div className="chips">
        <button className={!only ? "on" : ""} onClick={() => setOnly(null)}>all</button>
        {FEED_AGENTS.map((a) => (
          <button key={a} className={only === a ? "on" : ""} style={{ color: AGENT_COLORS[a] }} onClick={() => setOnly(only === a ? null : a)}>{a}</button>))}
        <span className="grow" />
        <button className={hideScan ? "on" : ""} onClick={() => setHideScan(!hideScan)} title="hide routine scan-cycle / equity ticks">decisions only</button>
        <button className={paused ? "on warn" : ""} onClick={() => setPaused(paused ? null : events)}>{paused ? "▶ resume" : "❚❚ pause"}</button>
      </div>
      <ActivityLog events={list} onOpen={onOpen} />
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
