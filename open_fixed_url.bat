@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

rem ============================================================
rem  Open the fixed public URL in a browser that BYPASSES the proxy.
rem
rem  Why this is needed: this machine runs a VPN client whose PAC is
rem      function FindProxyForURL(url, host) {
rem        return "SOCKS5 127.0.0.1:7890; DIRECT;";
rem      }
rem  i.e. EVERYTHING goes through the proxy, with no bypass rules. Chrome does not
rem  fall back to DIRECT when it is configured via PAC, so it hands the Tailscale IP
rem  (100.72.33.37) to the proxy, which drops it -> ERR_CONNECTION_CLOSED.
rem  Python/curl were fine all along because a raw socket ignores the WinINET proxy.
rem
rem  This only affects THIS machine. Anyone else opening the URL is unaffected.
rem ============================================================

set URL=https://silentwinter.tailf5fb77.ts.net

set EDGE=
for %%P in ("C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
            "C:\Program Files\Microsoft\Edge\Application\msedge.exe") do (
  if not defined EDGE if exist %%P set EDGE=%%~P
)
if not defined EDGE (
  echo   [ERROR] 找不到 Edge。可以手动用这个参数打开浏览器：
  echo           --no-proxy-server
  echo   然后访问 %URL%
  pause
  exit /b 1
)

echo 用独立窗口打开（不走代理）：%URL%
echo 口令在 album_password.txt
start "" "%EDGE%" --no-proxy-server --new-window "%URL%"
endlocal
