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
python -m pytest -q                      # 45 tests (29 marked "safety")

# frontend
cd ../frontend
npm install && npm run build             # output served by the backend

# run
cd ../backend
GROK_ADMIN_PASSWORD='choose-a-long-password' python -m grokbot
# open http://127.0.0.1:8800 → sign in → START
```

For frontend development, run `npm run dev` in `frontend/`. It proxies `/api` and `/ws` to `GROK_API` (default `http://127.0.0.1:8800`).

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
| Testnet / live order flow | Implemented. **Verified only against the in-repo mock exchange** (`tests/fake_exchange.py`). Must be validated on the Binance testnets with your keys |
| Backtests | Real historical klines. Hypothetical results |
| Performance statistics | Computed only from recorded trades of the selected mode. Paper, testnet and live are completely separate |

## Known limitations / outstanding work

* **Testnet validation pending:** signed Spot/Futures flows (OCO, Algo conditional orders, leverage/margin type, commission parsing) have not yet run against Binance's real testnets. No testnet keys were available, and `testnet.binance.vision` is geo-blocked from the development container. The Algo Service parameters (`algoType=CONDITIONAL`, `triggerPrice`, `closePosition`, `clientAlgoId`) follow Binance's docs and change log (migration effective 2025-12-09). Confirm them on testnet first.
* **Entries are market orders.** The adapters accept LIMIT orders, but the Executor does not yet manage resting limit entries.
* Market data uses REST polling (3 s), not exchange WebSocket streams. Exchange order state is reconciled by polling (10 s), not the user-data stream.
* The liquidation price is an approximation (isolated, 0.5 % maintenance rate, no maintenance-amount tiers). Binance's own figure is authoritative.
* Paper spot fees are modelled in USDT. Exchange modes use the reported commission, converting BNB fees via the BNB spot price.
* **Strategies are not proven profitable.** A 2,000-bar BTCUSDT 1h backtest (with fees and slippage) showed losses for momentum, volume and volatility, and roughly break-even results for breakout and trend. On 1m/5m timeframes the Sizer rejects most signals because fees make net R:R < 1.5. That is intended behaviour.
* Single-operator authentication. No built-in TLS: keep it on localhost or behind a TLS reverse proxy.

## Regulatory note (India)

Not legal or tax advice, and this software makes no compliance claim. Before live trading, verify independently:

* your Binance account's eligibility and product availability in India. Binance is registered with FIU-IND, but USDⓈ-M Futures availability for Indian residents must be confirmed for your account;
* income-tax treatment of VDA gains (Section 115BBH: flat 30 %, no loss set-off) and 1 % TDS under Section 194S;
* that derivatives may be treated differently.

Product availability and rules change.
