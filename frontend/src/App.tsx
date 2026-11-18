import { useCallback, useEffect, useState } from "react";
import { api, useLive } from "./api";
import { AgentDrawer, TradeDrawer } from "./components/drawers";
import { Header, type Page } from "./components/Header";
import { useToast } from "./components/ui";
import { Analytics } from "./pages/Analytics";
import { Live } from "./pages/Live";
import { Logs } from "./pages/Logs";
import { Settings } from "./pages/Settings";
import { Strategies } from "./pages/Strategies";
import { Terminal } from "./pages/Terminal";

function Login({ configured, onDone }: { configured: boolean; onDone: () => void }) {
  const [pw, setPw] = useState("");
  const [pw2, setPw2] = useState("");
  const [err, setErr] = useState("");
  const submit = async () => {
    setErr("");
    try {
      if (!configured) {
        if (pw !== pw2) throw new Error("passwords do not match");
        await api.post("/api/auth/setup", { password: pw });
      } else await api.post("/api/auth/login", { password: pw });
      onDone();
    } catch (e) { setErr((e as Error).message); }
  };
  return (
    <div className="login">
      <div className="login-box">
        <div className="brand"><div className="brand-mark">G</div><div><h1>GROK BOT TERMINAL</h1><small>OPERATOR ACCESS</small></div></div>
        <div className="note">{configured ? "Enter the operator password." : "First run: create the operator password (min. 10 characters)."}</div>
        <div className="field"><label>Password</label>
          <input className="input" type="password" autoFocus value={pw} onChange={(e) => setPw(e.target.value)} onKeyDown={(e) => e.key === "Enter" && configured && submit()} /></div>
        {!configured && <div className="field"><label>Confirm password</label>
          <input className="input" type="password" value={pw2} onChange={(e) => setPw2(e.target.value)} onKeyDown={(e) => e.key === "Enter" && submit()} /></div>}
        {err && <div className="err">{err}</div>}
        <button className="btn primary" onClick={submit} disabled={!pw}>{configured ? "Sign in" : "Create & sign in"}</button>
      </div>
    </div>
  );
}

export default function App() {
  const [auth, setAuth] = useState<{ configured: boolean; authenticated: boolean } | null>(null);
  const [page, setPage] = useState<Page>(() => (location.hash.slice(1) as Page) || "terminal");
  const [trade, setTrade] = useState<string | null>(null);
  const [agent, setAgent] = useState<string | null>(null);
  const { toast, node } = useToast();
  const checkAuth = useCallback(() => api.get<{ configured: boolean; authenticated: boolean }>("/api/auth/status").then(setAuth).catch(() => {}), []);
  useEffect(() => { checkAuth(); }, [checkAuth]);
  useEffect(() => {
    const h = () => setAuth((a) => (a ? { ...a, authenticated: false } : a));
    window.addEventListener("grok-unauthorized", h);
    return () => window.removeEventListener("grok-unauthorized", h);
  }, []);
  useEffect(() => { location.hash = page; }, [page]);
  const { snap, events, connected } = useLive(!!auth?.authenticated);

  if (!auth) return <div className="empty">Connecting…</div>;
  if (!auth.authenticated) return <Login configured={auth.configured} onDone={checkAuth} />;
  const sys = snap?.system;

  return (
    <div className="app">
      <Header snap={snap} page={page} setPage={setPage} connected={connected} toast={toast} />
      {sys?.halt?.halted && (
        <div className="banner red">
          <b>⚠ TRADING HALTED</b> · {sys.halt.kind?.replace(/_/g, " ")} · incident #{sys.halt.incident_id} · new entries blocked, pending entries cancelled.
          <span className="grow" />
          <ResumeButton toast={toast} />
        </div>
      )}
      {sys?.mode === "live" && <div className="banner red"><b>● LIVE MODE · REAL MONEY</b><span className="grow" />Orders use real funds on Binance.</div>}
      {snap?.market.futures.testnet_data && page === "terminal" && (
        <div className="banner orange">Futures prices are sourced from the Binance Futures TESTNET in this deployment (production futures API not reachable here). Spot data is production.</div>
      )}
      {sys && Object.keys(sys.loop_errors).length > 0 && (
        <div className="banner orange">Loop errors: {Object.entries(sys.loop_errors).map(([k, v]) => `${k}: ${v}`).join(" · ")}</div>
      )}
      <main className="content">
        {!snap ? <div className="empty">Loading terminal state…</div> : (
          <>
            {page === "terminal" && <Terminal snap={snap} events={events} openTrade={setTrade} openAgent={setAgent} toast={toast} />}
            {page === "analytics" && <Analytics mode={snap.system.mode} />}
            {page === "strategies" && <Strategies toast={toast} />}
            {page === "logs" && <Logs openTrade={setTrade} />}
            {page === "settings" && <Settings snap={snap} toast={toast} />}
            {page === "live" && <Live snap={snap} toast={toast} />}
          </>
        )}
      </main>
      {trade && <TradeDrawer id={trade} onClose={() => setTrade(null)} toast={toast} />}
      {agent && <AgentDrawer agent={agent} card={snap?.agents.find((a) => a.name === agent)} onClose={() => setAgent(null)}
        onOpenTrade={(id) => { setAgent(null); setTrade(id); }} />}
      {node}
    </div>
  );
}

function ResumeButton({ toast }: { toast: (m: string, e?: boolean) => void }) {
  const [open, setOpen] = useState(false);
  const [pw, setPw] = useState("");
  const [note, setNote] = useState("");
  if (!open) return <button className="btn danger" onClick={() => setOpen(true)}>Review & resume…</button>;
  return (
    <span className="row">
      <input className="input" style={{ width: 220 }} placeholder="Resolution note (required)" value={note} onChange={(e) => setNote(e.target.value)} />
      <input className="input" style={{ width: 160 }} type="password" placeholder="Password" value={pw} onChange={(e) => setPw(e.target.value)} />
      <button className="btn danger" disabled={!pw || note.length < 3} onClick={async () => {
        try { await api.post("/api/emergency/resume", { password: pw, note }); toast("Trading resumed (system remains stopped until you press Start)"); setOpen(false); }
        catch (e) { toast((e as Error).message, true); }
      }}>Authorize resume</button>
      <button className="btn" onClick={() => setOpen(false)}>Cancel</button>
    </span>
  );
}
