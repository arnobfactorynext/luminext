# Run by the LumiNext-pm2 logon task (see install-pm2-startup.ps1). `pm2 resurrect` alone can
# hang on Windows when the PC is busy after sign-in: the CLI times out waiting for its new
# daemon, spawns a second one, and the two deadlock on the pm2 named pipe. This wrapper
# verifies the controller answers /healthz and, if not, kills stray pm2 processes and retries.
param([int]$Attempts = 5, [string]$HealthUrl = 'http://127.0.0.1:8000/healthz')
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$logPath = Join-Path $root 'data\pm2-startup.log'
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
$pm2 = Join-Path $env:APPDATA 'npm\pm2.cmd'

function Log($message) {
    if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -gt 1MB) { Clear-Content -LiteralPath $logPath }
    Add-Content -LiteralPath $logPath -Value ('{0:yyyy-MM-dd HH:mm:ss} {1}' -f (Get-Date), $message)
}

function Test-Healthy {
    try { (Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 5).status -eq 'ok' } catch { $false }
}

function Stop-Pm2 {
    # Only pm2's own node processes; the LumiNext python tree is killed with its daemon.
    Get-CimInstance Win32_Process -Filter "Name='node.exe'" | Where-Object { $_.CommandLine -match '\\pm2\\(bin\\pm2|lib\\Daemon\.js)' } | ForEach-Object {
        & taskkill.exe /PID $_.ProcessId /T /F 2>$null | Out-Null
    }
    Start-Sleep -Seconds 3
}

Log 'Logon startup: restoring pm2 processes'
for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
    if (Test-Healthy) { Log "Healthy before attempt $attempt; nothing to do"; exit 0 }
    Stop-Pm2  # start from exactly one daemon: clear any half-started or duplicate ones
    $process = Start-Process -FilePath $pm2 -ArgumentList 'resurrect' -WindowStyle Hidden -PassThru
    if (-not $process.WaitForExit(90000)) { Log "Attempt ${attempt}: pm2 resurrect still running after 90s" }
    for ($wait = 0; $wait -lt 12; $wait++) {
        Start-Sleep -Seconds 5
        if (Test-Healthy) { Log "Attempt ${attempt}: controller healthy"; exit 0 }
    }
    Log "Attempt ${attempt}: controller not healthy after 60s; retrying"
}
Log "Gave up after $Attempts attempts; run 'pm2 resurrect' manually and check pm2 logs"
exit 1
