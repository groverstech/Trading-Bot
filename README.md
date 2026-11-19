# Grok Bot Terminal

Autonomous six-agent cryptocurrency trading terminal for **Binance Spot + USDⓈ-M Futures**.
Configured capital: **₹50,000** (converted to USDT at a live USD/INR rate).
Default mode: **PAPER**. Live trading is disabled until you complete an explicit, audited activation workflow.

```
SCANNER → RUG CHECK → SIZER → CENTRAL RISK → EXECUTOR → LOGGER → EQUITY
```

## Quick start

```bash
# backend (Python ≥ 3.11)
cd backend
pip install -r requirements.txt
python -m pytest -q                      # 108 tests (76 marked "safety")

# frontend
cd ../frontend
npm install && npm run build             # output served by the backend

# run
cd ../backend
GROK_ADMIN_PASSWORD='choose-a-long-password' python -m grokbot
# open http://127.0.0.1:8800 → sign in → START
```

For frontend development, run `npm run dev` in `frontend/`. It proxies `/api` and `/ws` to `GROK_API` (default `http://127.0.0.1:8800`).

### Tools

| Command | Purpose |
|---|---|
| `python -m grokbot` | Run the terminal (API + UI + agents) |
| `python -m grokbot.research [--apply]` | Walk-forward strategy research. `--apply` records promotion decisions; this is the only way a strategy can trade on testnet/live. Report: `docs/strategy_report.md` |
| `python -m grokbot.testnet_validate --market both [--pipeline]` | Binance **testnet** validation with recorded exchange responses. See `docs/TESTNET.md` |
| `python -m grokbot.audit [--mode paper]` | Independent recomputation of balances, fees, margin, liquidation and P&L from fills and the ledger |
| `python -m grokbot.paper_report` | Paper-session evidence report from the database |
| `python -m grokbot.session_end --policy report\|cancel_pending\|close_all` | End-of-test procedure: reconcile with the exchange, verify protection, report exposure, apply the cleanup policy (never closes blindly), and state the remaining positions and orders |
| `python -m grokbot.testnet_validate --check` | Testnet preflight: read-only checks of environment, isolation, credentials, permissions, account configuration, exposure and the safety tests; places nothing |
| `python -m grokbot.testnet_validate --cleanup-only` | Cancel run-created orders and flatten run-created exposure left by an interrupted run |
| `python -m grokbot.paper_validation` | Operator **validation** trades (labelled, not strategy signals) through the unchanged pipeline in paper mode, to observe stop-loss/take-profit on real prices |

### Documentation

* `docs/TESTNET.md`: testnet status and local procedure. `docs/testnet_report.md`: latest harness output.
* `docs/STRATEGY_RESEARCH.md`: why the strategies underperform. `docs/strategy_report.md`: full walk-forward tables.
* `docs/paper_session_report.md`: end-to-end paper-trading evidence.
* `docs/AUDIT.md`: financial and security audit.
* `docs/PHASE2_1_REPORT.md`: ETH stop investigation, open-position resolution, testnet lifecycle validation.
* `docs/PHASE3_READINESS.md`: readiness checklist for real testnet validation.
* `docs/WINDOWS_SETUP.md`: Windows PowerShell setup, secure credential entry, preflight, execution and cleanup verification (`scripts/windows/`).

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `GROK_DATA_DIR` | `./data` | SQLite DB, master key file |
| `GROK_MASTER_KEY` | (generated `data/master.key`) | Fernet key for API-secret encryption. Prefer setting this from a secrets manager |
| `GROK_ADMIN_PASSWORD` | – | Operator password on first run (otherwise set in the UI) |
| `GROK_HOST` / `GROK_PORT` | `127.0.0.1` / `8800` | Bind address. Put TLS + firewall in front if not localhost |
| `GROK_ALLOW_LIVE` | `0` | Deployment-level kill switch. Live trading is impossible unless `1` |
| `GROK_SPOT_DATA_URL` | `https://data-api.binance.vision` | Spot public market data (official market-data-only mirror) |
| `GROK_FUTURES_DATA_URL` | `https://fapi.binance.com` | Futures public market data |
| `GROK_SPOT_TESTNET_URL` / `GROK_FUTURES_TESTNET_URL` | `testnet.binance.vision` / `testnet.binancefuture.com` | Test environments |
| `GROK_SPOT_LIVE_URL` / `GROK_FUTURES_LIVE_URL` | `api.binance.com` / `fapi.binance.com` | Live trading |
| `GROK_FX_URL` | `https://open.er-api.com/v6/latest/USD` | USD/INR rate (USDT treated as 1 USD; shown with timestamp) |
| `GROK_PROFILE` | – | `testnet` points all market data and WebSocket streams at the Binance testnets (required for testnet trading) |
| `GROK_SPOT_WS_URL` / `GROK_FUTURES_WS_URL` | `data-stream.binance.vision` / `fstream.binance.com` | WebSocket market streams |
| `GROK_STREAMS` | `1` | `0` disables WebSocket streams (REST polling only) |
| `GROK_SESSION_IDLE_MINUTES` | `120` | Session idle timeout |
| `GROK_COOKIE_SECURE` | `0` | `1` forces the Secure cookie flag (behind a TLS reverse proxy) |

## Architecture

```
backend/grokbot/
  settings.py          deployment settings (env)
  config_store.py      validated, audited runtime config (risk, capital, strategies, scanner, automation)
  db.py                SQLite (WAL, synchronous=FULL): trades, events, orders, fills, positions, ledger, ...
  security.py          Fernet credential vault, scrypt password, sessions, HMAC approval tokens, rate limiter
  state.py             mode / running / halt / incidents / live-authorization workflow
  exchange/
    client.py          signed REST client: time sync, weight limits, 429/418 back-off, safe retry policy
    spot.py            Spot adapter (/api/v3, OCO via /api/v3/orderList/oco)
    futures.py         USDⓈ-M adapter (/fapi; conditional orders via Algo Service /fapi/v1/algoOrder)
    filters.py         exchange filters: tick/step size, min qty, min notional
    broker.py          PaperBroker (fills against the real order book) / ExchangeBroker (testnet/live)
  market/data.py       ticker/book/mark/funding polling, closed-candle-only kline cache, staleness tracking
  market/fx.py         USD/INR
  strategy/            indicators (causal), 5 strategies, backtester
  agents/              scanner, rugcheck, sizer, executor, logger, equity
  risk.py              central risk engine and authorization tokens
  portfolio.py         ledger-based accounting (Spot / Futures / Reserve; per mode)
  orchestrator.py      pipeline state machine, dedupe, halt/resume, approvals
  analytics.py         performance statistics from recorded data only
  api/                 FastAPI REST + WebSocket
frontend/              React + TypeScript + lightweight-charts terminal UI
```

### The six agents

| Agent | Does | Key safeguards |
|---|---|---|
| **Scanner** | Scans configured Spot/Futures pairs on entry timeframes (1m–1d) with higher-TF trend confirmation. Runs the enabled strategies and ranks signals | Closed candles only. Records explicit no-trade decisions. Never fabricates signals |
| **Rug Check** | Exchange status, data freshness, 24h liquidity, spread, depth within ±band vs. order size, book imbalance, abnormal candle/24h move, volume z-score, candle continuity. Futures: funding (level and adverse cost), mark/last deviation, open interest, liquidation buffer | Every check is persisted with value, limit and reason |
| **Sizer** | Deterministic Decimal sizing: risk budget ÷ (stop distance + fees + slippage), volatility factor, correlation factor, caps, exchange rounding, leverage/margin, liquidation estimate, net R:R | No AI involvement. Rejects below exchange minimums |
| **Executor** | Market entries, exchange-side SL/TP, position monitor, exits, funding, reconciliation | Needs a valid HMAC risk token. Compare-and-set stage transition. Deterministic client order IDs. Never resubmits ambiguous orders |
| **Logger** | Persists every decision and transaction under the trade UID. Searchable audit trail | Secrets scrubbed |
| **Equity** | Snapshots after every fill/close and each minute. Win rate, PF, drawdown. Watches daily-loss and drawdown limits | A breach triggers the emergency halt |

### Safety model

* **No bypass:** an opportunity executes only from stage `RISK_APPROVED` with an HMAC token bound to (trade id, mode, qty, stop, expiry). Rejected trades never reach an order call. This is tested.
* **Idempotency:** each opportunity has a UNIQUE dedupe key. Entry client order IDs are deterministic primary keys, so a trade can be submitted at most once.
* **Restarts:** the system restarts **stopped**. Pending signals expire. `EXECUTING` trades are reconciled by client order ID: found and filled → recovered into a position; not found → abandoned, **not resubmitted**. Live authorization is always revoked on restart.
* **Halt:** manual emergency stop, or automatic on daily-loss / drawdown breach. It stops new entries, cancels pending ones and applies the emergency policy: `keep_protected` (default) or `flatten`. `flatten` closes only if the exchange is reachable and data is fresh; it never closes blindly during an outage. Each halt records an incident. Resuming requires your password and a note.
* **Stale data / disconnection:** entries are blocked when prices are older than `stale_data_seconds` or a connection is unhealthy. The paper position monitor will not trigger exits on stale prices.
* **Leverage ≠ capital:** futures equity = wallet + unrealized P&L. Margin is a reservation. Leverage never increases available capital (tested).

### Live-trading activation

1. Preconditions, all required:
   * `GROK_ALLOW_LIVE=1`
   * live API keys stored and verified, with **withdrawals disabled** (checked via `/sapi/v1/account/apiRestrictions`)
   * the current risk configuration confirmed
   * the safety self-test passed within 24 h
   * at least one completed testnet round-trip
   * eligibility / regulatory / tax acknowledgement
   * no active halt
2. **Authorize** with your password.
3. **Activate** with the typed phrase `ACTIVATE LIVE TRADING` plus your password, while the system is stopped.
4. The **first real-money order** is held until you confirm it with your password.

Authorization is revoked on restart, on credential change, on a risk-config change, or if the deployment flag is removed.

## What is real vs. simulated

| Item | Status |
|---|---|
| Spot market data (tickers, books, klines, exchange filters) | **Real**, Binance production (`data-api.binance.vision`) |
| Futures market data | **Real** from `fapi.binance.com` by default. In the cloud dev container fapi was geo-blocked, so the demo used the **Futures testnet** feed (labelled in the UI) |
| USD/INR | **Real** (open.er-api.com, daily). USDT assumed = 1 USD |
| Paper fills | **Simulated** against a fresh real order-book snapshot + configurable extra slippage + Binance base-tier fees |
| Testnet / live order flow | Implemented. Signed flows **verified only against the in-repo mock exchange**. Futures testnet connectivity and market data verified live. See `docs/testnet_report.md` |
| Market data delivery | Binance **WebSocket** streams (book ticker, 24h ticker, klines, top-20 depth, mark price) with automatic reconnect. REST used for reconciliation and fallback. UI shows LIVE / DELAYED / DISCONNECTED per market |
| Backtests | Real historical klines. Hypothetical results |
| Performance statistics | Computed only from recorded trades of the selected mode. Paper, testnet and live are completely separate |

## Known limitations / outstanding work

* **Testnet:** signed order flows have not yet run against Binance's testnets. No testnet keys were available, and the Spot testnet is geo-blocked (HTTP 451) from the development container; the Futures testnet is reachable and its unauthenticated steps pass. The harness is ready and tested against the mock exchange. Run it locally: `docs/TESTNET.md`.
* **No strategy is promoted** for automated testnet/live trading; none met the walk-forward criteria after costs (`docs/STRATEGY_RESEARCH.md`). Paper research mode (paper only, off by default) lets unpromoted strategies forward-test.
* Spot user data is reconciled by REST polling. The Futures user-data stream is used, but only as a trigger for REST reconciliation.
* Paper limit fills use a conservative model: the price must trade through the order, and queue position is ignored.
* The liquidation estimate uses a fixed 0.5% maintenance rate. Binance's figure is authoritative, and some altcoin tiers are higher (`docs/AUDIT.md` F2).
* The master key falls back to a file next to the database; use `GROK_MASTER_KEY` in production.
* Single-operator authentication, no 2FA, no built-in TLS: keep it on localhost or behind a TLS reverse proxy.

## Regulatory note (India)

Not legal or tax advice, and this software makes no compliance claim. Before live trading, verify independently:

* your Binance account's eligibility and product availability in India. Binance is registered with FIU-IND, but USDⓈ-M Futures availability for Indian residents must be confirmed for your account;
* income-tax treatment of VDA gains (Section 115BBH: flat 30 %, no loss set-off) and 1 % TDS under Section 194S;
* that derivatives may be treated differently.

Product availability and rules change.
