# Run once (no administrator needed) after `pm2 start ecosystem.config.js` and `pm2 save`.
# Registers a logon task for the current user that runs tools\pm2-resurrect.ps1, restoring the
# saved process list after every Windows restart (with retries; log in data\pm2-startup.log).
# pm2 itself restarts crashed controllers.
[CmdletBinding()]
param([switch]$Uninstall)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$taskName = 'LumiNext-pm2'
if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed $taskName. Running pm2 processes were left unchanged."
    return
}
$pm2 = (Get-Command pm2.cmd -ErrorAction SilentlyContinue).Source
if (-not $pm2) { throw 'pm2 not found. Install Node.js LTS, then run: npm install -g pm2' }
$dump = Join-Path $env:USERPROFILE '.pm2\dump.pm2'
if (-not (Test-Path -LiteralPath $dump)) { throw 'No saved pm2 process list. Run: pm2 start ecosystem.config.js; pm2 save' }
foreach ($legacy in 'LumiNext-edge', 'LumiNext-cloud') {
    if (Get-ScheduledTask -TaskName $legacy -ErrorAction SilentlyContinue) {
        throw "$legacy (install-autostart.ps1) is installed. Remove it first; pm2 and Task Scheduler must not both start LumiNext."
    }
}
$user = "$env:USERDOMAIN\$env:USERNAME"
$wrapper = Join-Path $PSScriptRoot 'tools\pm2-resurrect.ps1'
$arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f $wrapper
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$trigger.Delay = 'PT30S'
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Restores LumiNext pm2 processes after Windows logon.' -Force | Out-Null
Write-Host "Installed $taskName for $user. LumiNext starts about 30 seconds after you sign in."
