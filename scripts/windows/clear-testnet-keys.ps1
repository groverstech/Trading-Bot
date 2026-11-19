# Removes the testnet credentials (and testnet profile) from THIS PowerShell window.
#   Run from "backend":   & ..\scripts\windows\clear-testnet-keys.ps1
# Closing the PowerShell window also removes them.

foreach ($n in "GROK_TESTNET_SPOT_KEY", "GROK_TESTNET_SPOT_SECRET", "GROK_TESTNET_FUTURES_KEY",
               "GROK_TESTNET_FUTURES_SECRET", "GROK_PROFILE", "GROK_ALLOW_LIVE") {
    [Environment]::SetEnvironmentVariable($n, $null, "Process")
}
try { Set-Clipboard -Value " " } catch { }
Write-Host "Testnet credentials removed from this window. (data-testnet keeps only reports; the app vault copy is deleted after each run.)" -ForegroundColor Green
