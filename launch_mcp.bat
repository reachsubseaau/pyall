@echo off
rem ==========================================================================
rem  launch_mcp.bat - start the pyall MCP server over HTTP on this machine/VM
rem                   and the status/log monitor web page (opened in a browser)
rem
rem  Edit the settings below, then double-click this file (or run it from a
rem  command prompt).  Any arguments you pass to this script are forwarded to
rem  pyall_mcp.py, so you can override the defaults, e.g.:
rem
rem      launch_mcp.bat --port 9000
rem      launch_mcp.bat --transport stdio
rem ==========================================================================

setlocal

rem -- folder this .bat lives in (with trailing backslash) --
set "ROOT=%~dp0"

rem -- editable settings ------------------------------------------------------
rem  HOST    : 0.0.0.0 accepts connections from other machines on the network.
rem            Use 127.0.0.1 to only allow this machine.
rem  PORT    : TCP port the MCP HTTP server listens on.
rem  MONPORT : TCP port the monitor web page is served on.
rem  DATA    : folder the server is allowed to read/write (file access is
rem            confined to this folder).  Change it to your survey data folder.
set "HOST=127.0.0.1"
set "PORT=8000"
set "MONPORT=8770"
set "DATA=%ROOT%sample"
set "LOGDIR=%ROOT%logs"
rem --------------------------------------------------------------------------

set "PYTHON=%ROOT%.venv\Scripts\python.exe"

rem  shared rotating log folder used by both the MCP server and the monitor
set "PYALL_LOG_DIR=%LOGDIR%"

rem  URL the monitor page is reached on (loopback even when bound to all NICs).
set "MONHOST=%HOST%"
if "%HOST%"=="0.0.0.0" set "MONHOST=127.0.0.1"
if "%HOST%"=="::" set "MONHOST=127.0.0.1"
set "MONURL=http://%MONHOST%:%MONPORT%/"

if not exist "%PYTHON%" (
    echo ERROR: Python venv not found at "%PYTHON%".
    echo Create it first:  python -m venv .venv  ^&^&  .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

echo Starting pyall monitor on %MONURL%
rem  start the monitor in its own window so it runs alongside the MCP server
start "pyall monitor" "%PYTHON%" "%ROOT%monitor.py" --host %HOST% --port %MONPORT% --dir "%LOGDIR%"

rem  give the monitor a moment to bind, then open the page in the default browser
timeout /t 1 /nobreak >nul
start "" "%MONURL%"

echo Starting pyall MCP server (HTTP) on %HOST%:%PORT%
echo File access confined to: %DATA%
echo Endpoint: http://%HOST%:%PORT%/mcp
echo Monitor:  %MONURL%
echo Log file: %LOGDIR%\pyall.log
echo Press Ctrl+C to stop.
echo.

"%PYTHON%" "%ROOT%pyall_mcp.py" --http --host %HOST% --port %PORT% --root "%DATA%" %*

endlocal
