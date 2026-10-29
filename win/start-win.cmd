@echo off
rem ---------------------------------------------------------------------------
rem WinLinuxBridge - Windows entry point.
rem
rem   start-win.cmd            run the supervisor hidden (agent + tunnel + sync)
rem   start-win.cmd console    run it in this window so you can see the log
rem
rem No administrator rights are required or requested.
rem ---------------------------------------------------------------------------
setlocal
set "HERE=%~dp0"
set "LOGDIR=%LOCALAPPDATA%\WinLinuxBridge\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>&1
set "LOG=%LOGDIR%\launcher.log"

set "PS=powershell.exe"
where pwsh.exe >nul 2>&1 && set "PS=pwsh.exe"

if /i "%~1"=="console" goto console

start "" /min "%PS%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%HERE%winbridge_supervisor.ps1" 2>>"%LOG%"
echo [start-win] supervisor launched in the background. Logs: "%LOGDIR%"
exit /b 0

:console
"%PS%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%HERE%winbridge_supervisor.ps1" %2 %3 %4 %5
exit /b %ERRORLEVEL%
