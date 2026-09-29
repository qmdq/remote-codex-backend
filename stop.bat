@echo off
setlocal
for /f "delims=" %%i in ('powershell -NoProfile -Command "[IO.Path]::GetTempPath()"') do set "TEMPDIR=%%i"
set "PIDFILE=%TEMPDIR%RemoteCodex\agent.pid"

if not exist "%PIDFILE%" (
    echo [RemoteCodex] Agent is not running.
    pause
    exit /b 0
)

set /p PID=<"%PIDFILE%"
taskkill /PID %PID% /T /F >nul 2>&1
if errorlevel 1 (
    echo [RemoteCodex] Agent process %PID% was not running.
    powershell -NoProfile -Command "[IO.File]::WriteAllText('%PIDFILE%', '0')" >nul 2>&1
) else (
    echo [RemoteCodex] Agent stopped.
    powershell -NoProfile -Command "[IO.File]::WriteAllText('%PIDFILE%', '0')" >nul 2>&1
)
pause
