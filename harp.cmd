@echo off
rem ============================================================
rem  harmonica toolchain launcher  --  ASCII only on purpose
rem  (non-ASCII bytes in a .cmd get mangled by the GBK console)
rem
rem  usage:
rem     harp.cmd env                     check the environment
rem     harp.cmd all   <song.mp3> [key]   split -> midi -> score
rem     harp.cmd split <song.mp3>         step 1  (demucs vocals)
rem     harp.cmd midi  <vocals.wav>       step 2  (basic-pitch)
rem     harp.cmd score <x.mid> [key]      step 3  (midi2harp)
rem     harp.cmd play  [score.txt] [--dry-run]   step 4 (autoplay, Esc = stop)
rem     harp.cmd test                    dry-run the built-in sample
rem ============================================================
setlocal
set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo [X] venv not found: "%PY%"
    echo     build it first:  powershell -ExecutionPolicy Bypass -File "%ROOT%setup-env.ps1"
    exit /b 1
)

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "TF_CPP_MIN_LOG_LEVEL=3"
rem demucs tries HuggingFace first and only falls back to AWS on error;
rem huggingface.co times out here, so force offline unless HARP_USE_HF=1
if not "%HARP_USE_HF%"=="1" set "HF_HUB_OFFLINE=1"
set "HF_HUB_DISABLE_TELEMETRY=1"

"%PY%" "%ROOT%harp_cli.py" %*
exit /b %ERRORLEVEL%
