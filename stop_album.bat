@echo off
setlocal
set PORT=8787

echo stopping album service on port %PORT% ...
powershell -NoProfile -Command "$c = Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue; if (-not $c) { Write-Host '   not listening; service was not running.'; exit 0 }; $pids = $c.OwningProcess | Sort-Object -Unique; foreach ($p in $pids) { $proc = Get-Process -Id $p -ErrorAction SilentlyContinue; if ($proc -and $proc.ProcessName -eq 'python') { Write-Host ('   stopping PID ' + $p); Stop-Process -Id $p -Force } else { Write-Host ('   port held by non-python PID ' + $p + ', left alone.') } }"

echo done. unfinished download/index tasks resume next start.
endlocal
