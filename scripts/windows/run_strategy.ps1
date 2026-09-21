param(
    [Parameter(Mandatory=$true)][string]$Strategy,
    [Parameter(Mandatory=$true)][string]$Config,
    [string]$ConductorDir = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
)

$ErrorActionPreference = "Stop"
Set-Location $ConductorDir
& uv run conductor run $Strategy --config $Config --trigger "windows_task_scheduler"
exit $LASTEXITCODE
