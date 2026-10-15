@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

rem ============================================================
rem  Logon entry point: if the album service is not up on 8787,
rem  start it (hidden) and retry a few times.
rem
rem  Notes (things that bit me):
rem   - Batch files must use CRLF line endings; with LF, cmd.exe
rem     chops the script into garbage ("'-' is not recognized").
rem   - No ^ line continuation inside ( ) blocks.
rem   - Use !PORT! inside blocks, not %PORT%.
rem ============================================================

set GROUP=123456789
set PORT=8787
set URL=http://127.0.0.1:8787/api/health
set LOG=%~dp0tools\autostart.log
if not exist "%~dp0tools" mkdir "%~dp0tools"

call :log "===== autostart check ====="

call :probe
if /i "!STATE!"=="UP" (
  call :log "service already up on !PORT!, nothing to do."
  goto :done
)
if /i "!STATE!"=="OTHER" (
  call :log "[WARN] port !PORT! is taken by another program; cannot start."
  call :log "       the bot will not connect. check: netstat -ano ^| findstr :!PORT!"
  goto :done
)

for /L %%A in (1,1,3) do (
  call :log "attempt %%A ..."
  start "" /b wscript.exe "%~dp0start_album_hidden.vbs"
  for /L %%B in (1,1,20) do (
    powershell -NoProfile -Command "Start-Sleep -Seconds 3" >nul 2>nul
    call :probe
    if /i "!STATE!"=="UP" (
      call :log "started ok: http://127.0.0.1:!PORT!/"
      goto :done
    )
  )
  call :log "attempt %%A failed."
)

call :log "[ERROR] failed 3 times; run start_album.bat by hand to see why."

:done
call :log "===== done ====="
endlocal
exit /b 0

:probe
set STATE=DOWN
for /f "usebackq delims=" %%S in (`powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri '!URL!' -TimeoutSec 4 -UseBasicParsing; if ($r.StatusCode -eq 200) { 'UP' } else { 'DOWN' } } catch { if (Get-NetTCPConnection -LocalPort !PORT! -State Listen -ErrorAction SilentlyContinue) { 'OTHER' } else { 'DOWN' } }"`) do set STATE=%%S
exit /b 0

:log
echo %~1
>>"%LOG%" echo [%DATE% %TIME%] %~1
exit /b 0
