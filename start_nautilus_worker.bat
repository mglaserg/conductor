@echo off
setlocal

REM ============================================================
REM Conductor - Start Nautilus IBKR Worker
REM Place this file in the root of the Conductor repository.
REM ============================================================

cd /d "%~dp0"

title Conductor - Nautilus IBKR Worker

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
echo Route:  windows_ibkr
echo Config: %CD%\conductor.toml
echo ============================================================
echo.

uv run conductor nautilus-worker windows_ibkr --config conductor.toml

set EXITCODE=%ERRORLEVEL%

echo.
echo ============================================================
echo Nautilus worker exited with code %EXITCODE%
echo ============================================================
echo.

if not "%EXITCODE%"=="0" (
    echo Worker stopped unexpectedly.
    pause
)

exit /b %EXITCODE%
