# Run once from an Administrator PowerShell window.
[CmdletBinding()]
param([switch]$IncludeCloud)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Open PowerShell using Run as administrator, then run this installer again.'
}
$root = $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw "Python environment missing: $python" }
$modes = @('edge')
if ($IncludeCloud) { $modes += 'cloud' }
# Check all requested ports before installing anything; never start a second controller.
foreach ($mode in $modes) {
    $port = if ($mode -eq 'edge') { 8000 } else { 8001 }
    $taskName = "LumiNext-$mode"
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ((Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) -and (!$task -or $task.State -ne 'Running')) {
        throw "Port $port is already in use. Stop the manually started $mode controller before installing."
    }
}
foreach ($mode in $modes) {
    $port = if ($mode -eq 'edge') { 8000 } else { 8001 }
    $dataPath = if ($mode -eq 'edge') { Join-Path $root 'data' } else { Join-Path $root 'data\cloud' }
    $taskName = "LumiNext-$mode"
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existing -and $existing.State -eq 'Running') {
        Write-Host "$taskName is already running; leaving it unchanged."
        continue
    }
    $arguments = '-m luminext --mode {0} --host 127.0.0.1 --port {1} --data "{2}"' -f $mode, $port, $dataPath
    $action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $trigger.Delay = 'PT30S'
    $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $account = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $account -Description 'LumiNext background controller; starts at boot and retries after failures.' -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName
    Write-Host "Installed and started $taskName on http://127.0.0.1:$port"
}
Write-Host 'Check /healthz in your browser after startup. Keep this project and its data at the same path.'
