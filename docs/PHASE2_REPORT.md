# Phase 2 report: validation, UI review and trading-engine improvement

Live trading was **not** activated. Live-authorization safeguards were not modified, and no risk control was loosened. Two controls were added: `entry_drift` and `data_source_matches_mode`.

| # | Deliverable | Where |
|---|---|---|
| 1 | Updated screenshots, desktop and mobile, taken from the running application against the paper session | `docs/screenshots/` |
| 2 | Testnet validation report (actual exchange responses) and local procedure | `docs/testnet_report.md`, `docs/TESTNET.md` |
| 3 | Paper-trading execution evidence | `docs/paper_session_report.md` |
| 4 | Strategy performance report | `docs/STRATEGY_RESEARCH.md`, `docs/strategy_report.md` |
| 5 | Automated tests | `backend/tests/` (run `python -m pytest -q`) |
| 6 | Security and financial audit | `docs/AUDIT.md` |
| 7 | Outstanding limitations | `README.md` (Known limitations), `docs/AUDIT.md` §6 |
| 8 | Local run instructions | `README.md` (Quick start), `docs/TESTNET.md` |

## What was implemented

* **Limit-order entries.** Post-only (LIMIT_MAKER / GTX) orders with:
  * expiry, and repricing capped at `max_chase_bps` from the signal price;
  * incremental partial-fill handling, with protection always sized to the filled quantity (versioned Spot OCO; Futures `closePosition` algo orders);
  * cancel-then-confirm before every reprice, and deterministic client IDs, so no duplicates or overfills;
  * cancel-before-exit, halt cancellation, and restart recovery that keeps and protects the filled part.
* **WebSocket market data.**
  * Spot and Futures combined streams: book ticker, 24h ticker, klines, top-20 depth and mark price/funding, with automatic reconnect and back-off.
  * REST is kept for reconciliation (every 30 s when streams are healthy) and as a fallback.
  * Per-market LIVE / DELAYED / DISCONNECTED status in the UI.
  * The Futures user-data stream (listenKey) triggers REST reconciliation.
* **Strategy research.**
  * Walk-forward out-of-sample framework that mirrors the live filters, with regime, cost-drag, concentration and parameter-stability analysis.
  * Promotion criteria were fixed before any results were seen.
  * Only promoted strategies can trade on testnet/live; none were promoted.
* **Testnet harness.** Spot and Futures run separately, with recorded responses, an application-pipeline validation trade (`--pipeline`), and a test against the mock exchange.
* **Audit tool.** Independent recomputation of conversion, VWAP, fees, margin, liquidation, funding, account cash and the equity identity.
* **UI.**
  * Feed status badges; agent cards with role, pass/accept/fill-rate bars and pipeline connectors.
  * Stop-loss ↔ take-profit range bars; a working-limits tab.
  * Activity feed with agent filters, "decisions only" and pause.
  * Equity curve with trade markers, 1D/1W/1M/ALL ranges and stats.
  * Walk-forward panel; execution settings.
  * Mobile layout: bottom navigation, swipeable agent cards, compact header.

## Defects found and fixed in this phase

* **High: stale-signal entry drift** (found by the paper session). After a restart, signals were priced from candles that had closed long before, which doubled the real stop distance. Fixed in three places. See `AUDIT.md` F11.
* Momentum's `rsi_len` parameter was ignored.
* Confirmation timeframes below the entry timeframe were being used.
* The liquidation estimate formula was an approximation; replaced with Binance's isolated formula.
* WebSocket Origin was not checked; there was no session idle timeout; scrypt cost was weak; the Secure cookie could not be forced behind a TLS proxy.
* Agent counters reset on restart (they are now rebuilt from the audit trail).
* Drawer panels were clipped; numbers displayed as "-0.00".

## Verified vs not verified

| Item | Status |
|---|---|
| Paper pipeline end-to-end on genuine data (market and limit entries, stop-loss and take-profit, fees, slippage, ledger, equity, audit trail, restarts) | ✅ Evidence in `paper_session_report.md` |
| Futures testnet connectivity, clock sync, filters, market data, signed-request routing | ✅ Real exchange responses |
| Spot testnet (any step) | ❌ HTTP 451 from this environment; run locally |
| Signed testnet order flows (orders, protection, reconciliation, user stream) | ⚠️ Mock exchange only; **needs your testnet keys locally** |
| Any strategy with credible out-of-sample performance after costs | ❌ None promoted |
| Live trading | Not activated (as instructed) |
