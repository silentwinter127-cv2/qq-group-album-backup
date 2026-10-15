@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

rem ============================================================
rem  Open the fixed public URL in a browser that BYPASSES the proxy.
rem
rem  Why this is needed here: a VPN client sets a PAC that sends EVERYTHING to a proxy
rem      function FindProxyForURL(url, host) {
rem        return "SOCKS5 127.0.0.1:7890; DIRECT;";
rem      }
rem  The Funnel hostname resolves to a Tailscale address (100.64.0.0/10). Chrome does not
rem  fall back to DIRECT when configured via PAC, so it hands 100.x.y.z to the proxy and
rem  the connection is closed -> ERR_CONNECTION_CLOSED. Python/curl were always fine
rem  because a raw socket ignores the WinINET proxy.
rem
rem  IMPORTANT: passing --no-proxy-server to an ALREADY RUNNING Edge does nothing --
rem  Edge is single-instance, the flag is dropped, and the new window keeps using the
rem  PAC. That is exactly why the first version of this script still failed to open.
rem  Forcing a separate --user-data-dir makes Edge start its own process, where the
rem  flag actually takes effect. Verified: login page renders, login returns 9 stat cards.
rem
rem  This only affects THIS machine. Other people opening the URL are unaffected.
rem ============================================================

set URL=https://silentwinter.tailf5fb77.ts.net
set PROFILE=%LOCALAPPDATA%\qqalbum-browser

set EDGE=
for %%P in ("C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
            "C:\Program Files\Microsoft\Edge\Application\msedge.exe") do (
  if not defined EDGE if exist %%P set EDGE=%%~P
)
if not defined EDGE (
  echo   [ERROR] 找不到 Edge。
  echo   可以手动用这些参数打开浏览器：
  echo       --no-proxy-server --user-data-dir="%PROFILE%"
  echo   然后访问 %URL%
  pause
  exit /b 1
)

if not exist "%PROFILE%" mkdir "%PROFILE%" >nul 2>nul

echo 打开：%URL%
echo 使用独立浏览器配置，避免复用已运行的窗口而丢掉代理设置：
echo   %PROFILE%
echo.
echo 口令在 album_password.txt

rem 不用 ^ 续行 —— start 命令里带续行会让参数被截断（这个坑踩过一次）
start "" "%EDGE%" --user-data-dir="%PROFILE%" --no-proxy-server --no-first-run --no-default-browser-check --new-window "%URL%"
endlocal
