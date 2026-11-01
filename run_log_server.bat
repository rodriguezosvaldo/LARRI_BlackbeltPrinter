@echo off
cd /d "%~dp0"

rem Log receiver for the printer ESP32s (job logs, replies, load-cell CSVs in logs\)
rem Standard library only: uses .venv if present, else the system python.
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" log_server.py
) else (
    python log_server.py
)

if errorlevel 1 (
    echo The log server ended with an error.
)
pause
