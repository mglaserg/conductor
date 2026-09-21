param(
    [Parameter(Mandatory=$true)][string]$Route,
    [Parameter(Mandatory=$true)][string]$Config,
    [string]$TaskName = "Conductor-Nautilus-Worker",
    [string]$ConductorDir = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
)

$ErrorActionPreference = "Stop"
$scriptPath = Join-Path $PSScriptRoot "run_nautilus_worker.ps1"
$actionArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`" -Route `"$Route`" -Config `"$Config`" -ConductorDir `"$ConductorDir`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $actionArgs
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 20 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force
Write-Host "Registered $TaskName at startup"
