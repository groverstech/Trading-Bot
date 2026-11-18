import { useEffect, useState } from "react";
import { api } from "../api";
import { Checks, ConfirmModal, Panel } from "../components/ui";
import { datetime } from "../fmt";
import type { Snapshot } from "../types";

type LiveStatus = {
  checks: { name: string; ok: boolean; detail: string }[];
  auth: { status: string; first_order_confirmed?: boolean; authorized_at?: number; activated_at?: number };
  mode: string; activation_phrase: string;
  selftest: { passed: boolean; ts: number; summary: string; duration_s: number; output_tail: string } | null;
  jurisdiction: { ack: boolean; statement: string; ts: number } | null;
  log: { id: number; ts: number; event: string; detail: string }[];
};

const JURISDICTION_TEXT =
  "I have independently verified that (1) my Binance account is eligible to use Binance Spot and, if enabled, " +
  "USDⓈ-M Futures from my jurisdiction (India), including KYC status and product availability for Indian residents; " +
  "(2) I understand applicable Indian rules for virtual digital assets, including income-tax treatment (e.g. Section 115BBH), " +
  "TDS under Section 194S, FIU-IND registration requirements for exchanges, and that derivatives treatment may differ; " +
  "(3) this software provides no regulatory, legal or tax assurance and I accept full responsibility for live trading.";

export function Live({ snap, toast }: { snap: Snapshot; toast: (m: string, e?: boolean) => void }) {
  const [st, setSt] = useState<LiveStatus | null>(null);
  const [modal, setModal] = useState<null | "risk" | "ack" | "authorize" | "activate" | "revoke">(null);
  const [running, setRunning] = useState(false);
  const load = () => api.get<LiveStatus>("/api/live/status").then(setSt).catch(() => {});
  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t); }, []);
  if (!st) return <div className="empty">Loading…</div>;
  const allOk = st.checks.every((c) => c.ok);
  const status = st.auth.status;

  return (
    <>
      <div className={`banner ${status === "active" ? "red" : "orange"}`} style={{ margin: 0 }}>
        {status === "active"
          ? <>● LIVE TRADING ACTIVE. Real funds at risk. {st.auth.first_order_confirmed ? "First order confirmed." : "The first real order will be held for your confirmation."}</>
          : <>Live trading is <b>&nbsp;DISABLED&nbsp;</b>. It stays disabled after any restart, redeployment or credential change until this workflow is completed again.</>}
      </div>
      <div className="grid-2">
        <Panel title="1 · Preconditions" accent={allOk ? "var(--green)" : "var(--red)"}>
          <Checks checks={st.checks} />
          <div className="row wrap" style={{ marginTop: 12 }}>
            <button className="btn" onClick={() => setModal("risk")}>Confirm risk configuration</button>
            <button className="btn" onClick={() => setModal("ack")}>Eligibility / regulatory / tax acknowledgement</button>
            <button className="btn primary" disabled={running} onClick={async () => {
              setRunning(true);
              try { const r: any = await api.post("/api/live/selftest"); toast(r.passed ? `Safety tests passed: ${r.summary}` : `Safety tests FAILED: ${r.summary}`, !r.passed); load(); }
              catch (e) { toast((e as Error).message, true); } finally { setRunning(false); }
            }}>{running ? "Running safety tests…" : "Run safety self-test"}</button>
          </div>
          {st.selftest && <details style={{ marginTop: 8 }}><summary className={`mono ${st.selftest.passed ? "pos" : "neg"}`} style={{ cursor: "pointer" }}>
            Last self-test {datetime(st.selftest.ts)}: {st.selftest.summary}</summary><pre className="json">{st.selftest.output_tail}</pre></details>}
        </Panel>
        <Panel title="2 · Authorization & activation" accent="var(--red)">
          <ol className="note" style={{ margin: 0, paddingLeft: 18 }}>
            <li><b>Authorize</b>: requires every precondition and your password. Bound to this process and the current API keys.</li>
            <li><b>Activate</b>: separate confirmation phrase + password; the system must be stopped. Switches the mode to LIVE.</li>
            <li><b>First order</b>: the first real-money order is held in “Awaiting approval” until you confirm it with your password.</li>
          </ol>
          <div className="kv" style={{ marginTop: 12 }}>
            <span>Authorization status</span><span className={status === "active" ? "neg" : status === "authorized" ? "warn" : "muted"}>{status.toUpperCase()}</span>
            <span>Current mode</span><span>{st.mode.toUpperCase()}</span>
            <span>Deployment flag</span><span className={snap.system.allow_live_deployment ? "pos" : "neg"}>{snap.system.allow_live_deployment ? "GROK_ALLOW_LIVE=1" : "not set"}</span>
            <span>Authorized at</span><span>{datetime(st.auth.authorized_at)}</span>
            <span>Activated at</span><span>{datetime(st.auth.activated_at)}</span>
          </div>
          <div className="row wrap" style={{ marginTop: 14 }}>
            <button className="btn danger" disabled={!allOk || status !== "inactive"} onClick={() => setModal("authorize")}>Authorize live trading</button>
            <button className="btn danger" disabled={status !== "authorized" || snap.system.running} onClick={() => setModal("activate")}>Activate LIVE mode</button>
            <button className="btn" disabled={status === "inactive" && st.mode !== "live"} onClick={() => setModal("revoke")}>Revoke / disable live</button>
          </div>
        </Panel>
      </div>
      <Panel title="Live authorization audit log" flush>
        <table className="t"><thead><tr><th>Time</th><th>Event</th><th>Detail</th></tr></thead>
          <tbody>{st.log.map((l) => (<tr key={l.id}><td>{datetime(l.ts)}</td><td className="cyan">{l.event}</td>
            <td className="muted" style={{ whiteSpace: "normal" }}>{l.detail.slice(0, 300)}</td></tr>))}
            {!st.log.length && <tr><td colSpan={3} className="empty">No live authorization events.</td></tr>}</tbody></table>
      </Panel>

      {modal === "risk" && <ConfirmModal needPassword title="CONFIRM RISK CONFIGURATION"
        body={<pre className="json">{JSON.stringify(snap.risk.config, null, 2)}</pre>}
        onConfirm={async ({ password }) => { await api.post("/api/live/confirm-risk", { password }); toast("Risk configuration confirmed"); load(); }}
        onClose={() => setModal(null)} />}
      {modal === "ack" && <ConfirmModal needPassword title="ELIGIBILITY, REGULATORY & TAX ACKNOWLEDGEMENT"
        body={<>{JURISDICTION_TEXT}</>} confirmLabel="I acknowledge"
        onConfirm={async ({ password }) => { await api.post("/api/live/ack-jurisdiction", { password, ack: true, statement: JURISDICTION_TEXT }); toast("Acknowledgement recorded"); load(); }}
        onClose={() => setModal(null)} />}
      {modal === "authorize" && <ConfirmModal danger needPassword title="AUTHORIZE LIVE TRADING"
        body={<>All preconditions are met. Authorizing allows LIVE mode to be activated in this running process.</>}
        onConfirm={async ({ password }) => { await api.post("/api/live/authorize", { password }); toast("Live trading authorized"); load(); }}
        onClose={() => setModal(null)} />}
      {modal === "activate" && <ConfirmModal danger needPassword phrase={st.activation_phrase} title="ACTIVATE LIVE MODE · REAL MONEY"
        body={<>The terminal will trade <b>real funds</b> on Binance within your risk limits once you start the system.</>}
        onConfirm={async ({ password, phrase }) => { await api.post("/api/live/activate", { password, phrase }); toast("LIVE MODE ACTIVE", true); load(); }}
        onClose={() => setModal(null)} />}
      {modal === "revoke" && <ConfirmModal title="REVOKE LIVE AUTHORIZATION"
        body={<>Stops the system (if live), revokes authorization and returns to PAPER mode. Open live positions keep their exchange-side protection.</>}
        onConfirm={async () => { await api.post("/api/live/revoke"); toast("Live authorization revoked"); load(); }}
        onClose={() => setModal(null)} />}
    </>
  );
}
