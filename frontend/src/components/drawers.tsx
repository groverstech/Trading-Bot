import { useEffect, useState } from "react";
import { api } from "../api";
import { clock, datetime, num, pnlClass, price, signed } from "../fmt";
import type { AgentCard, AgentEvent } from "../types";
import { ActivityLog } from "./panels";
import { AGENT_COLORS, Checks, ConfirmModal, Dir, Drawer, Mkt, Panel } from "./ui";

type Trail = {
  trade: Record<string, any>;
  events: AgentEvent[];
  orders: Record<string, any>[];
  fills: Record<string, any>[];
  position: Record<string, any> | null;
  ledger: Record<string, any>[];
};

export function TradeDrawer({ id, onClose, toast }: { id: string; onClose: () => void; toast: (m: string, e?: boolean) => void }) {
  const [t, setT] = useState<Trail | null>(null);
  const [err, setErr] = useState("");
  const [confirmLive, setConfirmLive] = useState(false);
  const load = () => api.get<Trail>(`/api/trades/${id}`).then(setT).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, [id]);
  const tr = t?.trade;
  const sig = tr?.signal ?? {};
  const act = async (path: string, msg: string) => {
    try { const r: any = await api.post(path); toast(r?.ok === false ? `Refused: ${r.reason}` : msg, r?.ok === false); load(); }
    catch (e) { toast((e as Error).message, true); }
  };
  return (
    <Drawer title={<>TRADE {id}</>} onClose={onClose}>
      {err && <div className="err">{err}</div>}
      {tr && (
        <>
          <div className="row wrap" style={{ gap: 10 }}>
            <b className="mono" style={{ fontSize: 16 }}>{tr.symbol}</b><Mkt m={tr.market} /><Dir d={tr.direction} />
            <span className="tag">{tr.strategy} · {tr.timeframe}</span><span className="tag">{tr.mode}</span>
            <span className={`tag ${tr.stage === "REJECTED" || tr.stage === "FAILED" ? "short" : tr.stage === "OPEN" || tr.stage === "CLOSED" ? "long" : ""}`}>{tr.stage}</span>
            <span className="grow" />
            {tr.stage === "AWAITING_APPROVAL" && <>
              <button className="btn go" onClick={() => act(`/api/trades/${id}/approve`, "Approved: re-validated by risk and sent to executor")}>Approve</button>
              <button className="btn danger" onClick={() => act(`/api/trades/${id}/reject`, "Rejected")}>Reject</button></>}
            {tr.stage === "AWAITING_LIVE_CONFIRM" && <>
              <button className="btn danger" onClick={() => setConfirmLive(true)}>Confirm first real-money order</button>
              <button className="btn" onClick={() => act(`/api/trades/${id}/reject`, "Rejected")}>Reject</button></>}
          </div>
          {tr.reject_reason && <div className="banner red" style={{ margin: 0 }}>{tr.reject_reason}</div>}
          <div className="grid-2">
            <Panel title="1 · Scanner signal" accent={AGENT_COLORS.scanner}>
              <div className="kv"><span>Entry</span><span>{price(sig.entry)}</span><span>Stop-loss</span><span className="neg">{price(sig.stop)}</span>
                <span>Take-profit</span><span className="pos">{price(sig.target)}</span><span>Gross R:R</span><span>{num(sig.reward_risk)}</span>
                <span>Score</span><span>{num(sig.score, 3)}</span><span>Detected</span><span>{datetime(tr.created_at)}</span></div>
              <ul className="note" style={{ margin: "8px 0 0", paddingLeft: 16 }}>{(sig.reasons ?? []).map((r: string) => <li key={r}>{r}</li>)}</ul>
            </Panel>
            <Panel title="3 · Sizer" accent={AGENT_COLORS.sizer}>
              {tr.sizing ? (tr.sizing.approved ? (
                <div className="kv"><span>Quantity</span><span>{tr.sizing.qty}</span><span>Notional</span><span>{num(tr.sizing.notional)} USDT</span>
                  <span>Risk</span><span>{num(tr.sizing.risk_usdt, 3)} USDT ({num(tr.sizing.risk_pct, 3)}%)</span>
                  <span>Leverage / margin</span><span>{tr.sizing.leverage}x / {num(tr.sizing.margin)}</span>
                  <span>Liquidation</span><span>{price(tr.sizing.liquidation_price)}</span><span>Net R:R</span><span>{num(tr.sizing.rr_net)}</span>
                  <span>Vol factor</span><span>{num(tr.sizing.calc?.vol_factor, 3)}</span><span>Corr factor</span><span>{num(tr.sizing.calc?.corr_factor, 2)}</span></div>
              ) : <div className="neg mono">{tr.sizing.reason}</div>) : <div className="dim">Not reached</div>}
            </Panel>
          </div>
          <Panel title="2 · Rug Check" accent={AGENT_COLORS.rugcheck}>
            {tr.rugcheck ? <Checks checks={tr.rugcheck.checks} /> : <div className="dim">Not reached</div>}
          </Panel>
          <Panel title="Central risk validation" accent={AGENT_COLORS.risk}>
            {tr.risk ? <Checks checks={tr.risk.checks} /> : <div className="dim">Not reached</div>}
          </Panel>
          <Panel title="4 · Executor: orders & fills" accent={AGENT_COLORS.executor} flush>
            {t.orders.length ? (
              <table className="t"><thead><tr><th>Client ID</th><th>Purpose</th><th>Side</th><th className="r">Qty</th><th className="r">Filled</th><th className="r">Avg</th><th className="r">Fee</th><th>Status</th></tr></thead>
                <tbody>{t.orders.map((o) => (
                  <tr key={o.client_id}><td className="muted">{o.client_id}</td><td>{o.purpose}</td><td>{o.side}</td><td className="r">{o.qty}</td>
                    <td className="r">{o.filled_qty}</td><td className="r">{price(Number(o.avg_price))}</td><td className="r">{num(Number(o.fee_usdt), 4)}</td>
                    <td className={o.status === "FILLED" ? "pos" : "warn"}>{o.status}{o.error ? ` · ${o.error}` : ""}</td></tr>))}</tbody></table>
            ) : <div className="empty">No orders submitted.</div>}
            {t.fills.length > 0 && <div className="note" style={{ padding: "6px 12px" }}>
              Slippage: {t.fills.map((f) => `${num(Number(f.slippage_usdt), 4)} USDT @ ${price(Number(f.price))} (ref ${price(Number(f.ref_price))})`).join(" · ")}</div>}
          </Panel>
          {t.position && (
            <Panel title="6 · Equity: position & ledger" accent={AGENT_COLORS.equity} flush>
              <div className="kv" style={{ padding: "8px 12px" }}>
                <span>Status</span><span>{t.position.status}{t.position.exit_reason ? ` · ${t.position.exit_reason}` : ""}</span>
                <span>Entry → exit</span><span>{price(Number(t.position.entry_price))} → {price(Number(t.position.exit_price))}</span>
                <span>Gross P&L</span><span className={pnlClass(Number(t.position.gross_pnl))}>{signed(Number(t.position.gross_pnl), 4)}</span>
                <span>Fees / funding</span><span>{num(Number(t.position.fees), 4)} / {signed(Number(t.position.funding), 4)}</span>
                <span>Realized (net)</span><span className={pnlClass(Number(t.position.realized_pnl))}>{signed(Number(t.position.realized_pnl), 4)}</span>
              </div>
              <table className="t"><thead><tr><th>Time</th><th>Account</th><th>Kind</th><th className="r">Amount</th><th>Note</th></tr></thead>
                <tbody>{t.ledger.map((l) => (<tr key={l.id}><td>{clock(l.ts)}</td><td>{l.account}</td><td>{l.kind}</td>
                  <td className={`r ${pnlClass(Number(l.amount))}`}>{signed(Number(l.amount), 4)}</td><td className="muted">{l.note}</td></tr>))}</tbody></table>
            </Panel>
          )}
          <Panel title="5 · Logger: full audit trail" accent={AGENT_COLORS.logger} flush>
            <ActivityLog events={[...t.events].reverse()} />
          </Panel>
        </>
      )}
      {confirmLive && (
        <ConfirmModal danger needPassword title="CONFIRM FIRST REAL-MONEY ORDER" confirmLabel="Place real order"
          body={<>This is the first order in LIVE mode and uses <b>real funds</b> on Binance. Risk is re-validated before submission.</>}
          onConfirm={async ({ password }) => {
            const r: any = await api.post(`/api/trades/${id}/confirm-live`, { password });
            toast(r?.ok === false ? `Refused: ${r.reason}` : "First live order confirmed", r?.ok === false); load();
          }} onClose={() => setConfirmLive(false)} />
      )}
    </Drawer>
  );
}

export function AgentDrawer({ agent, card, onClose, onOpenTrade }: { agent: string; card?: AgentCard; onClose: () => void; onOpenTrade: (id: string) => void }) {
  const [events, setEvents] = useState<AgentEvent[]>([]);
  useEffect(() => {
    const q = agent === "risk" ? "risk" : agent;
    const load = () => api.get<AgentEvent[]>(`/api/events?agent=${q}&limit=300`).then(setEvents).catch(() => {});
    load();
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, [agent]);
  const color = AGENT_COLORS[agent];
  return (
    <Drawer title={card?.title ?? agent.toUpperCase()} accent={color} onClose={onClose}>
      {card && (
        <div className="grid-2">
          <Panel title="Status" accent={color}>
            <div className="kv"><span>Status</span><span className={`status ${card.status}`}><span className="dot" />{card.status}</span>
              <span>Activity</span><span>{card.activity}</span><span>Last decision</span><span>{card.last_decision}</span>
              <span>Operations</span><span>{card.ops.toLocaleString()}</span><span>Errors</span><span className={card.errors ? "neg" : ""}>{card.errors}</span>
              <span>Last activity</span><span>{datetime(card.last_ts)}</span>
              {card.last_error && <><span>Last error</span><span className="neg">{card.last_error}</span></>}</div>
          </Panel>
          <Panel title="Metrics" accent={color}><pre className="json">{JSON.stringify(card.metrics, null, 2)}</pre></Panel>
        </div>
      )}
      <Panel title={`Activity log (${events.length})`} accent={color} flush style={{ minHeight: 300 }}>
        <ActivityLog events={events} onOpen={onOpenTrade} />
      </Panel>
    </Drawer>
  );
}
