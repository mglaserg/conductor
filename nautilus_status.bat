@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo Conductor Nautilus worker status - ibkr_main
echo ============================================================
uv run conductor worker-status ibkr_main --config conductor.toml
set "MAIN_EXIT=%ERRORLEVEL%"

echo.
echo ============================================================
echo Conductor Nautilus worker status - ibkr_tlaq
echo ============================================================
uv run conductor worker-status ibkr_tlaq --config conductor.toml
set "TLAQ_EXIT=%ERRORLEVEL%"

if not "%MAIN_EXIT%"=="0" exit /b %MAIN_EXIT%
exit /b %TLAQ_EXIT%
