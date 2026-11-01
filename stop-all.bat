@echo off
rem Stops the gateway (8001) and the engine (8000), whoever started them.
cd /d "%~dp0"
"%USERPROFILE%\.local\bin\uv.exe" run --no-sync python -m gateway.engine stop
