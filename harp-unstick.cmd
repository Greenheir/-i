@echo off
rem ============================================================
rem  UNSTICK: release the left / right / middle mouse buttons.
rem  Use this when the mouse feels "stuck" after a playback.
rem  ASCII only on purpose (GBK console).
rem ============================================================
setlocal
set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%ROOT%harp_dfh.py" --release
"%PY%" "%ROOT%harp2.py" --release
echo.
echo If the mouse is still stuck:
echo   1) click the stuck button once on any window
echo   2) press Ctrl+Alt+Del then Esc
timeout /t 6 >nul
