# Strategy walk-forward report

Generated 2026-11-18 16:51 UTC · runtime 97.0s · symbols BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT, XRPUSDT · source: Binance Spot public klines

Data ranges (UTC): 15m 2025-11-18 → 2026-11-18; 1h 2024-11-18 → 2026-11-18; 4h 2023-11-19 → 2026-11-18

## Method

* Signals use closed candles only; entry at the next bar's open with 3 bps adverse slippage.
* Mirrors the live pipeline: higher-timeframe confirmation (timeframes above the entry timeframe), minimum score 0.55, and the Sizer gate (net reward:risk after fees and slippage ≥ 1.5).
* Stop first when a bar touches both levels; stop fills at the stop or a worse gap-open.
* Rolling walk-forward: parameters are selected on the train window only (max total net R, ≥ 10 trades) and scored on the next unseen test window. Windows (train/test bars): 15m 8640/2880, 1h 4320/1440, 4h 1080/360.
* Cost scenarios: **spot** (0.10% taker/maker, long-only), **futures** (0.05% taker / 0.02% maker, long+short, ≈0.01% funding per 8 h), **nocost** (diagnostic only: zero fees, never promotable).
* Results are in R (1R = planned risk incl. costs). Equity statistics assume 0.5% risk per trade.

**Promotion criteria (fixed before results were generated, all required):** OOS trades ≥ 60, profit factor ≥ 1.2, expectancy ≥ 0.05 R, positive in ≥ 60% of traded folds, positive on ≥ 3 of 5 symbols, max drawdown ≤ 10.0%.

## Verdict

* **No strategy/timeframe/cost combination met the promotion criteria.** All strategies remain available for research but are **disabled for automated testnet/live trading**.

## Out-of-sample results

| Strategy | TF | Scenario | OOS trades | Win % | Breakeven win % | PF | Expectancy R | Net R | Return % | Max DD % | +Folds | +Symbols | Promote |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|:-:|
| breakout | 15m | futures | 714 | 31.0 | 37.2 | 0.76 | -0.167 | -119.5 | -45.8 | 49.1 | 2/9 | 0/5 | ❌ |
| breakout | 15m | nocost | 1880 | 25.5 | 25.9 | 0.98 | -0.016 | -29.4 | -19.2 | 49.0 | 3/9 | 1/5 | ❌ |
| breakout | 15m | spot | 186 | 33.9 | 38.7 | 0.81 | -0.126 | -23.4 | -11.3 | 19.4 | 2/8 | 1/5 | ❌ |
| breakout | 1h | futures | 1025 | 26.0 | 27.3 | 0.93 | -0.048 | -49.3 | -24.4 | 45.8 | 5/9 | 1/5 | ❌ |
| breakout | 1h | nocost | 1237 | 26.4 | 25.5 | 1.04 | 0.033 | 40.2 | 16.8 | 40.4 | 5/9 | 3/5 | ❌ |
| breakout | 1h | spot | 371 | 27.2 | 30.3 | 0.86 | -0.103 | -38.1 | -18.2 | 29.9 | 3/9 | 0/5 | ❌ |
| breakout | 4h | futures | 571 | 31.3 | 30.7 | 1.03 | 0.021 | 12.0 | 4.4 | 20.5 | 7/14 | 3/5 | ❌ |
| breakout | 4h | nocost | 571 | 31.3 | 29.2 | 1.10 | 0.072 | 41.1 | 20.6 | 19.9 | 7/14 | 3/5 | ❌ |
| breakout | 4h | spot | 306 | 30.7 | 33.0 | 0.90 | -0.069 | -21.1 | -10.7 | 22.5 | 6/14 | 1/5 | ❌ |
| momentum | 15m | futures | 985 | 30.2 | 34.0 | 0.84 | -0.113 | -111.4 | -44.0 | 45.4 | 1/9 | 0/5 | ❌ |
| momentum | 15m | nocost | 1896 | 26.9 | 28.6 | 0.92 | -0.060 | -112.9 | -46.4 | 57.0 | 3/9 | 0/5 | ❌ |
| momentum | 15m | spot | 133 | 37.6 | 38.4 | 0.97 | -0.021 | -2.8 | -1.6 | 11.4 | 4/8 | 2/5 | ❌ |
| momentum | 1h | futures | 978 | 30.8 | 31.3 | 0.98 | -0.017 | -16.7 | -10.5 | 26.6 | 4/9 | 2/5 | ❌ |
| momentum | 1h | nocost | 1203 | 29.7 | 28.8 | 1.04 | 0.030 | 36.6 | 15.5 | 23.1 | 6/9 | 3/5 | ❌ |
| momentum | 1h | spot | 339 | 31.9 | 35.6 | 0.85 | -0.105 | -35.7 | -17.0 | 27.8 | 4/9 | 1/5 | ❌ |
| momentum | 4h | futures | 573 | 31.2 | 31.7 | 0.98 | -0.015 | -8.4 | -5.6 | 21.5 | 8/14 | 2/5 | ❌ |
| momentum | 4h | nocost | 667 | 32.2 | 31.7 | 1.02 | 0.015 | 10.2 | 3.3 | 21.6 | 9/14 | 2/5 | ❌ |
| momentum | 4h | spot | 353 | 29.7 | 34.4 | 0.81 | -0.135 | -47.6 | -21.8 | 24.8 | 4/14 | 1/5 | ❌ |
| trend | 15m | futures | 852 | 31.3 | 34.5 | 0.87 | -0.091 | -77.4 | -33.4 | 45.2 | 2/9 | 1/5 | ❌ |
| trend | 15m | nocost | 1146 | 29.1 | 28.9 | 1.01 | 0.009 | 10.9 | 1.8 | 31.9 | 5/9 | 3/5 | ❌ |
| trend | 15m | spot | 144 | 29.2 | 36.8 | 0.71 | -0.206 | -29.7 | -14.0 | 18.9 | 2/8 | 1/5 | ❌ |
| trend | 1h | futures | 635 | 29.8 | 31.1 | 0.94 | -0.043 | -27.5 | -14.4 | 31.7 | 3/9 | 2/5 | ❌ |
| trend | 1h | nocost | 693 | 30.2 | 30.4 | 0.99 | -0.009 | -6.1 | -4.9 | 33.8 | 6/9 | 2/5 | ❌ |
| trend | 1h | spot | 226 | 28.3 | 32.7 | 0.81 | -0.135 | -30.4 | -14.6 | 19.1 | 3/9 | 1/5 | ❌ |
| trend | 4h | futures | 283 | 27.9 | 31.3 | 0.85 | -0.109 | -30.9 | -15.0 | 16.7 | 7/14 | 2/5 | ❌ |
| trend | 4h | nocost | 283 | 27.9 | 30.4 | 0.89 | -0.083 | -23.5 | -11.8 | 16.0 | 6/14 | 2/5 | ❌ |
| trend | 4h | spot | 128 | 28.1 | 31.1 | 0.87 | -0.095 | -12.2 | -6.2 | 12.4 | 4/13 | 1/5 | ❌ |
| volatility | 15m | futures | 533 | 24.8 | 33.9 | 0.64 | -0.269 | -143.2 | -51.7 | 52.6 | 1/8 | 0/5 | ❌ |
| volatility | 15m | nocost | 1156 | 25.8 | 25.9 | 0.99 | -0.004 | -4.9 | -6.3 | 36.8 | 5/9 | 3/5 | ❌ |
| volatility | 15m | spot | 77 | 27.3 | 38.2 | 0.61 | -0.286 | -22.0 | -10.5 | 12.6 | 1/7 | 2/5 | ❌ |
| volatility | 1h | futures | 627 | 28.9 | 28.2 | 1.03 | 0.025 | 15.6 | 5.9 | 29.8 | 5/9 | 3/5 | ❌ |
| volatility | 1h | nocost | 673 | 30.0 | 27.0 | 1.16 | 0.112 | 75.2 | 42.1 | 26.2 | 7/9 | 5/5 | ❌ |
| volatility | 1h | spot | 203 | 29.1 | 32.9 | 0.84 | -0.116 | -23.4 | -11.5 | 14.8 | 4/9 | 1/5 | ❌ |
| volatility | 4h | futures | 339 | 38.6 | 33.7 | 1.24 | 0.147 | 49.7 | 27.0 | 12.2 | 10/14 | 5/5 | ❌ |
| volatility | 4h | nocost | 353 | 37.1 | 32.5 | 1.23 | 0.143 | 50.3 | 27.3 | 16.7 | 9/14 | 5/5 | ❌ |
| volatility | 4h | spot | 178 | 33.1 | 31.1 | 1.10 | 0.066 | 11.8 | 5.5 | 9.7 | 8/14 | 3/5 | ❌ |
| volume | 15m | futures | 549 | 26.8 | 36.2 | 0.64 | -0.261 | -143.2 | -51.7 | 52.6 | 2/9 | 0/5 | ❌ |
| volume | 15m | nocost | 1277 | 29.1 | 32.0 | 0.87 | -0.092 | -117.4 | -46.2 | 57.5 | 3/9 | 1/5 | ❌ |
| volume | 15m | spot | 85 | 34.1 | 38.6 | 0.82 | -0.116 | -9.9 | -5.0 | 7.1 | 2/8 | 1/5 | ❌ |
| volume | 1h | futures | 613 | 24.6 | 29.8 | 0.77 | -0.173 | -106.1 | -42.1 | 44.6 | 2/9 | 0/5 | ❌ |
| volume | 1h | nocost | 668 | 27.2 | 29.4 | 0.90 | -0.075 | -50.0 | -23.6 | 32.6 | 4/9 | 0/5 | ❌ |
| volume | 1h | spot | 135 | 29.6 | 37.1 | 0.71 | -0.201 | -27.1 | -12.9 | 13.7 | 3/9 | 0/5 | ❌ |
| volume | 4h | futures | 267 | 30.0 | 29.6 | 1.02 | 0.013 | 3.6 | 1.0 | 17.6 | 5/14 | 3/5 | ❌ |
| volume | 4h | nocost | 296 | 31.8 | 29.4 | 1.11 | 0.078 | 23.2 | 11.2 | 12.6 | 6/14 | 2/5 | ❌ |
| volume | 4h | spot | 142 | 26.8 | 28.6 | 0.91 | -0.063 | -8.9 | -4.8 | 21.8 | 4/14 | 1/5 | ❌ |

## Cost drag (net R: nocost → futures → spot)

| Strategy | TF | nocost | futures | spot | avg cost per trade (R, spot) |
|---|---|---:|---:|---:|---:|
| momentum | 15m | -112.9 | -111.4 | -2.8 | 0.122 |
| momentum | 1h | 36.6 | -16.7 | -35.7 | 0.102 |
| momentum | 4h | 10.2 | -8.4 | -47.6 | 0.061 |
| breakout | 15m | -29.4 | -119.5 | -23.4 | 0.123 |
| breakout | 1h | 40.2 | -49.3 | -38.1 | 0.126 |
| breakout | 4h | 41.1 | 12.0 | -21.1 | 0.075 |
| trend | 15m | 10.9 | -77.4 | -29.7 | 0.135 |
| trend | 1h | -6.1 | -27.5 | -30.4 | 0.099 |
| trend | 4h | -23.5 | -30.9 | -12.2 | 0.052 |
| volume | 15m | -117.4 | -143.2 | -9.9 | 0.122 |
| volume | 1h | -50.0 | -106.1 | -27.1 | 0.123 |
| volume | 4h | 23.2 | 3.6 | -8.9 | 0.070 |
| volatility | 15m | -4.9 | -143.2 | -22.0 | 0.122 |
| volatility | 1h | 75.2 | 15.6 | -23.4 | 0.114 |
| volatility | 4h | 50.3 | 49.7 | 11.8 | 0.072 |

## Signal funnel (first parameter set, all history, spot scenario)

| Strategy | TF | raw signals | blocked: spot long-only | blocked: score < 0.55 | blocked: net R:R < 1.5 |
|---|---|---:|---:|---:|---:|
| momentum | 15m | 10317 | 5220 | 2030 | 2999 |
| momentum | 1h | 4303 | 2187 | 769 | 806 |
| momentum | 4h | 1337 | 685 | 126 | 15 |
| breakout | 15m | 10066 | 5207 | 1406 | 3427 |
| breakout | 1h | 5112 | 2723 | 756 | 1235 |
| breakout | 4h | 1620 | 899 | 143 | 66 |
| trend | 15m | 9132 | 5081 | 772 | 3211 |
| trend | 1h | 4000 | 2573 | 294 | 749 |
| trend | 4h | 1282 | 945 | 51 | 12 |
| volume | 15m | 4816 | 2527 | 870 | 1400 |
| volume | 1h | 2516 | 1328 | 503 | 445 |
| volume | 4h | 807 | 398 | 148 | 11 |
| volatility | 15m | 3809 | 1977 | 869 | 955 |
| volatility | 1h | 1825 | 921 | 417 | 328 |
| volatility | 4h | 738 | 409 | 121 | 9 |

## Regime breakdown (OOS, futures scenario)

| Strategy | TF | trend_up R (n) | trend_down R (n) | range R (n) | high_vol R (n) | low_vol R (n) | LONG R (n) | SHORT R (n) |
|---|---|---|---|---|---|---|---|---|
| momentum | 15m | -18.7 (125) | -26.3 (141) | -66.5 (719) | -82.0 (628) | -29.4 (357) | -48.8 (419) | -62.6 (566) |
| momentum | 1h | -2.6 (110) | 1.0 (127) | -15.1 (741) | 4.4 (476) | -21.1 (502) | -38.9 (427) | 22.2 (551) |
| momentum | 4h | -8.6 (68) | -1.0 (80) | 1.2 (425) | -43.9 (280) | 35.4 (293) | -24.4 (279) | 16.0 (294) |
| breakout | 15m | -28.2 (209) | -42.7 (238) | -48.6 (267) | -94.1 (598) | -25.5 (116) | -44.8 (379) | -74.7 (335) |
| breakout | 1h | 3.2 (210) | 1.5 (274) | -54.0 (541) | -26.8 (558) | -22.5 (467) | -24.0 (469) | -25.4 (556) |
| breakout | 4h | 37.9 (147) | 7.1 (156) | -33.0 (268) | -24.8 (297) | 36.8 (274) | 22.6 (305) | -10.6 (266) |
| trend | 15m | -80.5 (365) | 6.6 (432) | -3.5 (55) | -58.5 (643) | -19.0 (209) | -66.8 (387) | -10.7 (465) |
| trend | 1h | -58.4 (261) | 41.3 (314) | -10.4 (60) | 1.1 (349) | -28.6 (286) | -39.0 (277) | 11.5 (358) |
| trend | 4h | 2.9 (111) | -37.0 (123) | 3.2 (49) | -37.1 (163) | 6.2 (120) | 2.4 (141) | -33.3 (142) |
| volume | 15m | -42.6 (133) | -30.0 (158) | -70.7 (258) | -101.4 (401) | -41.8 (148) | -73.5 (271) | -69.7 (278) |
| volume | 1h | -16.8 (129) | -20.5 (171) | -68.9 (313) | -40.0 (343) | -66.1 (270) | -66.4 (282) | -39.7 (331) |
| volume | 4h | 13.4 (58) | -2.3 (79) | -7.4 (130) | -5.6 (145) | 9.2 (122) | 11.5 (130) | -7.9 (137) |
| volatility | 15m | -33.0 (71) | -21.6 (92) | -88.6 (370) | -74.9 (235) | -68.2 (298) | -77.6 (269) | -65.6 (264) |
| volatility | 1h | 1.8 (100) | 6.6 (105) | 7.2 (422) | 48.8 (183) | -33.3 (444) | -6.1 (267) | 21.7 (360) |
| volatility | 4h | 5.5 (38) | 18.5 (58) | 25.7 (243) | -3.8 (107) | 53.4 (232) | 21.5 (176) | 28.2 (163) |

## In-sample vs out-of-sample (overfitting check, futures scenario)

| Strategy | TF | best in-sample net R (full history, hindsight) | walk-forward OOS net R |
|---|---|---:|---:|
| momentum | 15m | -130.4 | -111.4 |
| momentum | 1h | -47.0 | -16.7 |
| momentum | 4h | 6.8 | -8.4 |
| breakout | 15m | -111.1 | -119.5 |
| breakout | 1h | 27.7 | -49.3 |
| breakout | 4h | 17.7 | 12.0 |
| trend | 15m | -62.5 | -77.4 |
| trend | 1h | -19.0 | -27.5 |
| trend | 4h | 42.4 | -30.9 |
| volume | 15m | -92.0 | -143.2 |
| volume | 1h | -6.6 | -106.1 |
| volume | 4h | 2.4 | 3.6 |
| volatility | 15m | -93.7 | -143.2 |
| volatility | 1h | 29.0 | 15.6 |
| volatility | 4h | 30.1 | 49.7 |

## Concentration (futures scenario)

| Strategy | TF | OOS net R | best symbol | its net R | net R without it |
|---|---|---:|---|---:|---:|
| momentum | 15m | -111.4 | XRPUSDT | -6.1 | -105.3 |
| momentum | 1h | -16.7 | SOLUSDT | 16.5 | -33.2 |
| momentum | 4h | -8.4 | BTCUSDT | 5.7 | -14.2 |
| breakout | 15m | -119.5 | XRPUSDT | -4.4 | -115.2 |
| breakout | 1h | -49.3 | XRPUSDT | 7.8 | -57.2 |
| breakout | 4h | 12.0 | XRPUSDT | 17.2 | -5.2 |
| trend | 15m | -77.4 | XRPUSDT | 5.5 | -82.9 |
| trend | 1h | -27.5 | XRPUSDT | 7.5 | -35.1 |
| trend | 4h | -30.9 | SOLUSDT | 3.4 | -34.3 |
| volume | 15m | -143.2 | BTCUSDT | -15.5 | -127.7 |
| volume | 1h | -106.1 | ETHUSDT | -12.3 | -93.8 |
| volume | 4h | 3.6 | XRPUSDT | 12.1 | -8.5 |
| volatility | 15m | -143.2 | XRPUSDT | -6.9 | -136.2 |
| volatility | 1h | 15.6 | ETHUSDT | 16.6 | -1.0 |
| volatility | 4h | 49.7 | XRPUSDT | 31.5 | 18.2 |

## Parameter stability (futures scenario): parameter set chosen in each fold

* momentum 15m: 3 distinct sets over 9 folds
* momentum 1h: 5 distinct sets over 9 folds
* momentum 4h: 6 distinct sets over 14 folds
* breakout 15m: 4 distinct sets over 9 folds
* breakout 1h: 2 distinct sets over 9 folds
* breakout 4h: 7 distinct sets over 14 folds
* trend 15m: 3 distinct sets over 9 folds
* trend 1h: 5 distinct sets over 9 folds
* trend 4h: 5 distinct sets over 14 folds
* volume 15m: 3 distinct sets over 9 folds
* volume 1h: 5 distinct sets over 9 folds
* volume 4h: 5 distinct sets over 14 folds
* volatility 15m: 4 distinct sets over 9 folds
* volatility 1h: 4 distinct sets over 9 folds
* volatility 4h: 5 distinct sets over 14 folds

## Per-symbol OOS net R (futures scenario)

| Strategy | TF | BTCUSDT | ETHUSDT | SOLUSDT | BNBUSDT | XRPUSDT |
|---|---|---:|---:|---:|---:|---:|
| momentum | 15m | -23.6 (136) | -18.8 (192) | -32.1 (259) | -30.8 (158) | -6.1 (240) |
| momentum | 1h | -16.4 (175) | -22.9 (203) | 16.5 (205) | -2.4 (189) | 8.5 (206) |
| momentum | 4h | 5.7 (115) | -8.6 (113) | 4.3 (127) | -3.7 (104) | -6.1 (114) |
| breakout | 15m | -27.5 (87) | -35.6 (160) | -43.4 (209) | -8.8 (85) | -4.4 (173) |
| breakout | 1h | -15.6 (205) | -17.6 (206) | -7.5 (199) | -16.5 (214) | 7.8 (201) |
| breakout | 4h | -11.3 (123) | 7.7 (105) | 13.4 (110) | -14.9 (115) | 17.2 (118) |
| trend | 15m | -14.1 (140) | -9.0 (183) | -45.9 (221) | -13.9 (131) | 5.5 (177) |
| trend | 1h | -2.1 (134) | -19.4 (130) | 4.0 (126) | -17.6 (122) | 7.5 (123) |
| trend | 4h | -9.5 (56) | -9.3 (50) | 3.4 (60) | 0.2 (57) | -15.6 (60) |
| volume | 15m | -15.5 (72) | -28.6 (126) | -34.6 (146) | -32.3 (80) | -32.2 (125) |
| volume | 1h | -26.8 (131) | -12.3 (138) | -25.0 (103) | -16.3 (127) | -25.7 (114) |
| volume | 4h | -8.6 (64) | 2.4 (58) | 3.2 (42) | -5.5 (48) | 12.1 (55) |
| volatility | 15m | -45.5 (93) | -46.4 (112) | -35.7 (116) | -8.7 (90) | -6.9 (122) |
| volatility | 1h | -1.8 (117) | 16.6 (128) | -3.9 (132) | 1.7 (130) | 2.9 (120) |
| volatility | 4h | 3.9 (69) | 5.9 (69) | 3.1 (74) | 5.2 (61) | 31.5 (66) |
