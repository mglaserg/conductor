param(
    [Parameter(Mandatory=$true)][string]$Strategy,
    [Parameter(Mandatory=$true)][string]$At,
    [Parameter(Mandatory=$true)][string]$Config,
    [string]$TaskName,
    [string]$ConductorDir = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
)

$ErrorActionPreference = "Stop"
if (-not $TaskName) { $TaskName = "Conductor-$Strategy" }
$scriptPath = Join-Path $PSScriptRoot "run_strategy.ps1"
$actionArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`" -Strategy `"$Strategy`" -Config `"$Config`" -ConductorDir `"$ConductorDir`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $actionArgs
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force
Write-Host "Registered $TaskName at $At"
