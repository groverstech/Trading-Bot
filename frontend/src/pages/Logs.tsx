import { Fragment, useEffect, useState } from "react";
import { api } from "../api";
import { Panel } from "../components/ui";
import { AGENT_COLORS } from "../components/ui";
import { datetime } from "../fmt";
import type { AgentEvent } from "../types";

export function Logs({ openTrade }: { openTrade: (id: string) => void }) {
  const [q, setQ] = useState("");
  const [agent, setAgent] = useState("");
  const [level, setLevel] = useState("");
  const [rows, setRows] = useState<AgentEvent[]>([]);
  const [audit, setAudit] = useState<Record<string, any>[]>([]);
  const [view, setView] = useState<"events" | "audit">("events");
  const [open, setOpen] = useState<number | null>(null);

  const search = () => {
    const p = new URLSearchParams({ limit: "500" });
    if (q) p.set("q", q);
    if (agent) p.set("agent", agent);
    if (level) p.set("level", level);
    api.get<AgentEvent[]>(`/api/events?${p}`).then(setRows).catch(() => {});
  };
  useEffect(search, [agent, level]);
  useEffect(() => { if (view === "audit") api.get<Record<string, any>[]>("/api/audit?limit=500").then(setAudit).catch(() => {}); }, [view]);

  return (
    <>
      <div className="row wrap">
        <div className="seg"><button className={view === "events" ? "active" : ""} onClick={() => setView("events")}>Agent decisions</button>
          <button className={view === "audit" ? "active" : ""} onClick={() => setView("audit")}>Operator & config audit</button></div>
        {view === "events" && <>
          <input className="input" style={{ width: 320 }} placeholder="Search message, trade id, data…" value={q}
            onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && search()} />
          <select className="input" style={{ width: 150 }} value={agent} onChange={(e) => setAgent(e.target.value)}>
            <option value="">All agents</option>
            {["scanner", "rugcheck", "sizer", "risk", "executor", "logger", "equity", "orchestrator"].map((a) => <option key={a}>{a}</option>)}
          </select>
          <select className="input" style={{ width: 130 }} value={level} onChange={(e) => setLevel(e.target.value)}>
            <option value="">All levels</option>{["info", "warning", "error", "critical"].map((l) => <option key={l}>{l}</option>)}
          </select>
          <button className="btn primary" onClick={search}>Search</button>
        </>}
      </div>
      {view === "events" ? (
        <Panel title={`Audit trail · ${rows.length} records (persistent, survives restarts)`} flush>
          <table className="t"><thead><tr><th>Time</th><th>Mode</th><th>Agent</th><th>Event</th><th>Trade</th><th>Message</th></tr></thead>
            <tbody>{rows.map((e) => (<Fragment key={e.id}>
              <tr className="click" onClick={() => setOpen(open === e.id ? null : e.id!)}>
                <td>{datetime(e.ts)}</td><td>{e.mode}</td><td style={{ color: AGENT_COLORS[e.agent] }}>{e.agent}</td><td>{e.event}</td>
                <td>{e.trade_id ? <a className="cyan" onClick={(ev) => { ev.stopPropagation(); openTrade(e.trade_id!); }}>{e.trade_id}</a> : ""}</td>
                <td className={e.level === "error" || e.level === "critical" ? "neg" : e.level === "warning" ? "warn" : ""}
                  style={{ whiteSpace: "normal" }}>{e.message}</td></tr>
              {open === e.id && e.data != null && <tr><td colSpan={6}><pre className="json">{JSON.stringify(e.data, null, 2)}</pre></td></tr>}
            </Fragment>))}</tbody></table>
        </Panel>
      ) : (
        <Panel title="Operator & configuration audit log" flush>
          <table className="t"><thead><tr><th>Time</th><th>Actor</th><th>Action</th><th>Detail</th></tr></thead>
            <tbody>{audit.map((a) => (<tr key={a.id}><td>{datetime(a.ts)}</td><td>{a.actor}</td><td className="cyan">{a.action}</td>
              <td className="muted" style={{ whiteSpace: "normal", maxWidth: 900 }}>{JSON.stringify(a.detail).slice(0, 400)}</td></tr>))}</tbody></table>
        </Panel>
      )}
    </>
  );
}
