@echo off
rem ============================================================
rem  harmonica GUI launcher  --  ASCII only on purpose
rem  (pythonw = no console window)
rem ============================================================
setlocal
set "ROOT=%~dp0"
set "PYW=%ROOT%.venv\Scripts\pythonw.exe"
set "PY=%ROOT%.venv\Scripts\python.exe"

if not exist "%PYW%" (
    echo [X] venv not found: "%PYW%"
    echo     build it first:  powershell -ExecutionPolicy Bypass -File "%ROOT%setup-env.ps1"
    pause
    exit /b 1
)

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "TF_CPP_MIN_LOG_LEVEL=3"

start "" "%PYW%" "%ROOT%harp_gui.py"
exit /b 0
