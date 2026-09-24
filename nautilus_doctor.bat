@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo Conductor Nautilus doctor
echo ============================================================
uv run conductor doctor --config conductor.toml
exit /b %ERRORLEVEL%
