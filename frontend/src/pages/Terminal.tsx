import { useEffect, useState } from "react";
import { api } from "../api";
import { EquityChart, PriceChart } from "../components/charts";
import {
  ActivityLog, AgentCards, AllocationPanel, PendingOrders, Pipeline, PositionsTable, PerfStrip, RiskPanel, Watchlist,
} from "../components/panels";
import { Panel } from "../components/ui";
import { ago, clock, num, price } from "../fmt";
import type { AgentEvent, Snapshot } from "../types";

type Tab = "positions" | "pending" | "approval" | "recent" | "history" | "comms" | "incidents";

export function Terminal({ snap, events, openTrade, openAgent, toast }: {
  snap: Snapshot; events: AgentEvent[]; openTrade: (id: string) => void; openAgent: (a: string) => void;
  toast: (m: string, e?: boolean) => void;
}) {
  const [sel, setSel] = useState("spot:BTCUSDT");
  const [tab, setTab] = useState<Tab>("positions");
  const [history, setHistory] = useState<Record<string, any>[]>([]);
  const [eqKey, setEqKey] = useState(0);
  const [m, sym] = sel.split(":") as ["spot" | "futures", string];
  const mode = snap.system.mode;
  const modePositions = snap.positions.filter((p) => p.mode === mode);
  const otherPositions = snap.positions.filter((p) => p.mode !== mode);

  useEffect(() => {
    if (tab !== "history") return;
    const load = () => api.get<Record<string, any>[]>("/api/orders?limit=200").then(setHistory).catch(() => {});
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [tab]);
  // refresh the equity curve when realized state changes
  useEffect(() => { setEqKey((k) => k + 1); }, [snap.portfolio.realized_pnl, snap.performance.active_positions, mode]);
  useEffect(() => { const t = setInterval(() => setEqKey((k) => k + 1), 60000); return () => clearInterval(t); }, []);

  const comms = events.filter((e) => e.event !== "scan_cycle" && e.event !== "equity_updated");
  const tabs: [Tab, string, number?][] = [
    ["positions", "Live trading", modePositions.length], ["pending", "Pending orders", snap.pending_orders.length],
    ["approval", "Awaiting approval", snap.awaiting.length], ["recent", "Recent trades", snap.recent_trades.length],
    ["history", "Execution history"], ["comms", "Agent comms"], ["incidents", "Incidents", snap.incidents.length],
  ];
  const nextIn = snap.system.next_cycle ? Math.max(0, Math.round(snap.system.next_cycle - Date.now() / 1000)) : null;

  return (
    <>
      <PerfStrip s={snap} />
      <AgentCards agents={snap.agents} onOpen={openAgent} />

      <div className="grid-main" style={{ height: 400 }}>
        <Panel title="Watchlist" flush right={<span className="dim mono nowrap" style={{ fontSize: 9.5 }}>
          S {snap.market.spot.age ?? "—"}s · P {snap.market.futures.age ?? "—"}s</span>}>
          <Watchlist rows={snap.watchlist} selected={sel} onSelect={setSel} />
        </Panel>
        <Panel title="Central trading activity · agent pipeline" flush accent="var(--purple)"
          right={<span className="dim mono" style={{ fontSize: 9.5 }}>
            {snap.system.running ? `next scan ${nextIn ?? "—"}s` : "system stopped"} · last {ago(snap.system.last_cycle)}</span>}>
          <Pipeline rows={snap.pipeline} onOpen={openTrade} />
        </Panel>
        <Panel title="Activity log" flush accent="var(--green)" right={<span className="dim mono" style={{ fontSize: 9.5 }}>{events.length} events</span>}>
          <ActivityLog events={events} onOpen={openTrade} />
        </Panel>
      </div>

      <div className="grid-2" style={{ height: 340 }}>
        <Panel title="Price chart" accent="var(--cyan)"><PriceChart market={m} symbol={sym} positions={snap.positions} /></Panel>
        <Panel title={`Equity curve · ${mode}`} accent="var(--yellow)"><EquityChart mode={mode} refreshKey={eqKey} /></Panel>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) 380px", gap: 12 }} className="grid-bottom">
        <section className="panel" style={{ minHeight: 280 }}>
          <div className="tabs">{tabs.map(([k, l, n]) => (
            <button key={k} className={tab === k ? "active" : ""} onClick={() => setTab(k)}>{l}{n !== undefined && <span className="n">{n}</span>}</button>))}</div>
          <div style={{ flex: 1, minHeight: 0, overflow: "auto" }}>
            {tab === "positions" && <>
              <PositionsTable rows={modePositions} onOpen={openTrade} toast={toast} />
              {otherPositions.length > 0 && <>
                <div className="note" style={{ padding: "8px 12px" }}>Positions from other modes (still monitored and protected):</div>
                <PositionsTable rows={otherPositions} onOpen={openTrade} toast={toast} /></>}
            </>}
            {tab === "pending" && <PendingOrders rows={snap.pending_orders} />}
            {tab === "approval" && (snap.awaiting.length ? <Pipeline rows={snap.awaiting} onOpen={openTrade} /> :
              <div className="empty">{snap.system.auto_approve ? "Automated approval is on: risk-approved trades execute automatically." : "Nothing awaiting approval."}</div>)}
            {tab === "recent" && <PositionsTable rows={snap.recent_trades} closed onOpen={openTrade} />}
            {tab === "history" && (history.length ? (
              <table className="t"><thead><tr><th>Time</th><th>Trade</th><th>Pair</th><th>Mkt</th><th>Side</th><th>Purpose</th>
                <th className="r">Qty</th><th className="r">Filled</th><th className="r">Avg price</th><th className="r">Fee</th><th>Status</th></tr></thead>
                <tbody>{history.map((o) => (
                  <tr key={o.client_id} className="click" onClick={() => openTrade(o.trade_id)}><td>{clock(o.created_at)}</td>
                    <td className="muted">{o.trade_id}</td><td>{o.symbol}</td><td>{o.market}</td>
                    <td className={o.side === "BUY" ? "pos" : "neg"}>{o.side}</td><td>{o.purpose}</td><td className="r">{o.qty}</td>
                    <td className="r">{o.filled_qty}</td><td className="r">{price(Number(o.avg_price))}</td><td className="r">{num(Number(o.fee_usdt), 4)}</td>
                    <td className={o.status === "FILLED" ? "pos" : "warn"}>{o.status}</td></tr>))}</tbody></table>
            ) : <div className="empty">No orders yet.</div>)}
            {tab === "comms" && <ActivityLog events={comms} onOpen={openTrade} />}
            {tab === "incidents" && (snap.incidents.length ? (
              <table className="t"><thead><tr><th>#</th><th>Time</th><th>Kind</th><th>Severity</th><th>Detail</th></tr></thead>
                <tbody>{snap.incidents.map((i) => (<tr key={i.id}><td>{i.id}</td><td>{clock(i.ts)}</td><td>{i.kind}</td>
                  <td className="neg">{i.severity}</td><td className="muted" style={{ whiteSpace: "normal" }}>{i.detail}</td></tr>))}</tbody></table>
            ) : <div className="empty">No unresolved incidents.</div>)}
          </div>
        </section>
        <div className="col" style={{ gap: 12 }}>
          <Panel title="Risk monitor" accent="var(--red)"><RiskPanel s={snap} /></Panel>
          <Panel title="Capital allocation" accent="var(--yellow)"><AllocationPanel s={snap} /></Panel>
        </div>
      </div>
    </>
  );
}
