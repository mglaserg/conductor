@echo off
setlocal
cd /d "%~dp0"

REM Start the two canonical Windows IBKR account workers in separate consoles.
start "Conductor Doctor" cmd /k call uv run conductor doctor --config conductor.toml