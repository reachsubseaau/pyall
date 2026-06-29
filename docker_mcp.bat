@echo off
rem ==========================================================================
rem  docker_mcp.bat - build and run the qc.all MCP server Docker container
rem
rem  Usage:
rem      docker_mcp.bat                build the image, then run it
rem      docker_mcp.bat build          build the image only
rem      docker_mcp.bat run            run the existing image only (no rebuild)
rem      docker_mcp.bat stop           stop and remove the running container
rem
rem  Any extra arguments after the action are forwarded to the server, e.g.:
rem      docker_mcp.bat run --port 9000
rem ==========================================================================

setlocal enabledelayedexpansion

rem -- folder this .bat lives in (with trailing backslash) --
set "ROOT=%~dp0"

rem -- editable settings ------------------------------------------------------
rem  IMAGE : the Docker image tag to build / run.
rem  NAME  : the running container name.
rem  PORT  : host port published to the container's port 8000.
rem  DATA  : host folder mounted at /data (the server is confined to this).
rem          Change it to your survey data folder. Append :ro to mount read-only.
set "IMAGE=qcall-mcp"
set "NAME=qcall-mcp"
set "PORT=8000"
set "DATA=%ROOT%sample"
rem --------------------------------------------------------------------------

rem -- check Docker is available.  If it is not on PATH (common in a shell opened
rem    before Docker Desktop was installed), fall back to the default install
rem    location and add it to PATH for this session. --
where docker >nul 2>&1
if errorlevel 1 (
    set "DOCKERBIN=%ProgramFiles%\Docker\Docker\resources\bin"
    if exist "!DOCKERBIN!\docker.exe" set "PATH=!DOCKERBIN!;%PATH%"
)
where docker >nul 2>&1
if errorlevel 1 (
    echo ERROR: 'docker' was not found on the PATH.
    echo Install Docker Desktop ^(Windows^) or Docker Engine ^(Linux^) and try again.
    echo If Docker Desktop IS installed, open a NEW terminal, or add this folder to PATH:
    echo     "%ProgramFiles%\Docker\Docker\resources\bin"
    exit /b 1
)

rem -- pick the action (default = build then run) --
set "ACTION=%~1"
if "%ACTION%"=="" set "ACTION=all"

rem -- strip the action from the forwarded arguments --
set "EXTRA="
if not "%~1"=="" shift & goto collect
:collect
if not "%~1"=="" (
    set "EXTRA=!EXTRA! %~1"
    shift
    goto collect
)

if /i "%ACTION%"=="build" goto build
if /i "%ACTION%"=="run"   goto run
if /i "%ACTION%"=="stop"  goto stop
if /i "%ACTION%"=="all"   goto build

echo Unknown action "%ACTION%". Use: build ^| run ^| stop ^| (no argument to build+run).
exit /b 1

:build
echo === Building image "%IMAGE%" ===
docker build -t "%IMAGE%" "%ROOT%."
if errorlevel 1 (
    echo Build failed.
    exit /b 1
)
if /i "%ACTION%"=="build" (
    echo Build complete.
    exit /b 0
)

:run
echo === Removing any existing container "%NAME%" ===
docker rm -f "%NAME%" >nul 2>&1

echo === Running "%IMAGE%" as "%NAME%" on port %PORT% ===
echo Data folder mounted at /data: %DATA%
echo MCP endpoint: http://localhost:%PORT%/mcp
echo Use "docker logs -f %NAME%" to watch output, or "docker_mcp.bat stop" to stop.
echo.
docker run -d --name "%NAME%" -p %PORT%:8000 -v "%DATA%:/data" "%IMAGE%"%EXTRA%
if errorlevel 1 (
    echo Failed to start container.
    exit /b 1
)
docker ps --filter "name=%NAME%"
exit /b 0

:stop
echo === Stopping and removing container "%NAME%" ===
docker rm -f "%NAME%"
exit /b 0
