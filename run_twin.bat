@echo off
cd /d "%~dp0"

rem Digital twin of both printers: http://localhost:5053 (reads the Duets and the ESP32s)
rem Standard library only: uses .venv if present, else the system python.
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" twin\twin_server.py
) else (
    python twin\twin_server.py
)

if errorlevel 1 (
    echo The twin server ended with an error.
)
pause
