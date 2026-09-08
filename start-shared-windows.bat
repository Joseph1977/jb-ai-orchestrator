@echo off
REM Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
REM SPDX-License-Identifier: Apache-2.0

REM Start the SHARED STACK on Windows/Linux (postgres + litellm + containerized
REM Ollama). Run this FIRST, then start the orchestrator with start-agent-windows.bat.
REM
REM   start-shared-windows.bat            :: up -d (default)
REM   start-shared-windows.bat down       :: stop the shared stack
REM   start-shared-windows.bat logs -f    :: tail logs
REM
REM Uses public images only (no git auth). Reads keys/ports from .env.
REM
REM Do not run this while another Postgres or LiteLLM already owns host ports
REM 5432 / 4000 - point the orchestrator at those instead (--no-shared).
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM Runs before any compose command: the overlays require LITELLM_MASTER_KEY, so
REM even 'down' and 'logs' need .env to exist and carry a key.
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\ensure-compose-env.ps1"
if not %errorlevel%==0 exit /b %errorlevel%

set "COMPOSE=docker compose -f docker-compose.shared.yml -f docker-compose.shared.windows.yml"

set "ARGS=%*"
if "%ARGS%"=="" set "ARGS=up -d"

REM On an 'up', bring up Postgres first and create the litellm admin-UI DB before
REM the rest boots (LiteLLM's prisma migrations need the DB to already exist).
echo %ARGS% | findstr /R /C:"^up" /C:"^start" /C:"^create" >nul
if not %errorlevel%==0 goto :run

set "PGUSER=postgres"
if exist ".env" (
  for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    if /I "%%A"=="POSTGRES_USER" set "PGUSER=%%B"
    if /I "%%A"=="LITELLM_DB" set "LITELLM_DB=%%B"
  )
)
if not defined LITELLM_DB set "LITELLM_DB=litellm"

echo ^>^> %COMPOSE% up -d postgres
%COMPOSE% up -d postgres

echo ^>^> waiting for shared postgres...
for /l %%i in (1,1,30) do (
  docker exec jb-ai-shared-postgres pg_isready -U !PGUSER! >nul 2>&1 && goto :pgready
  timeout /t 1 >nul
)
:pgready
docker exec jb-ai-shared-postgres psql -U !PGUSER! -tAc "SELECT 1 FROM pg_database WHERE datname='!LITELLM_DB!'" 2>nul | findstr "1" >nul
if errorlevel 1 (
  echo ^>^> creating litellm DB '!LITELLM_DB!'
  docker exec jb-ai-shared-postgres psql -U !PGUSER! -c "CREATE DATABASE \"!LITELLM_DB!\";" >nul
) else (
  echo ^>^> litellm DB '!LITELLM_DB!' already exists
)

:run
echo ^>^> %COMPOSE% %ARGS%
%COMPOSE% %ARGS%
endlocal
goto :eof
