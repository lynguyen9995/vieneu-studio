@echo off
rem Stop starting VieNeu Studio at logon.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0autostart\uninstall.ps1"
pause
