# Phase 2.1: testnet issue resolution and execution validation

No live trading was activated. Live-authorization requirements and existing safety controls are unchanged. No production credentials were used.

## Correction to the premise

The ETH stop and the open BTC position came from **paper (simulated) validation trades**: they priced against the Binance Futures *testnet's* public feed, but **no exchange orders were placed**. "27/27" was the independent audit of that paper database. The real testnet report (`testnet_report.md`) covers connectivity, market data and signed-request routing only, because no testnet keys exist in this environment. All reports now label results **SIMULATED** or **EXCHANGE**.

## 1. ETH stop-loss investigation

Evidence: real Binance Futures testnet mark-price and contract-price 1-minute klines for 17:14–17:25 UTC, plus the stored order, fill and Rug Check records.

| Question | Finding |
|---|---|
| Mark vs contract price | In the trigger minute (17:23) both moved from about 1757.9 to about 1765.2. At the trigger instant the bot's book snapshot showed mid 1757.57 while the mark had crossed 1762.54: the testnet book lagged the mark by about 0.28%. The testnet contract low was 1751.36 in 8 of 12 consecutive minutes, a recurring wick typical of a thin testnet book |
| Trigger configuration | `MARK_PRICE`, the same as the exchange-side algo orders (`workingType=MARK_PRICE`). The trigger fired correctly |
| Order-book liquidity | At entry: spread 6.8 bps, depth within ±0.5% ≈ 160M USDT notional (testnet), imbalance 1.8 |
| Execution price | Paper filled at 1758.68 by walking the lagging book, **3.86 USDT better than the stop** |
| Implementation defect? | **Yes, in the paper fill model, not in the trigger.** A real stop-market order fills at the prevailing book after triggering; it is never guaranteed, and in a fast move it is usually worse than the stop. Crediting a better fill made paper results optimistic |
| Fix | Conservative paper exits (never better than the level; raw fill retained). Exit forensics on every exit: trigger source and price, mark, last, bid/ask, fill vs level in bps, mark-last divergence. Anomaly alert. Configurable `stop_working_type` (MARK_PRICE or CONTRACT_PRICE) used consistently by paper and exchange. Periodic verification that exchange-side SL/TP orders exist and match the levels, with re-placement and an incident if missing |
| Found while testing the fix | **F13 (high):** an executed exchange stop was mistaken for "missing protection" and re-placed on a flat position. Fixed, with a regression test |

## 2. BTC open position at test end

* **Cause:** the validation tool waited at most 60 minutes and then exited without an end-of-test procedure. The position was left `OPEN` with **no process monitoring it**; paper stops exist only in the running monitor.
* **Resolution:** a new end-of-test procedure, `grokbot.session_end`, applied to both paper databases:
  1. identifies outstanding positions, working entries and pending orders;
  2. reconciles them against the exchange (testnet/live);
  3. verifies protection and reports exposure;
  4. applies the cleanup policy `report | cancel_pending | close_all`, never closing while the exchange is unreachable or data is stale;
  5. re-checks, and states the remaining positions and orders explicitly.
* **Startup downtime replay:** 1-minute candles since the last heartbeat are replayed, and any SL/TP that would have fired is applied.
* **Outcome:**
  * The BTC stop **was touched at 19:01 UTC**, about 37 minutes after the tool exited. The exit is recorded at the stop less slippage.
  * The strategy session's 3 positions did not touch either level in 188 unmonitored minutes and were closed by `close_all`.
  * **Both paper accounts: 0 open positions, 0 pending orders.**

## 3. Testnet validation

* The harness now runs the whole lifecycle: market data → signal → Rug Check → sizing → risk → order → fill → exchange SL/TP verified on the exchange → wait for the **exchange** to trigger SL or TP → exit → reconciliation → equity update → cleanup → audit. It ends with the test account's state queried from the exchange.
* `--check` preflight added.
* Verified here:
  * `--check`: 9/13, with the expected FAILs (no keys, Spot testnet 451).
  * A full run without keys: Futures public steps pass, Spot is blocked (451), and the report says no account was queried and no order placed.
* The keyed path is verified against the mock exchange: Futures exchange-triggered stop, Spot exchange-triggered OCO take-profit, the cleanup path, and flat final state.
* **Blocker: it has not been executed against Binance testnets.** That requires testnet keys on your machine. Exact procedure: `TESTNET.md`.

## 4. Reporting

`grokbot.exec_metrics` is used by both the paper and testnet reports. It reports:
* opportunities, approved and rejected, with rejection reasons;
* orders submitted, successful fills, partial fills, failed and cancelled orders;
* SL and TP exits, exit fill vs level (bps), slippage, fees, realized P&L;
* **remaining open positions and pending orders**, and the audit result.

Each block is labelled with its nature (SIMULATED or EXCHANGE).

## 5. Tests

86 automated tests pass (`python -m pytest -q`), including the new regressions:
* `test_eth_case_mark_triggers_while_book_lags_never_better_than_stop`
* `test_executed_stop_is_not_mistaken_for_missing_protection`
* `test_paper_downtime_replay_applies_stop_touched_while_down`
* `test_close_all_never_closes_blindly_on_stale_data`
* `test_futures_lifecycle_exchange_stop_reconciled_and_flat`
* `test_spot_lifecycle_take_profit_on_exchange`

## Outstanding blockers

1. **Binance testnet keys**: needed for any real exchange order validation (Spot and Futures). Run `TESTNET.md` §3 locally.
2. **Spot testnet is geo-blocked** (HTTP 451) from the cloud environment; it can only be validated locally.
3. A **take-profit exit has not yet been observed on a real exchange or on live prices**. It is covered by tests (mock exchange and paper).
4. No strategy is promoted for automated testnet/live trading (Phase 2 research); unchanged.
