"""Write the launcher .bat files with CRLF line endings and ASCII-only text.

Why this exists: the editor writes LF, and cmd.exe mis-parses LF-only batch files
(symptoms: fragments like "'-' is not recognized", "'stat' is not recognized").
Batch files must be CRLF; keeping them ASCII also avoids codepage surprises.
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
BASE = r"C:\path\to\qq-group-album-backup"

START = r"""@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

rem ============================================================
rem  Start the QQ group album backup service (WebUI + bot skill).
rem
rem  Why this script:
rem   - The service does not start by itself; after a reboot the bot
rem     cannot reach the album port.
rem   - Plain "python -m qqalbum" silently moves to port 8788 when
rem     8787 is busy, while the bot skill is configured for 8787 --
rem     that shows up as "service is running but bot cannot connect".
rem   So here: if the port is not ours, fail loudly. Never switch ports.
rem ============================================================

set GROUP=123456789
set PORT=8787
set URL=http://127.0.0.1:8787/api/health

echo [1/3] checking port !PORT! ...
set STATE=DOWN
for /f "usebackq delims=" %%S in (`powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri '!URL!' -TimeoutSec 5 -UseBasicParsing; if ($r.StatusCode -eq 200) { 'UP' } else { 'DOWN' } } catch { if (Get-NetTCPConnection -LocalPort !PORT! -State Listen -ErrorAction SilentlyContinue) { 'OTHER' } else { 'DOWN' } }"`) do set STATE=%%S

if /i "!STATE!"=="UP" (
  echo       Album service is already running at http://127.0.0.1:!PORT!/
  echo       Just open that address in your browser.
  goto :done
)
if /i "!STATE!"=="OTHER" (
  echo.
  echo       [ERROR] Port !PORT! is taken by another program.
  echo       The bot skill points at http://127.0.0.1:!PORT!/ so it cannot connect.
  echo       Find the owner:  netstat -ano ^| findstr :!PORT!
  echo       Or change "service URL" in the skill settings.
  echo.
  pause
  exit /b 1
)

echo [2/3] starting service (group !GROUP!, port !PORT!) ...
echo [3/3] closing this window stops the service; for background use autostart_album.bat
echo.
echo WebUI: http://127.0.0.1:!PORT!/
echo.

python -m qqalbum !GROUP! --port !PORT! %*

if errorlevel 1 (
  echo.
  echo [ERROR] exited with code !errorlevel! -- see the messages above.
  pause
)

:done
endlocal
"""

AUTOSTART = r"""@echo off
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
"""

STOP = r"""@echo off
setlocal
set PORT=8787

echo stopping album service on port %PORT% ...
powershell -NoProfile -Command "$c = Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue; if (-not $c) { Write-Host '   not listening; service was not running.'; exit 0 }; $pids = $c.OwningProcess | Sort-Object -Unique; foreach ($p in $pids) { $proc = Get-Process -Id $p -ErrorAction SilentlyContinue; if ($proc -and $proc.ProcessName -eq 'python') { Write-Host ('   stopping PID ' + $p); Stop-Process -Id $p -Force } else { Write-Host ('   port held by non-python PID ' + $p + ', left alone.') } }"

echo done. unfinished download/index tasks resume next start.
endlocal
"""

for name, text in (("start_album.bat", START),
                   ("autostart_album.bat", AUTOSTART),
                   ("stop_album.bat", STOP)):
    path = os.path.join(BASE, name)
    data = text.replace("\r\n", "\n").replace("\n", "\r\n")
    with open(path, "w", encoding="ascii", newline="") as fh:
        fh.write(data)
    raw = open(path, "rb").read()
    print(f"{name:22} {len(raw):5} bytes  CRLF={raw.count(bytes([13,10]))}  "
          f"bareLF={raw.count(bytes([10])) - raw.count(bytes([13,10]))}  "
          f"ascii={all(b < 128 for b in raw)}")
