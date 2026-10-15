@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

rem ============================================================
rem  Show the CURRENT public address of the album UI.
rem
rem  Cloudflare's free quick tunnel gets a NEW random hostname on every restart, so the
rem  address cannot be memorised. This prints whatever is live right now and keeps a copy
rem  in tools\public_url.txt.
rem ============================================================

python "%~dp0tools\show_public_url.py"
echo.
pause
endlocal
