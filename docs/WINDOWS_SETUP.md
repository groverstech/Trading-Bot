# Phase 3 on Windows: setup, preflight and testnet execution (PowerShell)

Every command works in **Windows PowerShell 5.1** (built into Windows) and in **PowerShell 7**. Run them in one PowerShell window, from start to finish. **Never paste API keys or secrets at the `PS>` prompt, into chat, or into any file.** They are entered only through the hidden prompt in step B2.

## A. One-time installation

**A1. Prerequisites** (once):
* **Git for Windows**: https://git-scm.com/download/win. Defaults are fine; Git Credential Manager handles GitHub sign-in.
* **Python 3.12 or 3.13 (64-bit)**: https://www.python.org/downloads/windows/. In the installer, tick **"Add python.exe to PATH"** and keep the **py launcher**.
* An accurate clock: Settings → Time & language → Date & time → **Sync now**. The preflight requires an offset under 1 second.

**A2. Clone, create the virtual environment, install, test:**
```powershell
cd $HOME
git clone https://github.com/groverstech/Trading-Bot.git
cd Trading-Bot
git checkout ccr-2e138369-wawg3y
git pull
cd backend

py -3 --version                                   # must print Python 3.12.x or 3.13.x
py -3 -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force   # this window only; needed for .ps1 files
.\.venv\Scripts\Activate.ps1                      # prompt now starts with (.venv)
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

$env:PYTHONUTF8 = "1"
python -m pytest -q                               # expect: "108 passed" (takes ~3 minutes)
```
If the repository is private, `git clone` opens a GitHub sign-in window. That is Git's own credential manager; it has nothing to do with Binance keys.

## B. Every session: configure, verify, preflight

Open a **new** PowerShell window for each session. Then:

**B1. Enter the project and the virtual environment:**
```powershell
cd $HOME\Trading-Bot\backend
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
.\.venv\Scripts\Activate.ps1
```

**B2. Enter the TESTNET credentials (hidden input):**
```powershell
& ..\scripts\windows\set-testnet-keys.ps1
```
* It prompts four times: `GROK_TESTNET_SPOT_KEY`, `GROK_TESTNET_SPOT_SECRET`, `GROK_TESTNET_FUTURES_KEY`, `GROK_TESTNET_FUTURES_SECRET`.
* Paste each value and press **Enter**. Nothing is shown; it only confirms "set (64 characters, value not shown)".
* Values are stored **only in this window's process environment**. They are not written to disk, not added to PSReadLine history (only the command line above is recorded), and not committed.
* The script also sets `GROK_PROFILE=testnet`, `GROK_DATA_DIR=...\backend\data-testnet` and `PYTHONUTF8=1`, removes `GROK_ALLOW_LIVE`, and clears the clipboard.
* If Windows clipboard history (Win+V) is enabled, clear it afterwards: Settings → System → Clipboard → **Clear**.

**B3. Verify the session configuration** (names and lengths only, never values):
```powershell
& ..\scripts\windows\show-testnet-config.ps1
```
Expected:
* `GROK_PROFILE testnet`;
* `GROK_ALLOW_LIVE (not set)`;
* the four credential variables shown as `set, NN characters (hidden)`;
* no red WARNING lines. A red line means a key or `GROK_ALLOW_LIVE` is set permanently at User or Machine level; remove it in System Properties → Environment Variables.

**B4. Preflight (READ-ONLY: places no orders):**
```powershell
python -m grokbot.testnet_validate --check
```
It takes 1–2 minutes, because it also runs the 76-test safety suite in a clean environment that never sees your keys. Every line must read `OK` (or `INFO`), ending with `N/N readiness checks passed`:

| Group | Checks |
|---|---|
| `[environment]` | Python version, required modules |
| `[safety]` | `GROK_PROFILE=testnet`; `GROK_ALLOW_LIVE` not set; **all endpoints validated as TESTNET hosts** (shows the spot/futures trading and data URLs); **initial capital ₹50,000**; **risk limits not looser than defaults**; **no live credentials** in the data directory; no leftover exposure marker; safety test suite passed |
| `[spot]` / `[futures]` | testnet reachable and clock offset < 1 s; BTCUSDT trading; **credentials accepted by testnet** (signed read-only call); trading permission; test balance sufficient (Spot ≥ 50 USDT, Futures ≥ 200 USDT); Futures **One-way** mode and single-asset margin; **no pre-existing BTCUSDT position or orders**; withdraw/transfer capability reported |

Fix every `FAIL` before going further:
* `-2015` / "credentials accepted … FAIL": a wrong key, a key pasted into the wrong prompt, or a production key. Production keys are rejected by the testnet.
* "no pre-existing BTCUSDT position / orders" FAIL: close it on the testnet website, or use `--symbol ETHUSDT`.
* "clock offset" FAIL: use **Sync now** in Windows time settings.
* "testnet reachable … 451": your network or region is blocked.

**STOP HERE and report the preflight result before running anything that places orders.**

## C. Commands that may place TESTNET orders

| Command | Places testnet orders? | Purpose |
|---|---|---|
| `python -m pytest -q` | **No** (mock exchange only) | automated tests |
| `python -m grokbot.testnet_validate --check` | **No** (read-only) | preflight |
| `python -m grokbot.testnet_validate --market both --pipeline` | **YES**: Spot and Futures testnet, minimum sizes | the Phase 3 validation |
| `python -m grokbot.testnet_validate --market spot` (or `futures`) | **YES**: one market, without the app pipeline | partial re-runs |
| `python -m grokbot.testnet_validate --cleanup-only` | **YES**, but only cancels and flattens what an earlier run created | recovery after an interruption |
| `python -m grokbot.session_end --mode testnet --policy close_all` | **YES** (cancel and close) | manual end-of-test cleanup of the app's testnet positions |
| `python -m grokbot.session_end --mode testnet --policy report` | **No** | exposure report |
| `python -m grokbot` (terminal UI) | Do **not** run it during Phase 3 | no strategy is promoted, so it would not trade, but keep Phase 3 to the harness |

Everything that places orders is hard-wired to `testnet.binance.vision` and `testnet.binancefuture.com`. Production hosts are refused before any request is sent.

## D. Running the validation (after the preflight passes)

```powershell
python -m grokbot.testnet_validate --market both --pipeline
```

**What it does, per market (Spot first, then Futures):**
1. Read-only checks: connectivity, clock, filters, market data, balances, permissions, account mode.
2. Order lifecycle at minimum size:
   * a limit order 10% away from the price (acknowledged, queried, duplicate client ID rejected, cancelled);
   * a partial-fill probe that flattens itself;
   * a market order with stop-loss/take-profit **placed in the same step**, verified on the exchange, then a planned exit.
3. An **exchange-triggered TAKE-PROFIT test** and an **exchange-triggered STOP-LOSS test** (up to `--trigger-wait` minutes each).
4. `--pipeline`: the full application lifecycle (Rug Check → Sizer → Risk → order → fill → exchange SL/TP → wait for the exchange to trigger → reconciliation → equity → cleanup → audit).
5. The **end-of-run guard**, always: it cancels the run's orders and flattens only exposure the run created. Spot BTC you held before the run is never sold.

**Expected duration:** typically **30–60 minutes**. The worst case is about **1.5 hours**: per market, about 3 minutes of checks, two trigger tests of up to 10 minutes each, and the pipeline's wait of up to 20 minutes. To shorten:
```powershell
python -m grokbot.testnet_validate --market both --pipeline --trigger-wait 5 --wait-minutes 10
```
You may press **Ctrl-C at any time**. The guard still cleans up and writes the report. Do not close the window while it prints `running the end-of-run guard`.

## E. Verifying that everything was cleaned up

1. **Console ending.** The last lines must be:
   ```
   Test account(s) queried (futures, spot): 0 open futures position(s), 0 open order(s) incl. algo orders.
   NN steps · 0 critical failures · report: docs/testnet_report.md
   ```
   If you see `!!! EXPOSURE NOT FULLY CLEANED`, run `python -m grokbot.testnet_validate --cleanup-only`, then repeat this check.
2. **Report:** open `..\docs\testnet_report.md`.
   * The first line must read **"NATURE OF RESULTS: ACTUAL EXCHANGE RESPONSES"**.
   * The section **"End-of-run guard"** must show `CLEAN` for both markets.
   * The section **"Final test-account state"** must list no positions and no orders.
3. **Independent re-check, read-only:** run `python -m grokbot.testnet_validate --check` again. Both "no pre-existing BTCUSDT position / orders" lines and "no leftover exposure" must be `OK`.
4. **No leftover marker:** `Test-Path $env:GROK_DATA_DIR\LEFTOVER_EXPOSURE.json` must print `False`.
5. **Optional visual check:** on https://testnet.binancefuture.com, the **Positions** and **Open Orders** tabs (including conditional orders) for BTCUSDT must be empty. The Spot testnet has no trading website; steps 1–3 cover it.
6. **Remove the credentials from the window:**
   ```powershell
   & ..\scripts\windows\clear-testnet-keys.ps1
   ```
   Then close the window. The app's encrypted vault copy is deleted automatically at the end of each run.

## F. What to send back

* The full console output of the preflight (it contains no secrets).
* `docs\testnet_report.md`. Signatures and keys are removed; it never contains your key or secret. Optionally also `backend\data-testnet\testnet_report.json`.

Do **not** send API keys or secrets anywhere, including this conversation. `data-testnet\` is in `.gitignore` and must never be committed.
