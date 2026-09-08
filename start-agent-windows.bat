@echo off
REM Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
REM SPDX-License-Identifier: Apache-2.0

REM Start the orchestrator on Windows/Linux, joined to a SHARED stack (postgres +
REM litellm). Infrastructure is never deployed from here.
REM
REM   start-agent-windows.bat                  :: up -d --build (default)
REM   start-agent-windows.bat up -d            :: skip rebuild
REM   start-agent-windows.bat down             :: stop the agent
REM   start-agent-windows.bat logs -f          :: tail logs
REM   start-agent-windows.bat --ensure-shared  :: start the shared stack first if absent
REM   start-agent-windows.bat --no-shared      :: standalone: no overlay, no detection
REM
REM Which infrastructure gets used, in order:
REM   1. --no-shared            -> docker-compose.yml alone; src\.env\docker\.env
REM                                decides where Postgres and LiteLLM live. Use this
REM                                when they are managed elsewhere (cloud, host).
REM   2. jb-ai-shared, if its network and containers are running.
REM   3. otherwise -> refuse with the command to run, unless --ensure-shared was
REM                   passed, in which case jb-ai-shared is started first.
REM
REM Anything left after our own flags is forwarded to `docker compose`.
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM Runs before any compose command: the stack overlay requires
REM LITELLM_MASTER_KEY, so even 'down' and 'logs' need .env to exist.
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\ensure-compose-env.ps1"
if not %errorlevel%==0 exit /b %errorlevel%

set "ENSURE_SHARED=0"
set "USE_SHARED=1"
set "ARGS="
for %%A in (%*) do (
  if /I "%%A"=="--ensure-shared" (
    set "ENSURE_SHARED=1"
  ) else if /I "%%A"=="--no-shared" (
    set "USE_SHARED=0"
  ) else (
    set "ARGS=!ARGS! %%A"
  )
)
if defined ARGS set "ARGS=!ARGS:~1!"
if not defined ARGS set "ARGS=up -d --build"

set "COMPOSE=docker compose -f docker-compose.yml"

if "!USE_SHARED!"=="0" (
  echo ^>^> standalone mode - src\.env\docker\.env decides where postgres + litellm are
  goto :run
)

set "COMPOSE=!COMPOSE! -f docker-compose.stack.yml"

REM Only require a live shared stack when bringing the orchestrator up.
echo !ARGS! | findstr /R /C:"^up" /C:"^start" /C:"^create" >nul
if not %errorlevel%==0 goto :netforother

call :stackup jb-ai-shared-net jb-ai-shared-postgres jb-ai-shared-litellm
if %errorlevel%==0 (
  set "SHARED_NET=jb-ai-shared-net"
  echo ^>^> jb-ai-shared detected - reusing postgres:5432 + litellm:4000 on jb-ai-shared-net
  goto :run
)

if "!ENSURE_SHARED!"=="1" (
  echo ^>^> no shared stack running - starting jb-ai-shared first
  call start-shared-windows.bat up -d
  set "SHARED_NET=jb-ai-shared-net"
  goto :run
)
goto :noshared

:netforother
REM down/logs/ps don't need a live stack, but the overlay still has to resolve its
REM external network, so name it anyway.
set "SHARED_NET=jb-ai-shared-net"

:run
echo ^>^> %COMPOSE% !ARGS!
%COMPOSE% !ARGS!
endlocal
goto :eof

:stackup
docker network inspect %1 >nul 2>&1
if not %errorlevel%==0 exit /b 1
docker ps --filter "name=^%2$" --filter "status=running" -q | findstr . >nul
if not %errorlevel%==0 exit /b 1
docker ps --filter "name=^%3$" --filter "status=running" -q | findstr . >nul
if not %errorlevel%==0 exit /b 1
exit /b 0

:noshared
echo !! No shared stack is running, so there is no postgres or litellm to join.
echo    Start one:        start-shared-windows.bat
echo    Or do it for me:  start-agent-windows.bat --ensure-shared
echo    Or run standalone against externally managed infra:
echo                      start-agent-windows.bat --no-shared
endlocal
exit /b 1
