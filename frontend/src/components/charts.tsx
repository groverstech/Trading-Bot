import {
  AreaSeries, CandlestickSeries, ColorType, createChart, HistogramSeries, LineSeries, LineStyle,
  type IChartApi, type IPriceLine, type ISeriesApi, type UTCTimestamp,
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

export function EquityChart({ mode, refreshKey }: { mode: string; refreshKey: number }) {
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const area = useRef<ISeriesApi<"Area"> | null>(null);
  const baseLine = useRef<ISeriesApi<"Line"> | null>(null);
  const dd = useRef<ISeriesApi<"Histogram"> | null>(null);
  const [view, setView] = useState<"total" | "spot" | "futures">("total");
  const [count, setCount] = useState(0);

  useEffect(() => {
    if (!el.current) return;
    const c = createChart(el.current, base);
    area.current = c.addSeries(AreaSeries, { lineColor: "#21e6ff", topColor: "rgba(33,230,255,.28)", bottomColor: "rgba(33,230,255,0)", lineWidth: 2 });
    baseLine.current = c.addSeries(LineSeries, { color: "rgba(255,208,58,.6)", lineWidth: 1, lineStyle: LineStyle.Dashed, priceLineVisible: false, lastValueVisible: false });
    dd.current = c.addSeries(HistogramSeries, { color: "rgba(255,71,102,.55)", priceFormat: { type: "percent" } as never }, 1);
    c.panes()[1]?.setHeight(60);
    chart.current = c;
    return () => { c.remove(); chart.current = null; };
  }, []);

  useEffect(() => {
    api.get<{ points: EqPoint[] }>(`/api/equity?mode=${mode}&limit=5000`).then((d) => {
      if (!area.current) return;
      // one point per second at most (chart requires strictly increasing time)
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
      let peak = -Infinity;
      dd.current?.setData(data.map((x) => {
        peak = Math.max(peak, x.value);
        return { time: x.time, value: peak > 0 ? -((peak - x.value) / peak) * 100 : 0 };
      }));
      chart.current?.timeScale().fitContent();
      setCount(data.length);
    }).catch(() => {});
  }, [mode, view, refreshKey]);

  return (
    <div style={{ display: "flex", flexDirection: "column", flex: 1, minHeight: 0 }}>
      <div className="row" style={{ marginBottom: 6 }}>
        <span className="muted mono" style={{ fontSize: 10 }}>{count} snapshots · dashed = starting equity · lower pane = drawdown %</span>
        <span className="grow" />
        <div className="seg">{(["total", "spot", "futures"] as const).map((v) => (
          <button key={v} className={view === v ? "active" : ""} onClick={() => setView(v)}>{v === "total" ? "Consolidated" : v}</button>))}</div>
      </div>
      <div ref={el} className="chart" style={{ flex: 1 }} />
    </div>
  );
}
