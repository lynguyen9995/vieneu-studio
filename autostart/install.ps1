# Creates a Startup shortcut so VieNeu Studio starts hidden at every Windows logon (no admin needed),
# then starts it now. Undo with uninstall-autostart.bat.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$vbs = Join-Path $PSScriptRoot 'run-hidden.vbs'
$wscript = Join-Path $env:WINDIR 'System32\wscript.exe'
$lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'VieNeu Studio.lnk'
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$s.TargetPath = $wscript
$s.Arguments = '"' + $vbs + '"'
$s.WorkingDirectory = $root
$s.Description = 'Start VieNeu Studio (TTS engine + gateway) hidden at logon'
$s.Save()
Write-Host "✅ Đã bật tự khởi động cùng Windows: $lnk"
Start-Process -FilePath $wscript -ArgumentList @('"' + $vbs + '"', 'now')
Write-Host '⏳ Đang khởi động ngầm. Đợi khoảng 30-60 giây rồi mở http://127.0.0.1:8001/app/'
Write-Host '   Tắt hẳn: stop-all.bat  ·  Gỡ tự khởi động: uninstall-autostart.bat  ·  Log: logs\autostart.log'
