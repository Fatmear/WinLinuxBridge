@echo off
rem ---------------------------------------------------------------------------
rem WinLinuxBridge - zero-install launcher for the control panel.
rem
rem Double-click this file. It needs no installed Python, no pip, no PuTTY:
rem everything it uses ships in this folder (runtime\ and vendor\).
rem
rem   start-gui.cmd             open the panel (no console window)
rem   start-gui.cmd console     same, but keep the console for diagnostics
rem ---------------------------------------------------------------------------
setlocal
set "HERE=%~dp0"
set "GUI=%HERE%WinBridgeGUI.pyw"
set "PYW=%HERE%runtime\pythonw.exe"
set "PY=%HERE%runtime\python.exe"

if not exist "%GUI%" (
  echo [start-gui] WinBridgeGUI.pyw is missing next to this script.
  pause
  exit /b 1
)

if not exist "%PYW%" goto fallback

if /i "%~1"=="console" (
  "%PY%" "%GUI%" %2 %3 %4 %5
  echo.
  echo [start-gui] exit code %ERRORLEVEL%
  pause
  exit /b %ERRORLEVEL%
)

start "" "%PYW%" "%GUI%"
exit /b 0

:fallback
rem No bundled runtime (someone deleted win\runtime): try whatever Python exists.
echo [start-gui] bundled runtime not found, falling back to the system Python.
where pythonw.exe >nul 2>&1
if errorlevel 1 (
  echo [start-gui] No Python found. Rebuild the package with:
  echo             powershell -ExecutionPolicy Bypass -File tools\build-runtime.ps1
  pause
  exit /b 1
)
if /i "%~1"=="console" (
  python "%GUI%" %2 %3 %4 %5
  pause
  exit /b %ERRORLEVEL%
)
start "" pythonw.exe "%GUI%"
exit /b 0
