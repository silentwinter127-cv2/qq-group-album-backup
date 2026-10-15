@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

rem ============================================================
rem  Logon entry point: if the album service is not up on 8787, start it.
rem
rem  The group number is required. Either pass it as the first argument, or
rem  put it in the file "group.txt" next to this script (one line, digits only),
rem  which keeps the group out of the task definition.
rem
rem  Notes (things that bit me):
rem   - Batch files must use CRLF line endings; with LF, cmd.exe chops the
rem     script into garbage ("'-' is not recognized").
rem   - No ^ line continuation inside ( ) blocks.
rem   - Use !VAR! inside blocks, not %VAR%.
rem ============================================================

set GROUP=%~1
if "!GROUP!"=="" (
  if exist "%~dp0group.txt" set /p GROUP=<"%~dp0group.txt"
)
if "!GROUP!"=="" (
  echo [ERROR] no group number: pass it as an argument or create group.txt
  exit /b 2
)

set PORT=8787
set URL=http://127.0.0.1:8787/api/health
set LOG=%~dp0tools\autostart.log
if not exist "%~dp0tools" mkdir "%~dp0tools"

call :log "===== autostart check (group !GROUP!) ====="

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
  start "" /b cmd /c ""%~dp0start_album.bat" !GROUP!"
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
