@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo .venv not found in this folder.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat
python alarm.py

if errorlevel 1 (
    echo The script ended with an error.
    pause
    exit /b 1
)

pause