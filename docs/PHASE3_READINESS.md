# Phase 3 readiness: real Binance testnet validation

Status: **ready for you to configure testnet credentials and run.** Nothing below activated live trading, changed the ₹50,000 capital configuration, loosened a risk control, or used any credential.

## Readiness checklist

| # | Requirement | How it is guaranteed | Proof |
|---|---|---|---|
| 1 | **Testnet fully isolated from production endpoints** | `grokbot/endpoints.py` holds an authoritative allowlist of Binance TESTNET and PRODUCTION hosts. Settings are validated at startup: a testnet URL that is not a testnet host (or vice versa) **refuses to start**, so an environment-variable typo cannot route orders to production. With `GROK_PROFILE=testnet`, *all* market-data and stream URLs must be testnet hosts. The harness validates its own hosts and can only build testnet clients. The risk check `data_source_matches_mode` uses the host allowlist | `test_isolation.py::test_misrouted_endpoints_refuse_to_start` (4 cases incl. a look-alike host), `test_testnet_profile_isolates_all_market_data` |
| 2 | **No production credentials can be loaded in testnet mode** | A credentialed REST client requires an explicit environment, and refuses to attach keys to a host of the other environment. The testnet profile can never build a live trading client. The harness reads only `GROK_TESTNET_*` variables. **No environment variable for live keys exists**: they can only be entered through the UI vault. The same key cannot be stored in both the testnet and live slots. The preflight fails if the data directory holds any live credentials. A production key pasted by mistake is only ever sent to testnet hosts, which reject it (`-2015`) | `test_credentials_only_travel_to_matching_environment`, `test_testnet_profile_never_builds_live_client`, `test_same_key_cannot_be_stored_as_testnet_and_live` |
| 3 | **Keys cannot be used for withdrawals or transfers** | The application contains **no** withdrawal or transfer code. Every signed request passes an **explicit allowlist** of (method, path) pairs, plus a deny pattern (withdraw, transfer, capital, asset, sub-account, margin, loan, convert…), and is refused **before any network call**. For live keys (not used in Phase 3), activation additionally requires the exchange to report `enableWithdrawals`, `enableInternalTransfer` and `permitsUniversalTransfer` all **false**. Testnet keys cannot be inspected via `/sapi`, and testnet funds have no value | `test_forbidden_and_unlisted_signed_endpoints_refused`, `test_client_refuses_withdrawal_before_any_network_call`, `test_codebase_has_no_withdrawal_or_transfer_calls` |
| 4 | **No switch to live through an env-variable mistake or UI action** | `GROK_PROFILE=testnet` + `GROK_ALLOW_LIVE=1` **refuses to start**. The harness refuses to run if `GROK_ALLOW_LIVE=1` is set. The UI mode endpoint accepts only paper/testnet. Live requires the unchanged workflow: deployment flag, verified live keys, withdrawals and transfers disabled, confirmed risk configuration, safety tests, a completed testnet round-trip, the jurisdiction acknowledgement, password authorization, the typed activation phrase, and separate confirmation of the first real order. It is revoked on every restart | `test_testnet_profile_cannot_enable_live`, `test_live_cannot_be_entered_via_mode_switch`, `test_live_activation_blocked_without_preconditions`, `test_live_authorization_revoked_on_restart` |
| 5 | **Preflight covers connectivity, credentials, permissions, account configuration and safety** | `python -m grokbot.testnet_validate --check`, read-only. **Environment:** Python and modules. **Safety:** profile, live flag off, endpoint validation, ₹50,000 capital, risk limits not looser than defaults, no live credentials in the data dir, no leftover exposure marker, **safety test suite**. **Per market:** testnet reachable, clock offset < 1 s, symbol trading, keys accepted (signed read), trading permission, test balance sufficient, Futures One-way mode and single-asset margin, **no pre-existing position or orders** on the test symbol. Withdraw/transfer capability is reported | Run here without keys (see below) |
| 6 | **Pipeline validates real acknowledgements, fills, protective orders, TP, SL, reconciliation and cleanup** | Adapter level: limit order + ack, status, duplicate-ID rejection, cancel, market fill + commission, partial-fill probe, **SL/TP placed in the same step as the fill** and verified on the exchange, reconciliation, liquidation comparison, flatten, history, restart lookup, user stream. **Exchange-triggered TAKE-PROFIT test and STOP-LOSS test**: levels a few bps from the market so the **exchange** executes them, reported as TRIGGERED with fill vs level in bps, or NOT_TRIGGERED. `--pipeline` runs the full application lifecycle through Rug Check, Sizer and Risk, waits for the exchange to fire SL/TP, then reconciliation, equity update, cleanup and audit | `test_testnet_harness.py` (mock exchange): exchange-triggered Futures stop, Spot OCO take-profit, no-trigger cleanup |
| 7 | **A failed or interrupted test cannot leave an unprotected position** | Protection is placed **in the same step** as every fill; if placement fails, the position is flattened immediately. Futures exits flatten *before* removing `closePosition` protection. The partial-fill probe flattens its own fill. Exchange-side SL/TP **stay on the exchange** if the process dies. An **end-of-run guard** runs on success, error, Ctrl-C or SIGTERM: it cancels the run's orders and algo orders on the test symbol, flattens **only run-created** exposure (Spot uses a persisted start baseline, never selling pre-existing holdings), and re-verifies. If the exchange is unreachable it writes `LEFTOVER_EXPOSURE.json`, and the harness and preflight **refuse to run** until `--cleanup-only` resolves it. The harness refuses to trade a symbol with pre-existing exposure. In the app itself, restart recovery re-places missing exchange protection and replays paper downtime | `test_guard_cleans_interrupted_run`, `test_guard_writes_leftover_marker_when_exchange_unreachable`, `test_harness_never_leaves_exposure_and_keeps_preexisting_holdings`, `test_harness_refuses_preexisting_futures_exposure`, `test_executed_stop_is_not_mistaken_for_missing_protection` |
| 8 | **Reports separate exchange results from simulations** | The testnet report's first line states **"ACTUAL EXCHANGE RESPONSES"** or **"MOCK EXCHANGE — NOT EXCHANGE RESULTS"**. A mock run can never overwrite `docs/testnet_report.md`. Execution metrics are labelled EXCHANGE vs SIMULATED. Paper reports say SIMULATED. Trigger tests that did not fire are reported as NOT_TRIGGERED, never as a pass of that trigger | report renderer; `exec_metrics.NATURE` |

Test suite: **110 tests pass, 78 marked safety** (`python -m pytest -q`; the safety subset also runs inside `--check`).

## Exact local setup (your machine)

**Use TESTNET keys only. Never paste keys into chat, commit them, or store production keys anywhere in this setup.**

1. **Create testnet keys.**
   * Spot: https://testnet.binance.vision, log in with GitHub, then **Generate HMAC_SHA256 Key**.
   * Futures: https://testnet.binancefuture.com, log in, then the **API Key** tab at the bottom of the trading page.
   * In the Futures testnet: position mode **One-way**, and **single-asset** margin mode (both are the defaults).
   * Make sure the test symbol (default BTCUSDT) has **no open position or orders** in either test account.
2. **Install.**
   ```bash
   git clone https://github.com/groverstech/Trading-Bot && cd Trading-Bot
   git checkout ccr-2e138369-wawg3y
   cd backend
   python -m venv .venv && source .venv/bin/activate          # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   python -m pytest -q                                        # all must pass
   ```
3. **Configure this shell only.** Prefer reading keys from a password manager over typing them where shell history records them.
   ```bash
   unset GROK_ALLOW_LIVE
   export GROK_PROFILE=testnet
   export GROK_DATA_DIR=./data-testnet                        # dedicated directory, never your paper/live data
   read -rs GROK_TESTNET_SPOT_KEY && export GROK_TESTNET_SPOT_KEY
   read -rs GROK_TESTNET_SPOT_SECRET && export GROK_TESTNET_SPOT_SECRET
   read -rs GROK_TESTNET_FUTURES_KEY && export GROK_TESTNET_FUTURES_KEY
   read -rs GROK_TESTNET_FUTURES_SECRET && export GROK_TESTNET_FUTURES_SECRET
   ```
   (`read -rs` waits silently for one paste followed by Enter.)
   On Windows PowerShell, use `$env:GROK_TESTNET_SPOT_KEY = Read-Host -MaskInput` and so on.
4. **Preflight** (places nothing):
   ```bash
   python -m grokbot.testnet_validate --check
   ```
   It must end with `N/N readiness checks passed` and exit code 0. Any `FAIL` names the fix.
5. **Run the validation:**
   ```bash
   python -m grokbot.testnet_validate --market both --pipeline
   ```
   Defaults:
   * `--trigger-wait 10`: minutes per exchange-triggered SL/TP test;
   * `--wait-minutes 20`: application lifecycle;
   * `--cleanup close_all`;
   * `--symbol BTCUSDT`;
   * `--direction LONG` (Futures pipeline only).

   Expected duration is roughly 1 to 1.5 hours at most; most runs are shorter because exits trigger. You can interrupt with Ctrl-C at any time: the guard still cleans up and writes the report.
6. **Check the ending.** The console must print:
   ```
   Test account(s) queried (futures, spot): 0 open futures position(s), 0 open order(s) incl. algo orders.
   NN steps · 0 critical failures · report: docs/testnet_report.md
   ```
   If it prints `!!! EXPOSURE NOT FULLY CLEANED`, run `python -m grokbot.testnet_validate --cleanup-only`, or close the position on the testnet website.
7. **Send back** `backend/../docs/testnet_report.md` (keys and signatures are already removed). Optionally also send `data-testnet/testnet_report.json` and `data-testnet/testnet_preflight.json`.

## Verified here (cloud environment, no credentials)

* `--check` correctly FAILs on: profile not set (when unset), missing keys, Spot testnet HTTP 451 (region). The Futures testnet checks pass.
* The full keyed path, including interruption cleanup, is exercised only against the mock exchange. **No real testnet order has been placed yet. That is Phase 3, on your machine.**
