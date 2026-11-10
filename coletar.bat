@echo off
rem Coleta continua de precos da Steam -> snapshot local + Supabase.
rem Deixe esta janela aberta. Ctrl+C (ou fechar a janela) para parar; o progresso fica salvo.
cd /d "%~dp0"
chcp 65001 >nul
if not exist .venv\Scripts\tradeup.exe (
    echo Rode instalar.bat primeiro.
    pause
    exit /b 1
)
title CS2 Trade-Up - coleta de precos
.venv\Scripts\tradeup.exe collect %*
pause
