@echo off
setlocal
chcp 65001 >nul
title SA Agent Service

set "SERVICE_DIR=%~dp0"
set "PROJECT_DIR=%SERVICE_DIR%..\"
set "PY=%PROJECT_DIR%.venv\Scripts\python.exe"
set "SERVER=%SERVICE_DIR%server.py"

if not exist "%PY%" (
    echo [ERROR] Python virtual environment was not found:
    echo         %PY%
    echo Create it from the sa-agent directory with:
    echo         python -m venv .venv
    echo Then install dependencies with:
    echo         .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

if not exist "%SERVER%" (
    echo [ERROR] Server script was not found:
    echo         %SERVER%
    pause
    exit /b 1
)

cd /d "%SERVICE_DIR%"
"%PY%" "%SERVER%"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] SA Agent exited with code %EXIT_CODE%.
)
pause
exit /b %EXIT_CODE%
