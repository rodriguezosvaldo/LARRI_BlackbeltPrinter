@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo .venv not found in this folder.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat

rem Spool monitor (load cells, http://localhost:5051) in its own window
start "Spool monitor" cmd /k ""%~dp0.venv\Scripts\python.exe" "%~dp0load cel\spool_monitor.py""

rem Alarm (printer polling + ntfy alerts) in this window
python alarm.py

if errorlevel 1 (
    echo The script ended with an error.
    pause
    exit /b 1
)

pause