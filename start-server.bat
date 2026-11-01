@echo off
setlocal
rem Starts the TTS engine on 127.0.0.1:8000 with the model chosen in engine.json (default: v3 Turbo GPU).
rem Change the model from the studio (tab Model) or edit engine.json.
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "UV_NO_SYNC=1"
set "UV_PYTHON_DOWNLOADS=never"
set "UV_EXE=%USERPROFILE%\.local\bin\uv.exe"
if not exist "%UV_EXE%" (
    echo uv.exe not found at "%UV_EXE%". See README.md.
    exit /b 1
)
echo Wait for application startup to finish. Press Ctrl+C in this window to stop.
"%UV_EXE%" run --no-sync python -m gateway.engine run
set "SERVER_EXIT=%ERRORLEVEL%"
endlocal & exit /b %SERVER_EXIT%
