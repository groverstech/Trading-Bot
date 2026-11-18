# Binance testnet validation: status and local procedure

## Status from the cloud development environment

| Environment | Reachable here | Result |
|---|---|---|
| Spot testnet REST `testnet.binance.vision` | ❌ HTTP **451** (geo-restricted) | Must be run locally |
| Spot testnet streams `stream.testnet.binance.vision` | ✅ | — |
| USDⓈ-M Futures testnet REST `testnet.binancefuture.com` | ✅ | Connectivity, clock sync, filters and market data **PASS**; a signed request with a dummy key returned the exchange's real `-2015` rejection, which proves signing and routing work |
| USDⓈ-M Futures testnet streams `fstream.binancefuture.com` | ✅ | Market streams verified |

**No testnet API keys were available**, so the signed steps (orders, balances, protection, reconciliation) were **not executed against Binance**. They are verified only against the in-repo mock exchange (`tests/test_testnet_harness.py`, `tests/test_limit_orders.py`, `tests/test_exchange_recovery.py`).

The latest harness output is in [`testnet_report.md`](testnet_report.md). It contains real exchange responses for every step that could run.

## What the harness checks (`python -m grokbot.testnet_validate`)

Spot and Futures are tested separately, and only testnet hosts are contacted:

1. Connectivity, server time and clock offset.
2. Exchange filters: tick size, step size, minimum quantity and minimum notional.
3. Market data: ticker, book, klines and depth. For Futures, also mark price and funding.
4. Account balances and commission rates. For Futures, also one-way position mode, ISOLATED margin and 2x leverage.
5. **Limit order + acknowledgement.** A GTC buy is placed 10% below the market, so it rests on the book.
6. **Order status** by client order ID.
7. **Duplicate client order ID is rejected** by the exchange.
8. **Cancellation** through the broker (cancel, then re-query the final state).
9. **Market order** through the broker: fills, average price, commission converted to USDT, and net quantity.
10. **Partial-fill probe.** A marketable limit order larger than the top-of-book size is placed, and the remainder is cancelled.
11. **Stop-loss + take-profit** through the broker: a Spot OCO via `/api/v3/orderList/oco`, or Futures `STOP_MARKET` + `TAKE_PROFIT_MARKET` via `/fapi/v1/algoOrder` with `closePosition`.
12. Protection query: the order list or the open algo orders.
13. **Position reconciliation** through `broker.check_exit`.
14. For Futures: Binance's liquidation price compared with our estimate.
15. Cancel protection, then flatten with a reduce-only market order. For Futures, confirm `positionAmt == 0`.
16. **Trade history**: myTrades or userTrades.
17. **Restart recovery**: a fresh client finds the entry order by its client order ID.
18. For Futures: the **user-data stream** delivers `ORDER_TRADE_UPDATE`.
19. With `--pipeline`: an operator **validation trade** runs through the whole application: Rug Check, Sizer, central Risk, Executor and exchange-side protection, then a manual close and an independent audit of the testnet books. This records a completed testnet round trip, which is one of the live-activation preconditions.

## Local procedure (your machine, India)

Use **test keys only**. Never use production keys for this.

1. Create testnet API keys:
   * Spot: https://testnet.binance.vision (log in with GitHub, then "Generate HMAC_SHA256 Key").
   * USDⓈ-M Futures: https://testnet.binancefuture.com (log in, then the API Key tab at the bottom).
2. Set up the project:
   ```bash
   git clone https://github.com/groverstech/Trading-Bot && cd Trading-Bot
   git checkout ccr-2e138369-wawg3y
   cd backend && python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   python -m pytest -q                                              # all tests must pass
   ```
3. Run the harness. Put the keys in environment variables; don't paste them anywhere else:
   ```bash
   export GROK_TESTNET_SPOT_KEY=...        GROK_TESTNET_SPOT_SECRET=...
   export GROK_TESTNET_FUTURES_KEY=...     GROK_TESTNET_FUTURES_SECRET=...
   export GROK_PROFILE=testnet  GROK_DATA_DIR=./data-testnet
   python -m grokbot.testnet_validate --market both --pipeline
   ```
   The harness writes `docs/testnet_report.md`, which includes every exchange response. It exits non-zero on any critical failure.
4. Optional: run the terminal against the testnet.
   ```bash
   GROK_PROFILE=testnet GROK_DATA_DIR=./data-testnet GROK_ADMIN_PASSWORD='a-long-password' python -m grokbot
   ```
   Then:
   1. Store the testnet keys in **Settings → API credentials** (they are encrypted at rest).
   2. Click **Verify**.
   3. Switch **Mode → testnet**.
   4. Press **Start**.

   With `GROK_PROFILE=testnet`, all market data and streams come from the testnet, so orders are priced from the testnet order books. A risk check blocks testnet orders priced from production data.

   Only **promoted** strategies trade on testnet. None are promoted at present (see `STRATEGY_RESEARCH.md`), so use `--pipeline` above to exercise the full order lifecycle.
5. Send back `docs/testnet_report.md`. Any failure there should be fixed before anything else.

## Notes and known testnet differences

* Testnet liquidity, spreads and prices differ from production. Rug Check may correctly reject testnet books, for example on spread or depth.
* Testnet balances are reset periodically by Binance.
* The Spot testnet has no `/sapi` endpoints, so the withdrawal-permission check only runs for **live** keys.
