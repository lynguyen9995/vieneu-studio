@echo off
rem Start VieNeu Studio hidden at every Windows logon (and start it now).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0autostart\install.ps1"
pause
