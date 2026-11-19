# Shows which Grok Bot variables are set in THIS window - names and lengths only,
# never values.   Run from "backend":   & ..\scripts\windows\show-testnet-config.ps1

$plain = @("GROK_PROFILE", "GROK_DATA_DIR", "GROK_ALLOW_LIVE", "PYTHONUTF8",
           "GROK_SPOT_TESTNET_URL", "GROK_FUTURES_TESTNET_URL", "GROK_SPOT_DATA_URL", "GROK_FUTURES_DATA_URL")
$secret = @("GROK_TESTNET_SPOT_KEY", "GROK_TESTNET_SPOT_SECRET", "GROK_TESTNET_FUTURES_KEY", "GROK_TESTNET_FUTURES_SECRET")

foreach ($n in $plain) {
    $v = [Environment]::GetEnvironmentVariable($n, "Process")
    "{0,-28} {1}" -f $n, $(if ($null -eq $v) { "(not set)" } else { $v })
}
foreach ($n in $secret) {
    $v = [Environment]::GetEnvironmentVariable($n, "Process")
    "{0,-28} {1}" -f $n, $(if ($null -eq $v) { "(not set)" } else { "set, $($v.Length) characters (hidden)" })
}
foreach ($scope in "User", "Machine") {
    foreach ($n in $secret + @("GROK_ALLOW_LIVE")) {
        if ([Environment]::GetEnvironmentVariable($n, $scope)) {
            Write-Host "WARNING: $n is set permanently at $scope level - remove it (System Properties > Environment Variables)." -ForegroundColor Red
        }
    }
}
if ($env:GROK_ALLOW_LIVE -eq "1") { Write-Host "DANGER: GROK_ALLOW_LIVE=1 - run: Remove-Item Env:GROK_ALLOW_LIVE" -ForegroundColor Red }
if ($env:GROK_PROFILE -ne "testnet") { Write-Host "GROK_PROFILE is not 'testnet' - run the set-testnet-keys script first." -ForegroundColor Yellow }
