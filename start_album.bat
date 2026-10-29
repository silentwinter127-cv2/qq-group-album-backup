@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

rem ============================================================
rem  Start the album backup service (WebUI) on a FIXED port.
rem
rem  Why this script exists:
rem   - Plain "python -m qqalbum" silently moves to port 8788 when 8787 is
rem     busy, while the QQ bot skill is configured for 8787 -- that shows up
rem     as "service is running but the bot cannot connect".
rem   So here: if the port is not ours, fail loudly. Never switch ports.
rem
rem  PASSWORD: the UI exposes other people's nicknames / QQ numbers, and this
rem  machine may be published through a tunnel, so the service must carry a
rem  password whenever it is reachable from outside. The value lives in
rem  album_password.txt (generated on first run) and is read here -- never
rem  hardcoded, so it cannot end up in the repository, and reading it from a
rem  file also keeps it out of the process command line.
rem
rem  Usage:
rem     start_album.bat <group number> [extra options]
rem     start_album.bat 123456789
rem ============================================================

set GROUP=%~1
set PORT=8787
set URL=http://127.0.0.1:8787/api/health

if "%GROUP%"=="" (
  echo.
  echo   [ERROR] group number required, e.g.  start_album.bat 123456789
  echo.
  pause
  exit /b 2
)

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

rem --- password: read album_password.txt, generate it if missing -------------
set QPASS=
if exist "%~dp0album_password.txt" for /f "usebackq delims=" %%P in ("%~dp0album_password.txt") do set QPASS=%%P
if not defined QPASS (
  echo       no album_password.txt -- generating one ...
  for /f "usebackq delims=" %%P in (`python "%~dp0tools\make_password.py" 2^>nul`) do set QPASS=%%P
)
if not defined QPASS (
  echo.
  echo       [ERROR] could not obtain a password; refusing to start.
  echo       The interface would be reachable without one, which exposes
  echo       other members' nicknames and QQ numbers.
  echo.
  pause
  exit /b 1
)

echo [2/3] starting service (group !GROUP!, port !PORT!, password on) ...
echo [3/3] closing this window stops the service.
echo.
echo WebUI: http://127.0.0.1:!PORT!/
echo.
echo If this machine is published through a tunnel, hand out that URL plus the
echo password stored in album_password.txt.
echo.

python -m qqalbum !GROUP! --port !PORT! --password !QPASS! %*

if errorlevel 1 (
  echo.
  echo [ERROR] exited with code !errorlevel! -- see the messages above.
  pause
)

:done
endlocal
