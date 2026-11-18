export type Mode = "paper" | "testnet" | "live";

export interface AgentCard {
  name: string;
  title: string;
  status: "idle" | "working" | "error" | "halted" | "disabled";
  activity: string;
  last_decision: string;
  ops: number;
  errors: number;
  last_ts: number | null;
  last_error: string | null;
  metrics: Record<string, unknown>;
}

export interface AgentEvent {
  id?: number;
  ts: number;
  mode: Mode;
  trade_id: string | null;
  agent: string;
  event: string;
  level: "info" | "warning" | "error" | "critical";
  message: string;
  data?: unknown;
}

export type StageState = "approved" | "rejected" | "pending" | "failed" | "working" | "waiting" | "skipped";

export interface PipelineRow {
  id: string;
  ts: number;
  updated: number;
  mode: Mode;
  market: "spot" | "futures";
  symbol: string;
  direction: "LONG" | "SHORT";
  strategy: string;
  timeframe: string;
  score: number;
  stage: string;
  reason: string;
  entry: number | null;
  stop: number | null;
  target: number | null;
  reasons: string[];
  qty: string | null;
  notional: number | null;
  risk_usdt: number | null;
  leverage: number | null;
  fill_price: string | null;
  stages: Record<"scanner" | "rugcheck" | "sizer" | "risk" | "executor" | "logger" | "equity", StageState>;
}

export interface Position {
  trade_id: string;
  mode: Mode;
  market: "spot" | "futures";
  symbol: string;
  direction: "LONG" | "SHORT";
  strategy: string;
  status: string;
  qty: number;
  entry: number;
  price: number;
  stop: number;
  target: number;
  leverage: number;
  margin: number;
  liq_price: number | null;
  notional: number;
  unrealized: number;
  pnl_pct: number;
  fees: number;
  funding: number;
  opened_at: number;
  closed_at: number | null;
  exit_price: number | null;
  exit_reason: string | null;
  realized: number;
  price_age: number;
}

export interface Ticker {
  symbol: string;
  market: "spot" | "futures";
  price: number;
  change_pct: number;
  quote_volume: number;
  bid: number;
  ask: number;
  mark: number;
  funding_rate: number;
  spread_bps: number;
  ts: number;
  rsi: number | null;
  trend: Record<string, number> | null;
  vol_ratio: number | null;
  signals: string[];
}

export interface Portfolio {
  mode: Mode;
  initialised: boolean;
  capital: { starting_inr: number; usd_inr: number; starting_usdt: number; ts: number; note: string } | null;
  starting_usdt: number;
  total_equity: number;
  total_pnl: number;
  realized_pnl: number;
  unrealized_pnl: number;
  fees_paid: number;
  funding_net: number;
  spot: { allocated: number; cash: number; holdings: number; equity: number; available: number; reserved: number; realized: number; unrealized: number; positions: number };
  futures: { allocated: number; wallet: number; margin: number; equity: number; available: number; realized: number; unrealized: number; positions: number };
  reserve: { allocated: number; balance: number };
  allocated_total: number;
  available_total: number;
  reserved_total: number;
  fx: { usd_inr: number | null; source: string; provider_updated: number | null; fetched_at: number | null; error: string | null };
  inr: { total_equity: number | null; total_pnl: number | null };
}

export interface FeedStatus {
  state: "LIVE" | "DELAYED" | "DISCONNECTED";
  age: number | null;
  source: "websocket" | "rest";
  stream: { connected: boolean; messages: number; reconnects: number; lag_ms: number; last_error: string; streams: number; last_msg_age: number | null } | null;
}

export interface Snapshot {
  ts: number;
  feed: { spot: FeedStatus; futures: FeedStatus; user_stream?: { connected: boolean; messages: number; last_error: string } };
  working: (PipelineRow & { execution: { working_cid: string; price: string; placed_at: number; first_placed_at: number; reprices: number; target_qty: string } })[];
  system: {
    running: boolean;
    mode: Mode;
    halt: { halted: boolean; kind?: string; detail?: Record<string, unknown>; ts?: number; incident_id?: number };
    started_at: number;
    session_started: number | null;
    live_auth: { status: string; first_order_confirmed?: boolean };
    allow_live_deployment: boolean;
    next_cycle: number | null;
    last_cycle: number | null;
    loop_errors: Record<string, string>;
    total_trades: number;
    auto_approve: boolean;
  };
  portfolio: Portfolio;
  performance: { wins: number; losses: number; closed: number; win_rate: number | null; active_positions: number };
  risk: {
    day_start_equity?: number;
    daily_pnl?: number;
    daily_pnl_pct?: number;
    peak_equity?: number;
    drawdown_pct?: number;
    daily_loss_breached?: boolean;
    drawdown_breached?: boolean;
    config: Record<string, number | string | boolean>;
    engine: { validated: number; approved: number; rejected: number; top_rejections: Record<string, number> };
  };
  agents: AgentCard[];
  positions: Position[];
  recent_trades: Position[];
  pipeline: PipelineRow[];
  awaiting: PipelineRow[];
  pending_orders: Record<string, unknown>[];
  incidents: { id: number; ts: number; kind: string; severity: string; detail: string }[];
  market: {
    spot: { age: number | null; error: string; source: string; testnet_data: boolean };
    futures: { age: number | null; error: string; source: string; testnet_data: boolean };
    health: Record<string, { connected: boolean; latency_ms: number; used_weight: number; weight_limit: number; last_error_msg: string }>;
  };
  watchlist: Ticker[];
}
