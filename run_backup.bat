@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

rem ============================================================
rem  QQ group album local backup
rem  Database and photos live in D:\QQAlbumBackup
rem
rem  Usage:
rem     run_backup.bat                             -> group 123456789
rem     run_backup.bat 123456789 --port 8787
rem     run_backup.bat 123456789 --caption         (resume captions on start)
rem
rem  NOTE: every argument after the group number is forwarded verbatim.
rem  The old version passed only %2..%9 and swallowed flags like --port,
rem  which made a hidden launcher start the service on the wrong port.
rem ============================================================

set GROUP=%~1
if "%GROUP%"=="" set GROUP=123456789

rem Collect the rest without shift (shift would destroy the group value we
rem just read): walk over all args, skip the first, keep the remainder.
set REST=
set SEEN=
for %%A in (%*) do (
  if not defined SEEN (
    set SEEN=1
  ) else (
    set REST=!REST! %%A
  )
)

echo ============================================
echo  QQ album local backup  -  group %GROUP%
echo  database + photos:  D:\QQAlbumBackup
echo ============================================
echo.

python -m qqalbum %GROUP% !REST!

if errorlevel 1 (
  echo.
  echo [ERROR] exited with code !errorlevel! -- see messages above.
  echo         (no pause here: the hidden logon autostart also uses this
  echo          script, and a pause would hang there forever.)
)
endlocal
