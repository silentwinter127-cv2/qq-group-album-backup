@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

rem ============================================================
rem  QQ group album local backup
rem  Photos/videos and the database are written under the path given to
rem  --db / --root (see README; default is next to this script).
rem
rem  Usage:
rem     run_backup.bat <group number> [options]
rem     run_backup.bat 123456789 --port 8787
rem     run_backup.bat 123456789 --caption        (resume captions on start)
rem
rem  The group number is required: there is no built-in default, because a
rem  backup tool has no business assuming which group you belong to.
rem
rem  NOTE: every argument after the group number is forwarded verbatim.
rem ============================================================

set GROUP=%~1
if "%GROUP%"=="" (
  echo.
  echo   [ERROR] group number required, e.g.  run_backup.bat 123456789
  echo.
  exit /b 2
)
if "%GROUP%"=="--help" goto :help
if "%GROUP%"=="-h" goto :help

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
echo  QQ group album local backup  -  group %GROUP%
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
exit /b 0

:help
echo Usage: run_backup.bat ^<group number^> [options]
echo    e.g. run_backup.bat 123456789 --port 8787
endlocal
exit /b 0
