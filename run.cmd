@echo off
rem Start PQ35 Flasher from a checkout after install.ps1 has run.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Run install.ps1 first.
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m pq35ui
