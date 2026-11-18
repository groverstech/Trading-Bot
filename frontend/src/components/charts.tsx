import {
  AreaSeries, CandlestickSeries, ColorType, createChart, createSeriesMarkers, HistogramSeries, LineSeries, LineStyle,
  type IChartApi, type IPriceLine, type ISeriesApi, type ISeriesMarkersPluginApi, type Time, type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { Position } from "../types";

const base = {
  layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: "#7a8696", fontFamily: "JetBrains Mono, monospace", fontSize: 10 },
  grid: { vertLines: { color: "rgba(28,35,45,.55)" }, horzLines: { color: "rgba(28,35,45,.55)" } },
  rightPriceScale: { borderColor: "#1c232d" },
  timeScale: { borderColor: "#1c232d", timeVisible: true, secondsVisible: false },
  crosshair: { vertLine: { color: "#2a3646" }, horzLine: { color: "#2a3646" } },
  autoSize: true,
} as const;

export function PriceChart({ market, symbol, positions }: { market: "spot" | "futures"; symbol: string; positions: Position[] }) {
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const vol = useRef<ISeriesApi<"Histogram"> | null>(null);
  const lines = useRef<IPriceLine[]>([]);
  const [tf, setTf] = useState("15m");
  const [err, setErr] = useState("");

  useEffect(() => {
    if (!el.current) return;
    const c = createChart(el.current, base);
    series.current = c.addSeries(CandlestickSeries, {
      upColor: "#16e08a", downColor: "#ff4766", borderVisible: false, wickUpColor: "#16e08a", wickDownColor: "#ff4766",
    });
    vol.current = c.addSeries(HistogramSeries, { priceScaleId: "vol", priceFormat: { type: "volume" }, color: "rgba(33,230,255,.25)" });
    c.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    chart.current = c;
    return () => { c.remove(); chart.current = null; };
  }, []);

  useEffect(() => {
    let stop = false;
    const load = async () => {
      try {
        const d = await api.get<{ candles: number[][] }>(`/api/klines?market=${market}&symbol=${symbol}&tf=${tf}&limit=300`);
        if (stop || !series.current) return;
        series.current.setData(d.candles.map(([t, o, h, l, c]) => ({ time: t as UTCTimestamp, open: o, high: h, low: l, close: c })));
        vol.current?.setData(d.candles.map(([t, o, , , c, v]) => ({ time: t as UTCTimestamp, value: v,
          color: c >= o ? "rgba(22,224,138,.25)" : "rgba(255,71,102,.25)" })));
        setErr("");
      } catch (e) { setErr((e as Error).message); }
    };
    load();
    const t = setInterval(load, 20000);
    return () => { stop = true; clearInterval(t); };
  }, [market, symbol, tf]);

  useEffect(() => {
    const s = series.current;
    if (!s) return;
    lines.current.forEach((l) => s.removePriceLine(l));
    lines.current = [];
    positions.filter((p) => p.symbol === symbol && p.market === market).forEach((p) => {
      lines.current.push(s.createPriceLine({ price: p.entry, color: "#21e6ff", lineWidth: 1, lineStyle: LineStyle.Solid, title: `ENTRY ${p.direction}` }));
      lines.current.push(s.createPriceLine({ price: p.stop, color: "#ff4766", lineWidth: 1, lineStyle: LineStyle.Dashed, title: "SL" }));
      lines.current.push(s.createPriceLine({ price: p.target, color: "#16e08a", lineWidth: 1, lineStyle: LineStyle.Dashed, title: "TP" }));
      if (p.liq_price) lines.current.push(s.createPriceLine({ price: p.liq_price, color: "#ff9a3d", lineWidth: 1, lineStyle: LineStyle.Dotted, title: "LIQ" }));
    });
  }, [positions, symbol, market]);

  return (
    <div style={{ display: "flex", flexDirection: "column", flex: 1, minHeight: 0 }}>
      <div className="row" style={{ marginBottom: 6 }}>
        <b className="mono">{symbol}</b><span className={`tag ${market}`}>{market === "futures" ? "PERP" : "SPOT"}</span>
        <span className="grow" />
        <div className="seg">{["1m", "5m", "15m", "1h", "4h", "1d"].map((x) => (
          <button key={x} className={tf === x ? "active" : ""} onClick={() => setTf(x)}>{x}</button>))}</div>
      </div>
      {err && <div className="err">{err}</div>}
      <div ref={el} className="chart" style={{ flex: 1 }} />
    </div>
  );
}

type EqPoint = { ts: number; spot: number; futures: number; reserve: number; total: number };
type ClosedPos = { closed_at: number; realized: number; symbol: string; market: string; exit_reason: string };

export function EquityChart({ mode, refreshKey }: { mode: string; refreshKey: number }) {
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const area = useRef<ISeriesApi<"Area"> | null>(null);
  const baseLine = useRef<ISeriesApi<"Line"> | null>(null);
  const dd = useRef<ISeriesApi<"Histogram"> | null>(null);
  const markers = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const [view, setView] = useState<"total" | "spot" | "futures">("total");
  const [range, setRange] = useState<"1D" | "1W" | "1M" | "ALL">("ALL");
  const [stats, setStats] = useState<{ n: number; first: number; last: number; mdd: number; trades: number } | null>(null);

  useEffect(() => {
    if (!el.current) return;
    const c = createChart(el.current, base);
    area.current = c.addSeries(AreaSeries, { lineColor: "#21e6ff", topColor: "rgba(33,230,255,.28)", bottomColor: "rgba(33,230,255,0)", lineWidth: 2 });
    baseLine.current = c.addSeries(LineSeries, { color: "rgba(255,208,58,.6)", lineWidth: 1, lineStyle: LineStyle.Dashed, priceLineVisible: false, lastValueVisible: false });
    dd.current = c.addSeries(HistogramSeries, { color: "rgba(255,71,102,.55)", priceFormat: { type: "percent" } as never }, 1);
    c.panes()[1]?.setHeight(56);
    markers.current = createSeriesMarkers(area.current, []);
    chart.current = c;
    return () => { c.remove(); chart.current = null; };
  }, []);

  useEffect(() => {
    Promise.all([
      api.get<{ points: EqPoint[] }>(`/api/equity?mode=${mode}&limit=5000`),
      api.get<ClosedPos[]>(`/api/positions?status=CLOSED&mode=${mode}&limit=500`),
    ]).then(([d, closed]) => {
      if (!area.current) return;
      const pts: EqPoint[] = [];
      for (const p of d.points) {
        const t = Math.floor(p.ts);
        if (pts.length && Math.floor(pts[pts.length - 1].ts) === t) pts[pts.length - 1] = p; else pts.push(p);
      }
      const data = pts.map((p) => ({ time: Math.floor(p.ts) as UTCTimestamp, value: p[view] }));
      area.current.setData(data);
      if (data.length) {
        baseLine.current?.setData([{ time: data[0].time, value: data[0].value }, { time: data[data.length - 1].time, value: data[0].value }]);
      }
      let peak = -Infinity, mdd = 0;
      dd.current?.setData(data.map((x) => {
        peak = Math.max(peak, x.value);
        const v = peak > 0 ? ((peak - x.value) / peak) * 100 : 0;
        mdd = Math.max(mdd, v);
        return { time: x.time, value: -v };
      }));
      const rel = closed.filter((c) => view === "total" || c.market === view).filter((c) => data.length && c.closed_at >= data[0].time);
      const seen = new Set<number>();
      markers.current?.setMarkers(rel.sort((a, b) => a.closed_at - b.closed_at).map((c) => {
        // snap to the nearest equity point at/after the close
        const pt = data.find((x) => x.time >= Math.floor(c.closed_at)) ?? data[data.length - 1];
        return { pt, c };
      }).filter(({ pt }) => pt && !seen.has(pt.time as number) && (seen.add(pt.time as number), true)).map(({ pt, c }) => ({
        time: pt.time, position: c.realized >= 0 ? "aboveBar" as const : "belowBar" as const,
        color: c.realized >= 0 ? "#16e08a" : "#ff4766", shape: c.realized >= 0 ? "arrowUp" as const : "arrowDown" as const,
        text: `${c.symbol.replace("USDT", "")} ${c.realized >= 0 ? "+" : ""}${c.realized.toFixed(2)}`,
      })));
      const last = data[data.length - 1]?.time as number | undefined;
      if (last && range !== "ALL") {
        const span = { "1D": 86400, "1W": 7 * 86400, "1M": 30 * 86400 }[range];
        chart.current?.timeScale().setVisibleRange({ from: (last - span) as UTCTimestamp, to: last as UTCTimestamp });
      } else chart.current?.timeScale().fitContent();
      setStats(data.length ? { n: data.length, first: data[0].value, last: data[data.length - 1].value, mdd, trades: rel.length } : null);
    }).catch(() => {});
  }, [mode, view, range, refreshKey]);

  const chg = stats ? stats.last - stats.first : 0;
  return (
    <div style={{ display: "flex", flexDirection: "column", flex: 1, minHeight: 0 }}>
      <div className="row wrap" style={{ marginBottom: 6, gap: 10 }}>
        <span className="mono" style={{ fontSize: 13, fontWeight: 600 }}>{stats ? stats.last.toFixed(2) : "—"}</span>
        <span className={`mono ${chg > 0.005 ? "pos" : chg < -0.005 ? "neg" : "muted"}`} style={{ fontSize: 11 }}>
          {stats ? `${chg >= 0 ? "+" : ""}${chg.toFixed(2)} (${((chg / (stats.first || 1)) * 100).toFixed(2)}%)` : ""}</span>
        <span className="muted mono" style={{ fontSize: 10 }}>max DD {stats ? stats.mdd.toFixed(2) : "0.00"}% · {stats?.trades ?? 0} closed trades marked · {stats?.n ?? 0} snapshots</span>
        <span className="grow" />
        <div className="seg">{(["1D", "1W", "1M", "ALL"] as const).map((v) => (
          <button key={v} className={range === v ? "active" : ""} onClick={() => setRange(v)}>{v}</button>))}</div>
        <div className="seg">{(["total", "spot", "futures"] as const).map((v) => (
          <button key={v} className={view === v ? "active" : ""} onClick={() => setView(v)}>{v === "total" ? "All" : v}</button>))}</div>
      </div>
      <div ref={el} className="chart" style={{ flex: 1 }} />
    </div>
  );
}
