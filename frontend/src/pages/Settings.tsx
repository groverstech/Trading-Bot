import { useEffect, useState } from "react";
import { api } from "../api";
import { Checks, ConfirmModal, Panel } from "../components/ui";
import { datetime, num } from "../fmt";
import type { Snapshot } from "../types";

type Cfg = Record<string, Record<string, any>>;
const ENUMS: Record<string, string[]> = {
  margin_mode: ["ISOLATED", "CROSSED"], emergency_policy: ["keep_protected", "flatten"],
};
const HELP: Record<string, string> = {
  risk_per_trade_pct: "% of the market's allocated equity risked per trade (incl. fees & slippage)",
  max_exposure_pct: "gross notional of open positions, % of that market's equity",
  max_correlated_exposure_pct: "notional of highly correlated positions, % of spot+futures equity",
  max_position_pct: "max single-position notional, % of that market's equity",
  min_reward_risk: "minimum NET reward:risk after fees and slippage",
  emergency_policy: "keep_protected: leave SL/TP active · flatten: close at market only if exchange reachable",
  stale_data_seconds: "block trading when prices are older than this",
  signal_ttl_seconds: "opportunities older than this expire unexecuted",
  margin_mode: "ISOLATED recommended; CROSSED is optional",
};

function ConfigForm({ section, value, onSaved, toast, exclude = [], toPatch }: {
  section: string; value: Record<string, any>; onSaved: () => void; toast: (m: string, e?: boolean) => void; exclude?: string[];
  toPatch?: (all: Record<string, any>, changed: Record<string, any>) => Record<string, any>;
}) {
  const [d, setD] = useState<Record<string, any>>(value);
  useEffect(() => setD(value), [JSON.stringify(value)]);
  const changed = Object.keys(d).filter((k) => JSON.stringify(d[k]) !== JSON.stringify(value[k]));
  const set = (k: string, v: unknown) => setD({ ...d, [k]: v });
  return (
    <>
      <div className="form-grid">
        {Object.entries(d).filter(([k, v]) => !exclude.includes(k) && (typeof v !== "object" || Array.isArray(v))).map(([k, v]) => (
          <div className="field" key={k}>
            <label>{k.replace(/_/g, " ")}</label>
            {typeof v === "boolean" ? (
              <label className="check"><input type="checkbox" checked={v} onChange={(e) => set(k, e.target.checked)} />{v ? "on" : "off"}</label>
            ) : ENUMS[k] ? (
              <select className="input" value={v} onChange={(e) => set(k, e.target.value)}>{ENUMS[k].map((o) => <option key={o}>{o}</option>)}</select>
            ) : Array.isArray(v) ? (
              <input className="input" value={v.join(", ")} onChange={(e) => set(k, e.target.value.split(",").map((x) => x.trim()).filter(Boolean)
                .map((x) => (typeof value[k]?.[0] === "number" ? Number(x) : x)))} />
            ) : (
              <input className="input" type={typeof v === "number" ? "number" : "text"} step="any" value={v}
                onChange={(e) => set(k, typeof v === "number" ? Number(e.target.value) : e.target.value)} />
            )}
            {HELP[k] && <small>{HELP[k]}</small>}
          </div>
        ))}
      </div>
      <div className="row" style={{ marginTop: 12 }}>
        <span className="note">{changed.length ? `Changed: ${changed.join(", ")}` : "No changes"} · every change is validated and audited</span>
        <span className="grow" />
        <button className="btn" disabled={!changed.length} onClick={() => setD(value)}>Reset</button>
        <button className="btn primary" disabled={!changed.length} onClick={async () => {
          const ch = Object.fromEntries(changed.map((k) => [k, d[k]]));
          try { await api.put(`/api/config/${section}`, toPatch ? toPatch(d, ch) : ch); toast(`${section} saved`); onSaved(); }
          catch (e) { toast((e as Error).message, true); }
        }}>Save {section}</button>
      </div>
    </>
  );
}

type Cred = { id: string; venue: string; env: string; key_hint: string; created_at: number; verified_at: number | null; verification: any };

export function Settings({ snap, toast }: { snap: Snapshot; toast: (m: string, e?: boolean) => void }) {
  const [cfg, setCfg] = useState<Cfg | null>(null);
  const [creds, setCreds] = useState<Cred[]>([]);
  const [form, setForm] = useState({ venue: "spot", env: "testnet", api_key: "", api_secret: "" });
  const [pwModal, setPwModal] = useState(false);
  const [sessions, setSessions] = useState<any[]>([]);
  const [pw, setPw] = useState({ current: "", next: "" });
  const [balances, setBalances] = useState<any>(null);
  const load = () => {
    api.get<Cfg>("/api/config").then(setCfg);
    api.get<Cred[]>("/api/credentials").then(setCreds);
    api.get<any[]>("/api/auth/sessions").then(setSessions);
  };
  useEffect(load, []);
  if (!cfg) return <div className="empty">Loading configuration…</div>;
  const health = snap.market.health;
  const sys = snap.system;

  return (
    <>
      <div className="grid-2">
        <Panel title="Exchange · Binance connections" accent="var(--yellow)">
          <table className="t"><thead><tr><th>Connection</th><th>Endpoint</th><th>Status</th><th className="r">Latency</th><th className="r">Weight</th></tr></thead>
            <tbody>
              {Object.entries(health).map(([k, h]) => (
                <tr key={k}><td>{k}</td><td className="muted">{k === "spot_data" ? snap.market.spot.source : k === "futures_data" ? snap.market.futures.source : ""}</td>
                  <td className={h.connected ? "pos" : "neg"}>{h.connected ? "● connected" : `○ ${h.last_error_msg || "down"}`}</td>
                  <td className="r">{num(h.latency_ms, 0)} ms</td><td className="r">{h.used_weight}/{h.weight_limit}</td></tr>))}
            </tbody></table>
          {snap.market.futures.testnet_data && <div className="banner orange" style={{ margin: "10px 0 0" }}>
            Futures market data is coming from the Binance FUTURES TESTNET (GROK_FUTURES_DATA_URL). Testnet prices, spreads and depth differ
            from production. Use fapi.binance.com where your location permits.</div>}
          <div className="note" style={{ marginTop: 10 }}>API keys need <b>Enable Reading</b> plus <b>Spot & Margin Trading</b> and/or <b>Futures</b>.
            Never enable withdrawals: live activation verifies withdrawals are disabled. IP-whitelisting the key is strongly recommended.</div>
        </Panel>
        <Panel title="API credentials (encrypted at rest, write-only)" accent="var(--yellow)">
          <table className="t"><thead><tr><th>Venue</th><th>Env</th><th>Key</th><th>Verified</th><th /></tr></thead>
            <tbody>{creds.map((c) => (
              <tr key={c.id}><td>{c.venue}</td><td className={c.env === "live" ? "neg" : "warn"}>{c.env}</td><td>{c.key_hint}</td>
                <td className={c.verification?.ok ? "pos" : c.verification ? "neg" : "muted"}>{c.verified_at ? datetime(c.verified_at) : c.verification ? "failed" : "not verified"}</td>
                <td className="right"><button className="btn sm primary" onClick={async () => {
                  try { const r: any = await api.post(`/api/credentials/${c.venue}/${c.env}/verify`); toast(r.ok ? "Verified" : "Verification failed", !r.ok); load(); }
                  catch (e) { toast((e as Error).message, true); }
                }}>Verify</button>{" "}
                  <button className="btn sm danger" onClick={async () => { if (!confirm(`Delete ${c.id} credentials?`)) return; await api.del(`/api/credentials/${c.venue}/${c.env}`); load(); }}>Delete</button></td></tr>))}
              {!creds.length && <tr><td colSpan={5} className="empty">No credentials stored. Paper mode needs none.</td></tr>}</tbody></table>
          {creds.filter((c) => c.verification).map((c) => (
            <details key={c.id + "v"} style={{ marginTop: 8 }}><summary className="mono muted" style={{ cursor: "pointer" }}>{c.id} verification</summary>
              <Checks checks={c.verification.checks ?? []} /></details>))}
          <div className="form-grid" style={{ marginTop: 12 }}>
            <div className="field"><label>Venue</label><select className="input" value={form.venue} onChange={(e) => setForm({ ...form, venue: e.target.value })}>
              <option value="spot">Spot</option><option value="futures">USDⓈ-M Futures</option></select></div>
            <div className="field"><label>Environment</label><select className="input" value={form.env} onChange={(e) => setForm({ ...form, env: e.target.value })}>
              <option value="testnet">Testnet</option><option value="live">Live (real funds)</option></select></div>
            <div className="field"><label>API key</label><input className="input" autoComplete="off" value={form.api_key} onChange={(e) => setForm({ ...form, api_key: e.target.value })} /></div>
            <div className="field"><label>API secret</label><input className="input" type="password" autoComplete="new-password" value={form.api_secret} onChange={(e) => setForm({ ...form, api_secret: e.target.value })} /></div>
          </div>
          <div className="row" style={{ marginTop: 10 }}><span className="grow" />
            <button className="btn primary" disabled={!form.api_key || !form.api_secret} onClick={() => setPwModal(true)}>Store encrypted</button>
            <button className="btn" onClick={async () => { try { setBalances(await api.get(`/api/exchange/balances?env=testnet`)); } catch (e) { toast((e as Error).message, true); } }}>Testnet balances</button></div>
          {balances && <pre className="json" style={{ marginTop: 8 }}>{JSON.stringify(balances, null, 2)}</pre>}
        </Panel>
      </div>

      <div className="grid-2">
        <Panel title="Automation & operating mode" accent="var(--cyan)">
          <div className="row wrap" style={{ marginBottom: 12 }}>
            <span className="muted mono">Mode:</span>
            {(["paper", "testnet"] as const).map((m) => (
              <button key={m} className={`btn ${sys.mode === m ? "primary" : ""}`} disabled={sys.running || sys.mode === m} onClick={async () => {
                try { await api.post("/api/system/mode", { mode: m }); toast(`Mode: ${m}`); } catch (e) { toast((e as Error).message, true); }
              }}>{m}</button>))}
            <span className="note">LIVE can only be entered via the Live activation workflow. Stop the system to change mode.</span>
          </div>
          <ConfigForm section="automation" value={{ auto_approve: cfg.automation.auto_approve, ...Object.fromEntries(
            Object.entries(cfg.automation.enabled_agents).map(([k, v]) => [`agent_${k}`, v])) }}
            onSaved={load} toast={toast} toPatch={(all) => ({ auto_approve: all.auto_approve, enabled_agents: Object.fromEntries(
              Object.entries(all).filter(([k]) => k.startsWith("agent_")).map(([k, v]) => [k.slice(6), v])) })} />
          <div className="note" style={{ marginTop: 8 }}>agent_scanner / agent_executor disable automatic scanning / execution. With auto approve off, risk-approved trades wait in “Awaiting approval”.</div>
          <h4 className="mono muted" style={{ margin: "14px 0 8px", fontSize: 10, letterSpacing: ".14em" }}>TRADING SCHEDULE (UTC)</h4>
          <ScheduleForm value={cfg.automation.schedule} onSaved={load} toast={toast} />
        </Panel>
        <Panel title="Capital allocation" accent="var(--yellow)">
          <ConfigForm section="capital" value={cfg.capital} onSaved={load} toast={toast} />
          <div className="row" style={{ marginTop: 10 }}>
            <span className="note">Starting capital is fixed once a mode is initialised (converted at the then-current USD/INR rate).
              After changing percentages, reallocate to move free cash between accounts. Margin and holdings are never moved.</span>
            <button className="btn" onClick={async () => { try { const r: any = await api.post("/api/capital/reallocate"); toast(`Reallocated (${r.moves.length} transfers)`); } catch (e) { toast((e as Error).message, true); } }}>Reallocate</button>
          </div>
        </Panel>
      </div>

      <Panel title="Risk management" accent="var(--red)">
        <ConfigForm section="risk" value={cfg.risk} onSaved={load} toast={toast} />
      </Panel>
      <div className="grid-2">
        <Panel title="Rug check thresholds" accent="var(--orange)"><ConfigForm section="rugcheck" value={cfg.rugcheck} onSaved={load} toast={toast} /></Panel>
        <Panel title="Fees & paper simulation" accent="var(--purple)"><ConfigForm section="fees" value={cfg.fees} onSaved={load} toast={toast} />
          <div className="note" style={{ marginTop: 8 }}>Defaults are Binance base-tier taker/maker rates. Exchange modes record the actual commissions reported by Binance.</div></Panel>
      </div>
      <Panel title="Scanner: pairs, timeframes, frequency" accent="var(--cyan)">
        <ConfigForm section="scanner" value={cfg.scanner} onSaved={load} toast={toast} />
        <div className="note" style={{ marginTop: 8 }}>Timeframes: 1m, 5m, 15m, 1h, 4h, 1d. Entry timeframes generate signals; confirm timeframes adjust scores by trend alignment.
          Only USDT-quoted pairs are supported.</div>
      </Panel>

      <div className="grid-2">
        <Panel title="Security · password" accent="var(--purple)">
          <div className="form-grid">
            <div className="field"><label>Current password</label><input className="input" type="password" value={pw.current} onChange={(e) => setPw({ ...pw, current: e.target.value })} /></div>
            <div className="field"><label>New password (min 10)</label><input className="input" type="password" value={pw.next} onChange={(e) => setPw({ ...pw, next: e.target.value })} /></div>
          </div>
          <div className="row" style={{ marginTop: 10 }}><span className="grow" />
            <button className="btn primary" disabled={!pw.current || pw.next.length < 10} onClick={async () => {
              try { await api.post("/api/auth/password", { current: pw.current, new: pw.next }); setPw({ current: "", next: "" }); toast("Password changed; other sessions signed out"); load(); }
              catch (e) { toast((e as Error).message, true); }
            }}>Change password</button></div>
        </Panel>
        <Panel title="Security · active sessions" accent="var(--purple)" flush>
          <table className="t"><thead><tr><th>ID</th><th>Created</th><th>Last seen</th><th>Expires</th></tr></thead>
            <tbody>{sessions.map((s) => (<tr key={s.id}><td>{s.id}…</td><td>{datetime(s.created_at)}</td><td>{datetime(s.last_seen)}</td><td>{datetime(s.expires_at)}</td></tr>))}</tbody></table>
          <div className="row" style={{ padding: 10 }}><span className="note grow">Secrets are encrypted with a master key (GROK_MASTER_KEY or data/master.key) and never sent to the browser or written to logs.</span>
            <button className="btn danger" onClick={async () => { await api.post("/api/auth/logout"); location.reload(); }}>Sign out</button></div>
        </Panel>
      </div>

      {pwModal && (
        <ConfirmModal needPassword title={`STORE ${form.env.toUpperCase()} ${form.venue.toUpperCase()} API KEY`}
          danger={form.env === "live"} confirmLabel="Encrypt & store"
          body={<>The key is encrypted with the master key and is never shown again. {form.env === "live" && <b className="neg">Live keys can trade real funds.</b>}</>}
          onConfirm={async ({ password }) => {
            await api.post("/api/credentials", { ...form, password });
            setForm({ ...form, api_key: "", api_secret: "" }); toast("Credentials stored. Run Verify."); load();
          }} onClose={() => setPwModal(false)} />
      )}
    </>
  );
}

function ScheduleForm({ value, onSaved, toast }: { value: any; onSaved: () => void; toast: (m: string, e?: boolean) => void }) {
  const [d, setD] = useState(value);
  return (
    <div className="row wrap" style={{ gap: 12 }}>
      <label className="check"><input type="checkbox" checked={d.enabled} onChange={(e) => setD({ ...d, enabled: e.target.checked })} />enabled</label>
      <div className="field"><label>Days (0=Mon)</label><input className="input" style={{ width: 150 }} value={d.days.join(",")}
        onChange={(e) => setD({ ...d, days: e.target.value.split(",").map((x: string) => Number(x.trim())).filter((x: number) => !Number.isNaN(x)) })} /></div>
      <div className="field"><label>Start</label><input className="input" style={{ width: 90 }} value={d.start_utc} onChange={(e) => setD({ ...d, start_utc: e.target.value })} /></div>
      <div className="field"><label>End</label><input className="input" style={{ width: 90 }} value={d.end_utc} onChange={(e) => setD({ ...d, end_utc: e.target.value })} /></div>
      <button className="btn primary" onClick={async () => {
        try { await api.put("/api/config/automation", { schedule: d }); toast("Schedule saved"); onSaved(); } catch (e) { toast((e as Error).message, true); }
      }}>Save schedule</button>
    </div>
  );
}
