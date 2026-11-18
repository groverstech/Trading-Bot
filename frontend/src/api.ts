import { useEffect, useRef, useState } from "react";
import type { AgentEvent, Snapshot } from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: {
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...(method !== "GET" ? { "X-Grok-Request": "1" } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const d = data?.detail;
    const msg = typeof d === "string" ? d : Array.isArray(d) ? d.map((x: { msg: string }) => x.msg).join("; ") : res.statusText;
    if (res.status === 401) window.dispatchEvent(new Event("grok-unauthorized"));
    throw new ApiError(res.status, msg);
  }
  return data as T;
}

export const api = {
  get: <T,>(p: string) => request<T>("GET", p),
  post: <T,>(p: string, b: unknown = {}) => request<T>("POST", p, b),
  put: <T,>(p: string, b: unknown) => request<T>("PUT", p, b),
  del: <T,>(p: string) => request<T>("DELETE", p),
};

/** Live terminal state over WebSocket with automatic reconnect. */
export function useLive(enabled: boolean) {
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const retry = useRef(0);

  useEffect(() => {
    if (!enabled) return;
    let ws: WebSocket | null = null;
    let closed = false;
    let timer: number | undefined;
    api.get<AgentEvent[]>("/api/events?limit=150").then((ev) => setEvents(ev)).catch(() => {});
    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws`);
      ws.onopen = () => {
        setConnected(true);
        retry.current = 0;
      };
      ws.onmessage = (m) => {
        const msg = JSON.parse(m.data);
        if (msg.type === "snapshot") setSnap(msg.data);
        else if (msg.type === "event") setEvents((prev) => [msg.event, ...prev].slice(0, 400));
      };
      ws.onclose = (e) => {
        setConnected(false);
        if (e.code === 4401) {
          window.dispatchEvent(new Event("grok-unauthorized"));
          return;
        }
        if (!closed) timer = window.setTimeout(connect, Math.min(1000 * 2 ** retry.current++, 15000));
      };
    };
    connect();
    return () => {
      closed = true;
      if (timer) clearTimeout(timer);
      ws?.close();
    };
  }, [enabled]);

  return { snap, events, connected };
}
