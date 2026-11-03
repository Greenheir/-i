@echo off
rem ============================================================
rem  Play a harmonica score the easy way.
rem  HOW TO USE: drag a score .txt file onto this file.
rem  (or:  harp-play.cmd "C:\path\to\score.txt")
rem
rem  It will:  dry-run first -> ask you -> play (auto-elevates).
rem  ASCII only on purpose (GBK console).
rem ============================================================
setlocal
set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo [X] venv not found: "%PY%"
    echo     build it:  powershell -ExecutionPolicy Bypass -File "%ROOT%setup-env.ps1"
    pause
    exit /b 1
)

if "%~1"=="" (
    echo.
    echo   Drag a score .txt file onto this file to play it.
    echo   Example score: "%ROOT%lantingxu-dfh.txt"
    echo.
    pause
    exit /b 1
)

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

echo === step 1/2 : dry run (no keys are sent) ===
"%PY%" "%ROOT%harp_dfh.py" "%~1" --dry-run
echo.
choice /c yn /m "Play it for real now? (Esc stops, needs admin - UAC will pop up)"
if errorlevel 2 (
    echo Cancelled.
    pause
    exit /b 0
)

echo === step 2/2 : playing ===
"%PY%" "%ROOT%harp_dfh.py" "%~1"
echo.
pause
