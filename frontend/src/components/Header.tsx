import { useEffect, useState } from "react";
import { api } from "../api";
import { duration, inr, num } from "../fmt";
import type { Snapshot } from "../types";
import { ConfirmModal } from "./ui";

export type Page = "terminal" | "analytics" | "strategies" | "logs" | "settings" | "live";
const PAGES: Page[] = ["terminal", "analytics", "strategies", "logs", "settings", "live"];

export function Header({ snap, page, setPage, connected, toast }: {
  snap: Snapshot | null; page: Page; setPage: (p: Page) => void; connected: boolean; toast: (m: string, e?: boolean) => void;
}) {
  const [, tick] = useState(0);
  const [estop, setEstop] = useState(false);
  useEffect(() => {
    const t = setInterval(() => tick((x) => x + 1), 1000);
    return () => clearInterval(t);
  }, []);
  const sys = snap?.system;
  const p = snap?.portfolio;
  const halted = sys?.halt?.halted;
  const mode = sys?.mode ?? "paper";
  const sessionSecs = sys?.session_started ? Date.now() / 1000 - sys.session_started : null;

  const run = async (path: string, label: string) => {
    try { await api.post(path); toast(label); } catch (e) { toast((e as Error).message, true); }
  };

  return (
    <header className="header">
      <div className="brand">
        <div className="brand-mark">G</div>
        <div><h1>GROK BOT TERMINAL</h1><small>6-AGENT AUTONOMOUS · BINANCE</small></div>
      </div>
      <nav className="nav">
        {PAGES.map((x) => (
          <button key={x} className={page === x ? "active" : ""} onClick={() => setPage(x)}>{x}</button>
        ))}
      </nav>
      <span className="grow" />
      <span className={`pill ${halted ? "halt" : sys?.running ? "run" : "stop"}`}>
        <span className="dot" />{halted ? "HALTED" : sys?.running ? "RUNNING" : "STOPPED"}
      </span>
      <span className={`pill ${mode}`} title={mode === "paper" ? "Simulated capital on real market data" : undefined}>
        {mode === "live" ? "● LIVE · REAL MONEY" : mode === "testnet" ? "TESTNET" : "PAPER"}
      </span>
      <div className="hsep" />
      <div className="hstat"><label>Session</label><b>{duration(sessionSecs)}</b></div>
      <div className="hstat"><label>Equity</label>
        <b>{num(p?.total_equity)} <span className="dim" style={{ fontSize: 10 }}>USDT</span></b></div>
      <div className="hstat"><label>≈ INR</label><b>{inr(p?.inr.total_equity)}</b></div>
      <div className="hstat"><label>Trades</label><b>{sys?.total_trades ?? "—"}</b></div>
      <div className="hstat"><label>Feed</label>
        <b className={connected ? "pos" : "neg"} style={{ fontSize: 11 }}>{connected ? "● ONLINE" : "○ OFFLINE"}</b></div>
      <div className="hsep" />
      {sys?.running ? (
        <button className="btn" onClick={() => run("/api/system/stop", "System stopped: no new scans")}>Stop</button>
      ) : (
        <button className="btn go" disabled={!!halted} onClick={() => run("/api/system/start", "System started")}>Start</button>
      )}
      <button className="estop" onClick={() => setEstop(true)}>⏻ EMERGENCY STOP</button>
      {estop && (
        <ConfirmModal danger title="EMERGENCY STOP" confirmLabel="Halt trading now"
          body={<>Immediately halts all new trading, cancels pending entries and applies the configured emergency
            policy (<b>{String(snap?.risk.config.emergency_policy ?? "keep_protected")}</b>) to open positions.
            Positions are never closed blindly while the exchange is unreachable. Resuming requires your password.</>}
          extraField={{ label: "Reason (recorded in the incident log)", min: 3 }}
          onConfirm={async ({ extra }) => { await api.post("/api/emergency/stop", { reason: extra }); toast("Trading halted", true); }}
          onClose={() => setEstop(false)} />
      )}
    </header>
  );
}
