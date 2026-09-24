@echo off
setlocal
cd /d "%~dp0"

REM Convenience launcher for the two canonical Windows IBKR Nautilus workers.
call "%~dp0start_nautilus_workers.bat"
