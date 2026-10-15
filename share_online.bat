@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

rem ============================================================
rem  临时把本机相册界面开给外地的人访问 - Cloudflare 快速隧道
rem
rem  用法：
rem     share_online.bat <群号> [口令]
rem     share_online.bat 123456789 MyPass123
rem
rem  为什么必须给口令: 隧道会把界面挂到公网上，任何人都能扫到。
rem  没有口令就等于把群成员昵称/QQ号/全部照片公开。
rem
rem  注意：
rem   - 快速隧道的网址每次启动都会变，关掉窗口即失效；
rem   - 第一次会自动下载 cloudflared 约 20 MB到 tools\bin\；
rem   - 关掉本窗口 = 停止对外分享。
rem ============================================================

set GROUP=%~1
set PASS=%~2
set PORT=8787

if "%GROUP%"=="" (
  echo.
  echo   [用法] share_online.bat ^<群号^> [口令]
  echo   例如：share_online.bat 123456789 MyPass123
  echo.
  pause
  exit /b 2
)

if "%PASS%"=="" (
  echo.
  echo   [错误] 必须给一个口令，否则等于把群成员信息挂到公网上。
  echo   例如：share_online.bat %GROUP% MyPass123
  echo.
  pause
  exit /b 2
)

if not exist "tools\bin\cloudflared.exe" (
  echo [1/4] 第一次使用，下载 cloudflared ...
  python tools\get_cloudflared.py || (
    echo       下载失败，请看上面的提示手动放置。
    pause
    exit /b 1
  )
)

rem 端口占用必须先判断：如果这个端口上已经有一个「没口令」的服务在跑
rem 而我们还照常建隧道，那隧道就指到了那个没口令的实例上 —— 口令形同虚设。
rem 判定依赖 /api/health 里的 auth_required；老版本没有这个字段，
rem 所以取不到就按「没口令」处理 —— 宁可不分享，也不能裸奔。
set OURS=FREE
for /f "usebackq delims=" %%S in (`powershell -NoProfile -Command "try { $r=Invoke-WebRequest -Uri 'http://127.0.0.1:%PORT%/api/health' -TimeoutSec 3 -UseBasicParsing; $j=$r.Content ^| ConvertFrom-Json; if ($j.service -ne 'qqalbum') {'OTHER'} elseif ($j.auth_required -eq $true) {'OURS_AUTH'} else {'OURS_NOAUTH'} } catch { if (Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue) {'OTHER'} else {'FREE'} }"`) do set OURS=%%S
if /i "!OURS!"=="OURS_NOAUTH" (
  echo.
  echo   [错误] 端口 %PORT% 上已经有一个相册服务在跑，而且它**没有口令**。
  echo          现在建隧道的话会指到那个实例上，口令就白设了。
  echo.
  echo   二选一：
  echo     a^) 先停掉它： stop_album.bat        然后重新运行本脚本
  echo     b^) 换个端口： 把本脚本里的 set PORT=%PORT% 改成 8788 之类
  echo.
  pause
  exit /b 1
)
if /i "!OURS!"=="OTHER" (
  echo.
  echo   [错误] 端口 %PORT% 被别的程序占用，无法启动相册服务。
  echo          查占用者： netstat -ano ^| findstr :%PORT%
  echo.
  pause
  exit /b 1
)

if /i "!OURS!"=="OURS_AUTH" (
  echo [2/4] 端口 %PORT% 上已经有带口令的相册服务，直接复用它。
  goto :tunnel
)

echo [2/4] 启动相册服务 - 仅本机监听，由隧道转发 ...
start "qqalbum-service" /min cmd /c "python -u -m qqalbum %GROUP% --host 127.0.0.1 --port %PORT% --no-open --password %PASS%"

echo       等待服务就绪 ...
set READY=0
for /L %%A in (1,1,40) do (
  if "!READY!"=="0" (
    powershell -NoProfile -Command "Start-Sleep -Milliseconds 800" >nul 2>nul
    set STATE=
    for /f "usebackq delims=" %%S in (`powershell -NoProfile -Command "try { $r=Invoke-WebRequest -Uri 'http://127.0.0.1:%PORT%/api/health' -TimeoutSec 3 -UseBasicParsing; if ($r.StatusCode -eq 200) {'UP'} } catch { 'DOWN' }"`) do set STATE=%%S
    if /i "!STATE!"=="UP" set READY=1
  )
)
if "!READY!"=="0" (
  echo.
  echo   [错误] 服务没起来。请先单独运行 start_album.bat %GROUP% 看报错。
  pause
  exit /b 1
)

:tunnel
echo [3/4] 建立公网隧道 ...
echo.
echo ============================================================
echo   下面会打印一个 https://xxxx.trycloudflare.com 的网址
echo   把它和你设的口令一起发给对方，对方用手机浏览器就能看
echo   关闭本窗口 = 停止分享
echo ============================================================
echo.

tools\bin\cloudflared.exe tunnel --url http://127.0.0.1:%PORT% --no-autoupdate

echo.
echo [4/4] 隧道已关闭，服务仍在后台运行，可用 stop_album.bat 停掉。
pause
endlocal
