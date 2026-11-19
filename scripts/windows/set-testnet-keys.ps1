# Grok Bot Terminal - Binance TESTNET credentials for THIS PowerShell window only.
#
#   Run from the "backend" folder:   & ..\scripts\windows\set-testnet-keys.ps1
#   (run with &, do NOT dot-source: the variables are process-wide either way)
#
# * Prompts for the four TESTNET values with hidden input (Read-Host -AsSecureString).
#   Pasted values are never echoed, never written to disk, and never enter
#   PSReadLine history (only typed COMMANDS are recorded, not Read-Host input).
# * Values are stored as process-scope environment variables: they disappear
#   when this PowerShell window closes. Nothing is set at User/Machine level.
# * Also sets the safe testnet profile, removes GROK_ALLOW_LIVE, and clears
#   the clipboard afterwards.
# Works in Windows PowerShell 5.1 and PowerShell 7+.

$ErrorActionPreference = "Stop"

function Read-HiddenValue([string]$Prompt) {
    $secure = Read-Host -Prompt $Prompt -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

Write-Host ""
Write-Host "Binance TESTNET credentials (Spot: testnet.binance.vision, Futures: testnet.binancefuture.com)" -ForegroundColor Cyan
Write-Host "Paste each value and press Enter. Input is hidden. NEVER use production (real-money) keys." -ForegroundColor Cyan
Write-Host ""

$names = @("GROK_TESTNET_SPOT_KEY", "GROK_TESTNET_SPOT_SECRET", "GROK_TESTNET_FUTURES_KEY", "GROK_TESTNET_FUTURES_SECRET")
foreach ($name in $names) {
    while ($true) {
        $value = (Read-HiddenValue "  $name").Trim()
        if ($value -match '^[A-Za-z0-9]{16,256}$') {
            [Environment]::SetEnvironmentVariable($name, $value, "Process")
            Write-Host ("    set ({0} characters, value not shown)" -f $value.Length) -ForegroundColor Green
            break
        }
        Write-Host "    Not a valid Binance HMAC key/secret (expected 16-256 letters/digits). Please paste again." -ForegroundColor Yellow
    }
    $value = $null
}

if ($env:GROK_TESTNET_SPOT_KEY -eq $env:GROK_TESTNET_SPOT_SECRET -or $env:GROK_TESTNET_FUTURES_KEY -eq $env:GROK_TESTNET_FUTURES_SECRET) {
    Write-Host "A key and its secret are identical - something was pasted twice. Re-run this script." -ForegroundColor Red
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $null, "Process") }
    return
}

# Safe profile for this window only.
[Environment]::SetEnvironmentVariable("GROK_ALLOW_LIVE", $null, "Process")
$env:GROK_PROFILE = "testnet"
if (-not $env:GROK_DATA_DIR) { $env:GROK_DATA_DIR = Join-Path (Get-Location) "data-testnet" }
$env:PYTHONUTF8 = "1"

try { Set-Clipboard -Value " " } catch { }   # do not leave the last secret on the clipboard

Write-Host ""
Write-Host "Session configured:" -ForegroundColor Cyan
Write-Host "  GROK_PROFILE   = $env:GROK_PROFILE"
Write-Host "  GROK_DATA_DIR  = $env:GROK_DATA_DIR"
Write-Host "  GROK_ALLOW_LIVE: not set (live trading impossible)"
Write-Host "  4 testnet credential variables set for THIS window only."
Write-Host "Tip: if Windows clipboard history (Win+V) is on, clear it: Settings > System > Clipboard > Clear." -ForegroundColor DarkYellow
