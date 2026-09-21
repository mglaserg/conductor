param(
    [Parameter(Mandatory=$true)][string]$Route,
    [Parameter(Mandatory=$true)][string]$Config,
    [string]$ConductorDir = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
)

$ErrorActionPreference = "Stop"
Set-Location $ConductorDir
& uv run conductor nautilus-worker $Route --config $Config
exit $LASTEXITCODE
