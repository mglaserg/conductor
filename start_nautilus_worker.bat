@echo off
setlocal

REM ============================================================
REM Conductor - Start one Nautilus IBKR Worker
REM Usage: start_nautilus_worker.bat [route_id]
REM Default route: ibkr_main
REM ============================================================

cd /d "%~dp0"

set "ROUTE=%~1"
if "%ROUTE%"=="" set "ROUTE=ibkr_main"

title Conductor - Nautilus IBKR Worker - %ROUTE%

if not exist "logs" mkdir "logs"

where uv >nul 2>&1
if errorlevel 1 (
    echo.
    echo ERROR: uv was not found on PATH.
    echo Install uv or open a shell where uv is available.
    echo.
    pause
    exit /b 1
)

if not exist "conductor.toml" (
    echo.
    echo ERROR: conductor.toml was not found in:
    echo   %CD%
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo Starting Conductor Nautilus IBKR worker
echo Route:  %ROUTE%
echo Config: %CD%\conductor.toml
echo ============================================================
echo.

uv run conductor nautilus-worker %ROUTE% --config conductor.toml

set EXITCODE=%ERRORLEVEL%

echo.
echo ============================================================
echo Nautilus worker %ROUTE% exited with code %EXITCODE%
echo ============================================================
echo.

if not "%EXITCODE%"=="0" (
    echo Worker stopped unexpectedly.
    pause
)

exit /b %EXITCODE%
