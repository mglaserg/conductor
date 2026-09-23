@echo off
setlocal
cd /d "%~dp0"

REM Start the two canonical Windows IBKR account workers in separate consoles.
start "Conductor ibkr_main" cmd /k call "%~dp0start_nautilus_worker.bat" ibkr_main
start "Conductor ibkr_tlaq" cmd /k call "%~dp0start_nautilus_worker.bat" ibkr_tlaq
