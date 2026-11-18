/** Rounds away float noise so tiny values never render as "-0.00". */
const clean = (v: number, d: number) => (Math.abs(v) < 0.5 * 10 ** -d ? 0 : v);

export const num = (v: number | null | undefined, d = 2) =>
  v === null || v === undefined || Number.isNaN(v) ? "—" : clean(v, d).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });

export const signed = (v: number | null | undefined, d = 2) =>
  v === null || v === undefined ? "—" : `${clean(v, d) >= 0 ? "+" : ""}${num(v, d)}`;

export const pct = (v: number | null | undefined, d = 2) => (v === null || v === undefined ? "—" : `${num(v, d)}%`);

export const spct = (v: number | null | undefined, d = 2) =>
  v === null || v === undefined ? "—" : `${clean(v, d) >= 0 ? "+" : ""}${num(v, d)}%`;

export function price(v: number | null | undefined) {
  if (v === null || v === undefined) return "—";
  const a = Math.abs(v);
  const d = a >= 1000 ? 2 : a >= 10 ? 3 : a >= 1 ? 4 : 6;
  return num(v, d);
}

export const inr = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : `₹${v.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;

export const usdt = (v: number | null | undefined, d = 2) => (v === null || v === undefined ? "—" : `${num(v, d)} USDT`);

export const clock = (ts: number | null | undefined) =>
  ts ? new Date(ts * 1000).toLocaleTimeString("en-GB", { hour12: false }) : "—";

export const datetime = (ts: number | null | undefined) =>
  ts ? new Date(ts * 1000).toLocaleString("en-GB", { hour12: false, month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—";

export function duration(seconds: number | null | undefined) {
  if (seconds === null || seconds === undefined || seconds < 0) return "—";
  const s = Math.floor(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = s % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(ss).padStart(2, "0")}`;
}

export function ago(ts: number | null | undefined) {
  if (!ts) return "—";
  const s = Date.now() / 1000 - ts;
  if (s < 60) return `${Math.max(0, Math.floor(s))}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export const pnlClass = (v: number | null | undefined) => (v === null || v === undefined || Math.abs(v) < 0.005 ? "" : v > 0 ? "pos" : "neg");

export const compact = (v: number) =>
  v >= 1e9 ? `${(v / 1e9).toFixed(2)}B` : v >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `${(v / 1e3).toFixed(1)}K` : v.toFixed(0);
