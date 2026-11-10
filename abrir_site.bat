@echo off
rem Abre a interface web em http://127.0.0.1:8000
cd /d "%~dp0"
chcp 65001 >nul
if not exist .venv\Scripts\tradeup.exe (
    echo Rode instalar.bat primeiro.
    pause
    exit /b 1
)
title CS2 Trade-Up - site
start "" http://127.0.0.1:8000
.venv\Scripts\tradeup.exe serve
pause
