@echo off
REM Windows launcher — double-click to run
cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo Python 3 is required. Install from https://www.python.org/downloads/
    pause
    exit /b 1
)

python satpp.py
