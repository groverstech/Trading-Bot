# Binance testnet validation report

Run 2026-11-18 16:59:25 UTC · symbol BTCUSDT · host: vm

Only Binance TEST environments are contacted (testnet.binance.vision, testnet.binancefuture.com). Responses below are the exchange's actual replies (signatures/keys removed).

## Spot — 0 pass · 3 fail · 2 skipped

| # | Step | Result | Note | ms |
|---:|---|:-:|---|---:|
| 1 | connectivity + server time | FAIL |  | 388 |
| 2 | clock sync (offset ms) | FAIL |  | 36 |
| 3 | exchange filters (precision / min size / min notional) | FAIL |  | 38 |
| 4 | market data | SKIPPED | https://testnet.binance.vision not reachable from this environment |  |
| 5 | signed steps | SKIPPED | https://testnet.binance.vision not reachable from this environment |  |

<details><summary>Exchange responses</summary>

**1. connectivity + server time**
```json
{
 "http": 451,
 "code": 0,
 "msg": "Service unavailable from a restricted location according to 'b. Eligibility' in https://www.binance.com/en/terms. Please contact customer service if you believe you received this message in error.",
 "path": "/api/v3/time"
}
```
**2. clock sync (offset ms)**
```json
{
 "http": 451,
 "code": 0,
 "msg": "Service unavailable from a restricted location according to 'b. Eligibility' in https://www.binance.com/en/terms. Please contact customer service if you believe you received this message in error.",
 "path": "/api/v3/time"
}
```
**3. exchange filters (precision / min size / min notional)**
```json
{
 "http": 451,
 "code": 0,
 "msg": "Service unavailable from a restricted location according to 'b. Eligibility' in https://www.binance.com/en/terms. Please contact customer service if you believe you received this message in error.",
 "path": "/api/v3/exchangeInfo"
}
```
</details>

## USDⓈ-M Futures — 5 pass · 0 fail · 15 skipped

| # | Step | Result | Note | ms |
|---:|---|:-:|---|---:|
| 1 | connectivity + server time | PASS | serverTime 1795021163489 | 629 |
| 2 | clock sync (offset ms) | PASS | offset 0 ms | 196 |
| 3 | exchange filters (precision / min size / min notional) | PASS | min notional 50 | 363 |
| 4 | market data (ticker, book, klines, depth) | PASS | bid 62513.70 / ask 62545.20 | 1144 |
| 5 | signed request reaches exchange (dummy key → expected rejection) | PASS | exchange answered -2015 | 696 |
| 6 | account balances | SKIPPED | no testnet credentials (set GROK_TESTNET_FUTURES_KEY / _SECRET) |  |
| 7 | commission rates | SKIPPED | no testnet credentials (set GROK_TESTNET_FUTURES_KEY / _SECRET) |  |
| 8 | limit order + acknowledgement | SKIPPED | no testnet credentials (set GROK_TESTNET_FUTURES_KEY / _SECRET) |  |
| 9 | order status | SKIPPED | no testnet credentials (set GROK_TESTNET_FUTURES_KEY / _SECRET) |  |
| 10 | duplicate client order id rejected | SKIPPED | no testnet credentials (set GROK_TESTNET_FUTURES_KEY / _SECRET) |  |
| 11 | cancel order | SKIPPED | no testnet credentials (set GROK_TESTNET_FUTURES_KEY / _SECRET) |  |
| 12 | market order + fills/commission | SKIPPED | no testnet credentials (set GROK_TESTNET_FUTURES_KEY / _SECRET) |  |
| 13 | partial-fill probe | SKIPPED | no testnet credentials (set GROK_TESTNET_FUTURES_KEY / _SECRET) |  |
| 14 | stop-loss + take-profit placement | SKIPPED | no testnet credentials (set GROK_TESTNET_FUTURES_KEY / _SECRET) |  |
| 15 | protection query / cancel | SKIPPED | no testnet credentials (set GROK_TESTNET_FUTURES_KEY / _SECRET) |  |
| 16 | position reconciliation | SKIPPED | no testnet credentials (set GROK_TESTNET_FUTURES_KEY / _SECRET) |  |
| 17 | trade history | SKIPPED | no testnet credentials (set GROK_TESTNET_FUTURES_KEY / _SECRET) |  |
| 18 | restart recovery by client order id | SKIPPED | no testnet credentials (set GROK_TESTNET_FUTURES_KEY / _SECRET) |  |
| 19 | user-data stream event | SKIPPED | no testnet credentials (set GROK_TESTNET_FUTURES_KEY / _SECRET) |  |
| 20 | flatten / cleanup | SKIPPED | no testnet credentials (set GROK_TESTNET_FUTURES_KEY / _SECRET) |  |

<details><summary>Exchange responses</summary>

**1. connectivity + server time**
```json
{
 "serverTime": 1795021163489
}
```
**2. clock sync (offset ms)**
```json
0
```
**3. exchange filters (precision / min size / min notional)**
```json
{
 "status": "TRADING",
 "tick": "0.10",
 "step": "0.0001",
 "min_qty": "0.0001",
 "min_notional": "50"
}
```
**4. market data (ticker, book, klines, depth)**
```json
{
 "last": "62545.20",
 "bid": "62513.70",
 "ask": "62545.20",
 "mark": "62541.47329710",
 "funding": "0.00010000",
 "klines": 5,
 "depth_levels": [
  20,
  20
 ]
}
```
**5. signed request reaches exchange (dummy key → expected rejection)**
```json
{
 "http": 401,
 "code": -2015,
 "msg": "Invalid API-key, IP, or permissions for action",
 "path": "/fapi/v3/account"
}
```
</details>

## Spot: full application pipeline — 0 pass · 0 fail · 1 skipped

| # | Step | Result | Note | ms |
|---:|---|:-:|---|---:|
| 1 | application pipeline round trip | SKIPPED | no testnet credentials (GROK_TESTNET_SPOT_KEY / _SECRET) |  |

<details><summary>Exchange responses</summary>

</details>

## USDⓈ-M Futures: full application pipeline — 0 pass · 0 fail · 1 skipped

| # | Step | Result | Note | ms |
|---:|---|:-:|---|---:|
| 1 | application pipeline round trip | SKIPPED | no testnet credentials (GROK_TESTNET_FUTURES_KEY / _SECRET) |  |

<details><summary>Exchange responses</summary>

</details>
