import { useEffect, useState, type ReactNode } from "react";

export function Panel({ title, accent, right, children, flush, className, style }: {
  title: ReactNode; accent?: string; right?: ReactNode; children: ReactNode; flush?: boolean; className?: string;
  style?: React.CSSProperties;
}) {
  return (
    <section className={`panel ${className ?? ""}`} style={style}>
      <div className="panel-h">
        <span className="acc" style={accent ? { background: accent, boxShadow: `0 0 8px ${accent}` } : undefined} />
        <span className="t">{title}</span>
        <span className="grow" />
        {right}
      </div>
      <div className={`panel-b ${flush ? "flush" : ""}`} style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}>
        {children}
      </div>
    </section>
  );
}

export function Tag({ kind, children }: { kind?: string; children: ReactNode }) {
  return <span className={`tag ${kind ?? ""}`}>{children}</span>;
}

export function Dir({ d }: { d: string }) {
  return <Tag kind={d === "LONG" ? "long" : "short"}>{d}</Tag>;
}

export function Mkt({ m }: { m: string }) {
  return <Tag kind={m}>{m === "futures" ? "PERP" : "SPOT"}</Tag>;
}

export function Meter({ label, value, limit, fmt, invert }: {
  label: string; value: number; limit: number; fmt: (v: number) => string; invert?: boolean;
}) {
  const used = limit > 0 ? Math.min(100, Math.max(0, (value / limit) * 100)) : 0;
  const cls = used >= 90 ? "hi" : used >= 60 ? "mid" : "";
  return (
    <div className="meter">
      <div className="meter-top">
        <span>{label}</span>
        <span className="mono">{fmt(value)} <span className="dim">/ {fmt(limit)}</span></span>
      </div>
      <div className="bar"><i className={invert ? "" : cls} style={{ width: `${used}%` }} /></div>
    </div>
  );
}

export function Checks({ checks }: { checks: { name: string; ok: boolean; detail?: string; value?: unknown; limit?: unknown }[] }) {
  return (
    <div className="checks">
      {checks.map((c) => (
        <div className="checkrow" key={c.name}>
          <span className={`i ${c.ok ? "pos" : "neg"}`}>{c.ok ? "✓" : "✕"}</span>
          <span className={c.ok ? "" : "neg"}>{c.name}</span>
          <span className="muted" style={{ overflow: "hidden", textOverflow: "ellipsis" }}>
            {c.detail}
            {c.value !== undefined && c.value !== null ? ` · ${String(c.value)}` : ""}
            {c.limit !== undefined && c.limit !== null ? ` (limit ${String(c.limit)})` : ""}
          </span>
        </div>
      ))}
    </div>
  );
}

export function Modal({ title, danger, children, onClose }: { title: string; danger?: boolean; children: ReactNode; onClose: () => void }) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose]);
  return (
    <div className="modal-wrap" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`modal ${danger ? "danger" : ""}`} role="dialog" aria-modal="true">
        <h3 className={danger ? "neg" : "cyan"}>{title}</h3>
        {children}
      </div>
    </div>
  );
}

/** Confirmation dialog that can require the operator password and/or a typed phrase. */
export function ConfirmModal({ title, body, danger, needPassword, phrase, extraField, confirmLabel, onConfirm, onClose }: {
  title: string; body: ReactNode; danger?: boolean; needPassword?: boolean; phrase?: string;
  extraField?: { label: string; min?: number }; confirmLabel?: string;
  onConfirm: (v: { password: string; phrase: string; extra: string }) => Promise<unknown>; onClose: () => void;
}) {
  const [password, setPassword] = useState("");
  const [typed, setTyped] = useState("");
  const [extra, setExtra] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const ready = (!needPassword || password) && (!phrase || typed === phrase) && (!extraField || extra.length >= (extraField.min ?? 1));
  return (
    <Modal title={title} danger={danger} onClose={onClose}>
      <div className="note">{body}</div>
      {extraField && (
        <div className="field"><label>{extraField.label}</label>
          <input className="input" value={extra} onChange={(e) => setExtra(e.target.value)} autoFocus /></div>
      )}
      {phrase && (
        <div className="field"><label>Type exactly: <span className="neg">{phrase}</span></label>
          <input className="input" value={typed} onChange={(e) => setTyped(e.target.value)} /></div>
      )}
      {needPassword && (
        <div className="field"><label>Operator password</label>
          <input className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)}
            autoFocus={!extraField} /></div>
      )}
      {err && <div className="err">{err}</div>}
      <div className="row" style={{ justifyContent: "flex-end" }}>
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className={`btn ${danger ? "danger" : "primary"}`} disabled={!ready || busy} onClick={async () => {
          setBusy(true); setErr("");
          try { await onConfirm({ password, phrase: typed, extra }); onClose(); }
          catch (e) { setErr((e as Error).message); }
          finally { setBusy(false); }
        }}>{busy ? "Working…" : confirmLabel ?? "Confirm"}</button>
      </div>
    </Modal>
  );
}

export function Drawer({ title, accent, onClose, children, right }: { title: ReactNode; accent?: string; onClose: () => void; children: ReactNode; right?: ReactNode }) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose]);
  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <aside className="drawer">
        <div className="drawer-h">
          <h2 style={{ color: accent ?? "var(--cyan)" }}>{title}</h2>
          <span className="grow" />
          {right}
          <button className="btn sm" onClick={onClose}>Close ✕</button>
        </div>
        <div className="drawer-b">{children}</div>
      </aside>
    </div>
  );
}

export function useToast() {
  const [msg, setMsg] = useState<{ text: string; err?: boolean } | null>(null);
  useEffect(() => {
    if (!msg) return;
    const t = setTimeout(() => setMsg(null), 4500);
    return () => clearTimeout(t);
  }, [msg]);
  const node = msg ? (
    <div style={{ position: "fixed", bottom: 18, right: 18, zIndex: 80 }}
      className={`banner ${msg.err ? "red" : "cyan"}`}>{msg.text}</div>
  ) : null;
  return { toast: (text: string, err?: boolean) => setMsg({ text, err }), node };
}

export const AGENT_COLORS: Record<string, string> = {
  scanner: "var(--cyan)", rugcheck: "var(--orange)", sizer: "var(--purple)", executor: "var(--green)",
  logger: "var(--blue)", equity: "var(--yellow)", risk: "#ff7ad9", orchestrator: "#c3ccd8",
};
