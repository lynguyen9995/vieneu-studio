@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONDONTWRITEBYTECODE=1"
set "UV_PYTHON_DOWNLOADS=never"
"%USERPROFILE%\.local\bin\uv.exe" run --no-sync python -m gateway.launch
set "LAUNCH_EXIT=%ERRORLEVEL%"
endlocal & exit /b %LAUNCH_EXIT%
