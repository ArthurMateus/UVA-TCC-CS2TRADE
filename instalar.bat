@echo off
rem Primeira vez: cria o ambiente Python, instala tudo e baixa o catalogo de skins.
cd /d "%~dp0"
chcp 65001 >nul
if not exist .venv\Scripts\python.exe (
    echo Criando ambiente virtual em .venv ...
    python -m venv .venv || (echo Python nao encontrado. Instale o Python 3.10+ e tente de novo. & pause & exit /b 1)
)
echo Instalando dependencias ...
.venv\Scripts\python.exe -m pip install --upgrade pip -q
.venv\Scripts\python.exe -m pip install -e ".[dev,supabase]" -q || (echo Falha na instalacao. & pause & exit /b 1)
if not exist .env (
    copy .env.example .env >nul
    echo Criado o arquivo .env - abra e preencha a senha do banco em DATABASE_URL.
)
.venv\Scripts\tradeup.exe setup
echo.
echo Pronto. Agora de dois cliques em coletar.bat (deixe rodando) e em abrir_site.bat.
pause
