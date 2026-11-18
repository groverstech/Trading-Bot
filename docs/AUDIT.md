# Financial and security audit (Phase 2)

Scope: the code on branch `ccr-2e138369-wawg3y`, plus the records of the extended paper session.

Method:
* Code review.
* An independent recalculation tool, `python -m grokbot.audit`, that recomputes every reported figure from raw fills and the ledger without using the portfolio code.
* Automated tests. The audit runs inside the test suite after full trade lifecycles.

No regulatory or tax compliance claim is made. See the README's regulatory note.

## 1. Independent calculation checks

`grokbot.audit` checks the following. It is run against the paper-session database (results embedded in `paper_session_report.md`) and in the test `test_independent_audit_after_full_lifecycles`, which covers a spot take-profit, a futures stop-loss with funding, and a partial limit fill.

| Area | Independent recomputation | Result |
|---|---|---|
| INR → USDT | `starting_inr / usd_inr` vs stored `starting_usdt`; allocations sum to the starting capital | ✅ ₹50,000 / 93.513494 = 534.6822 USDT |
| Entry price | quantity-weighted VWAP of the entry fills (net of base-asset fees on Spot) vs the position's entry price | ✅ |
| Fees | sum of fill fees vs position fees. For paper, each fill's implied rate vs the configured maker/taker rate | ✅ (e.g. 0.0005 futures taker) |
| Futures margin | `qty × entry / leverage` | ✅ |
| Liquidation | Binance isolated closed form `LP = EP(1−1/L)/(1−MMR)` for longs, `EP(1+1/L)/(1+MMR)` for shorts | ✅ (formula corrected in this phase, see §2) |
| Realized P&L | `Σ d·(exit − VWAP)·qty − fees + funding`; closed quantity = entry quantity | ✅ |
| Funding | position funding = sum of the ledger's funding rows; sign: longs pay positive rates | ✅ |
| Account separation | Spot, Futures and Reserve cash recomputed from fills vs ledger sums | ✅ |
| Equity identity | `spot cash + holdings + futures wallet + reserve = start + realized − open fees + open funding` | ✅ |
| Ledger integrity | no trade-linked ledger row without fills | ✅ |

## 2. Financial findings

| # | Finding | Severity | Status |
|---|---|---|---|
| F1 | The liquidation estimate used `EP·(1 − 1/L + MMR)`, an approximation. Replaced with Binance's isolated formula (above). Both were conservative (estimate closer to entry than the true value) for MMR ≥ the actual tier rate | Low | **Fixed** |
| F2 | The maintenance rate is fixed at 0.5%. Binance's tier-1 rate is 0.4% for BTC/ETH (so the estimate is conservative) but **can be higher for some altcoins**, where the estimate would be optimistic. At the 1–2x leverage limit the liquidation price is about 50% away while stops are 1–5% away, so the practical impact is nil today. Binance's figure is authoritative in exchange modes | Low | Open: use the signed `leverageBracket` endpoint in exchange modes |
| F3 | Momentum's `rsi_len` parameter was ignored, so RSI was always 14 | Medium (research validity) | **Fixed** |
| F4 | Testnet orders could be priced from production market data | Medium | **Fixed**: `GROK_PROFILE=testnet` plus risk check `data_source_matches_mode` |
| F5 | The configured ₹ budget could exceed the real exchange balance | Medium | **Fixed in Phase 1**: risk check `exchange_balance` for testnet/live |
| F6 | Paper Spot fees are charged in USDT. Binance deducts the base asset on buys unless BNB fee payment is on, so real held quantity is about 0.1% smaller | Low | Documented. Exchange modes use the reported commission and net quantity |
| F7 | USDT is valued at 1 USD for INR display. The rate is fixed at initialisation for the capital figure; INR equity display uses the current rate | Info | Documented in the UI |
| F8 | Paper funding uses the last published funding rate at the settlement rollover; Binance settles on mark-price notional at that rate | Info | Documented |
| F9 | The paper limit-fill model is conservative: an order fills only when the opposite side trades **through** the price, and queue position is ignored | Info | Documented. Testnet/live use real fills |
| F11 | **Found during the paper session:** after a restart, the first scan priced signals at the close of a candle that had closed long before (a 1h candle closed 41 min earlier). The market had moved 0.7–1.6%, so the actual stop distance of two trades was about 2× the planned one. The LINK short then lost 0.768 USDT against 0.40 USDT planned (still under the 1% max-risk cap). Backtests were unaffected (they enter at the next bar's open) | **High** | **Fixed** with three layers: Scanner drops signals from candles older than `max(120 s, 2×interval)`; Sizer sizes from the executable bid/ask, not the candle close; new risk check `entry_drift` rejects adverse drift > 25% of the stop distance. Regression tests replay the LINK case |
| F10 | Futures paper prices in this cloud environment came from the **testnet** futures feed (production is geo-blocked). Testnet books are thinner, so futures paper slippage here is not representative | Info | Labelled in UI and reports |

## 3. Reconciliation and recovery

| Mechanism | Evidence |
|---|---|
| Deterministic client order IDs (PRIMARY KEY) mean a trade is submitted at most once | Tests `test_duplicate_signal_processed_once`, `test_order_timeout_reconciled_never_resubmitted`, `test_reprice_respects_chase_cap_and_never_duplicates` |
| An ambiguous submission (timeout) is reconciled by client ID and never resent | `test_order_timeout_reconciled_never_resubmitted` |
| Restart: system comes back STOPPED, pending signals expire, `EXECUTING` orders are recovered or abandoned, working limits are cancelled and their filled part kept and protected | `test_restart_*` tests, plus **two real restarts with open positions** during the paper session (order count unchanged, positions intact) |
| Exchange positions: protection re-placed if missing after restart. Exit detection by REST polling (10 s), accelerated by the Futures user-data stream | `test_testnet_futures_uses_algo_conditional_orders`, recovery code |
| Partial fills: protection always matches the filled quantity (Spot OCO cancelled and re-placed with a versioned list ID; Futures `closePosition` algo orders) | `test_testnet_spot_partial_fills_resize_oco`, `test_testnet_futures_partial_fill_protected_once` |

## 4. Security findings

| # | Finding | Severity | Status |
|---|---|---|---|
| S1 | The WebSocket `/ws` did not check `Origin`, a cross-site WebSocket hijacking risk. Mitigated by SameSite=Strict but not enforced | Medium | **Fixed**: Origin must match Host (test `test_websocket_rejects_foreign_origin`) |
| S2 | No session idle timeout (12 h absolute only) | Medium | **Fixed**: 120 min idle timeout (`GROK_SESSION_IDLE_MINUTES`) |
| S3 | scrypt N=2¹⁴ is below the OWASP recommendation | Low | **Fixed**: N=2¹⁷, versioned hash format, transparent rehash of old hashes |
| S4 | The cookie `Secure` flag was set only when the app itself saw HTTPS, which fails behind a TLS reverse proxy | Low | **Fixed**: `GROK_COOKIE_SECURE=1` |
| S5 | The master key falls back to `data/master.key` (mode 0600) **next to the database**. A copy of the data directory includes the key | Medium | Open (documented): set `GROK_MASTER_KEY` from an OS keychain or secrets manager in production |
| S6 | API secrets: Fernet-encrypted at rest, write-only through the API, never logged (logger scrubbing) | — | Verified by test `test_credentials_are_write_only_and_password_protected` |
| S7 | Withdrawal permission: live activation refuses keys with `enableWithdrawals=true` | — | Verified in code; needs a live key to exercise |
| S8 | CSRF: SameSite=Strict cookie plus a mandatory `X-Grok-Request` header on mutations; no CORS headers are emitted | — | Tested |
| S9 | Brute force: login limited to 5 attempts per minute per IP, plus a constant 0.5 s delay on failure | — | Tested manually |
| S10 | Sensitive actions (credentials, resume after halt, live authorize/activate, first live order) require the password again | — | Tested |
| S11 | Single operator account; no 2FA | Low | Open: keep the terminal on localhost or behind an authenticated TLS proxy |
| S12 | Promotion status cannot be set through the API or UI, only by the walk-forward tool | — | Tested (`test_scanner_only_trades_promoted_strategies`) |

## 5. Emergency stop

Tested behaviour:
* new entries blocked;
* pending and working entry orders cancelled (including resting limit orders);
* `keep_protected` (default) leaves stop-loss/take-profit active;
* `flatten` closes positions **only** when the exchange is reachable and data is fresh;
* an incident is recorded;
* resuming requires the password and a note.

Daily-loss and drawdown breaches trigger the same halt automatically. None of these controls were weakened in this phase.

## 6. Assumptions and limitations

* Fee rates are Binance base-tier defaults (configurable). Exchange modes use the actual reported commissions.
* Slippage in paper mode = walking the real order book + 2 bps. Research uses 3 bps per side.
* The INR figure relies on open.er-api.com (daily updates).
* Live trading was not activated or tested with real funds, as instructed.
