# Removes the Startup shortcut. Does not stop the running services (use stop-all.bat).
$lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'VieNeu Studio.lnk'
if (Test-Path $lnk) { Remove-Item $lnk; Write-Host "✅ Đã gỡ tự khởi động: $lnk" }
else { Write-Host 'Chưa bật tự khởi động, không có gì để gỡ.' }
Write-Host 'Server đang chạy (nếu có) vẫn chạy tiếp; muốn tắt ngay thì chạy stop-all.bat.'
