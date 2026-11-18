# Binance testnet validation: status and exact local procedure

## 1. Status (honest summary)

| Item | Status |
|---|---|
| Spot testnet `testnet.binance.vision` | ❌ HTTP **451** (geo-restricted) from the cloud development environment. **Run locally.** |
| USDⓈ-M Futures testnet `testnet.binancefuture.com` | ✅ Reachable. Connectivity, clock sync, exchange filters and market data **pass** with real responses. A signed request with a dummy key got the exchange's real `-2015` rejection |
| Signed order flows (both markets) | ⚠️ **Not yet executed on Binance.** No testnet keys were available. The full lifecycle is verified against the in-repo mock exchange: `tests/test_testnet_harness.py`, including exchange-triggered stop-loss (Futures), exchange-triggered take-profit (Spot OCO), and the cleanup path when nothing triggers |
| Paper "validation trades" from Phase 2 | These were **simulated**. They used the Futures testnet's public prices, but **no exchange orders** were involved. See `paper_session_report.md` |

## 2. What `--pipeline` validates (complete lifecycle, per market)

```
1  market data (testnet book; must come from a testnet host)
2  signal (operator VALIDATION trade, labelled, not a strategy signal)
3  Rug Check  ─┐
4  Sizer       ├─ any rejection is reported; nothing is bypassed
5  Central risk┘  (includes data_source_matches_mode, exchange_balance, entry_drift)
6  order submission + exchange acknowledgement + fill (avg price, fee in USDT)
7  exchange-side SL/TP queried FROM THE EXCHANGE and validated
     Spot: OCO order list live with LIMIT_MAKER + STOP_LOSS_LIMIT legs covering the quantity
     Futures: STOP_MARKET + TAKE_PROFIT_MARKET algo orders, closePosition, triggers at the planned levels
8  wait (default 20 min) for the EXCHANGE to trigger SL or TP
9  exit fill vs protective level, in bps (a stop-loss does NOT guarantee its price)
10 reconciliation (system vs exchange) + end-of-test cleanup (default close_all)
11 equity update (new equity snapshots; realized P&L and fees booked)
12 independent audit of the testnet books (recomputed from fills and ledger)
```

The report also contains the adapter-level checks:
* limit order and acknowledgement;
* order status;
* duplicate client-ID rejection;
* cancellation;
* market order with fills and commission;
* partial-fill probe;
* SL/TP placement, query and cancel;
* trade history;
* restart recovery by client order ID;
* Futures user-data stream.

It ends with **"Final test-account state"**, queried directly from the exchange: open Futures positions, open orders and open algo orders.

If neither SL nor TP triggers inside the wait window, that is not a failure. The end-of-test policy then closes the position, and the report says so. Positions are **never** closed while the exchange is unreachable or data is stale; they are reported as kept.

## 3. Exact local procedure (your machine)

Use **testnet keys only**. Never use production keys, and never paste keys into chat or commit them.

**Step 1: get test keys.**
* Spot: log in at https://testnet.binance.vision with GitHub, then **Generate HMAC_SHA256 Key**. Test balances are pre-funded.
* USDⓈ-M Futures: log in at https://testnet.binancefuture.com, then the **API Key** tab at the bottom of the trading page. The test USDT balance is shown there.
* The Futures test account must be in **One-way** position mode (the default). The harness checks this.

**Step 2: install.**
```bash
git clone https://github.com/groverstech/Trading-Bot && cd Trading-Bot
git checkout ccr-2e138369-wawg3y
cd backend
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m pytest -q                                      # expect: all tests pass
```

**Step 3: set the environment for this shell only.**
```bash
export GROK_PROFILE=testnet                 # all market data + streams from the testnets
export GROK_DATA_DIR=./data-testnet         # separate database; never your paper/live data
export GROK_TESTNET_SPOT_KEY=...      GROK_TESTNET_SPOT_SECRET=...
export GROK_TESTNET_FUTURES_KEY=...   GROK_TESTNET_FUTURES_SECRET=...
# Windows PowerShell: $env:GROK_PROFILE="testnet"  etc.
```

**Step 4: preflight** (places nothing).
```bash
python -m grokbot.testnet_validate --check
```
Expected output: every line `OK`, ending with `13/13 prerequisites met`, exit code 0. Fix any `FAIL` line first. A typical failure is `spot testnet reachable: HTTP Error 451`, meaning your network or region is blocked.

**Step 5: run the full validation.**
```bash
python -m grokbot.testnet_validate --market both --pipeline
#   options: --wait-minutes 20       how long to wait for the exchange to fire SL/TP
#            --direction SHORT        futures validation trade direction (spot is always LONG)
#            --cleanup close_all      end-of-test policy: close_all | cancel_pending | report
#            --symbol BTCUSDT
```
* Expected runtime: about 1–2 minutes of adapter checks, plus up to `--wait-minutes` per market for the lifecycle.
* Expected console output: one line per step (`[spot] PASS …`, `[futures-pipeline] PASS 7. exchange-side SL/TP present and valid …`). It ends with:
  ```
  Test account(s) queried (futures, spot): 0 open futures position(s), 0 open order(s) incl. algo orders.
  NN steps · 0 critical failures · report: docs/testnet_report.md
  ```
* Exit code 0 means no critical failure.
* `8. wait …` may say "neither level reached in the window". That is not a failure; step 10 then shows the cleanup.

**Step 6: review and send back** `docs/testnet_report.md` (keys and signatures are removed). Check in particular:
* the step table for each market and pipeline;
* **"Exchange execution metrics"**: opportunities, approvals and rejections with reasons, orders, fills, partial fills, failed orders, SL/TP exits, slippage, fees, realized P&L, remaining positions and orders, audit;
* **"Final test-account state"**: it must state 0 open positions and 0 open orders, or list exactly what remains.

**Step 7 (optional): run the terminal against the testnet.**
```bash
GROK_PROFILE=testnet GROK_DATA_DIR=./data-testnet GROK_ADMIN_PASSWORD='a-long-password' python -m grokbot
```
Then:
1. Store the test keys in **Settings → API credentials** and click **Verify**.
2. Set **Mode → testnet**.

Only *promoted* strategies trade on testnet, and none are promoted today, so the scanner will not place trades. Use step 5 for order lifecycles. Stop the terminal with Ctrl-C and run `python -m grokbot.session_end --mode testnet --policy report` to see the remaining exposure.

## 4. If something is left open

```bash
python -m grokbot.session_end --mode testnet --policy report         # report only
python -m grokbot.session_end --mode testnet --policy close_all      # cancel pending + close (only if exchange reachable)
```
This reconciles the system against the exchange, verifies protective orders, lists the remaining exposure, and ends with an explicit statement of the open positions and orders.

## 5. Known testnet differences

* Testnet books are thinner. Spreads, depth and the mark-vs-last relationship differ from production; Phase 2 observed a lagging testnet book and recurring low wicks. Rug Check may correctly **reject** testnet books on spread or imbalance. That is the system working, not a harness failure.
* Stop-market orders fill at the book after triggering; fills can be worse or better than the stop level. The report shows the difference in bps.
* Testnet balances are reset periodically by Binance. The Spot testnet has no `/sapi` endpoints, so the withdrawal-permission check only applies to **live** keys.
