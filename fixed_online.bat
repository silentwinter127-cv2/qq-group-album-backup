@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

rem ============================================================
rem  固定公网地址 - 免费方案 - Tailscale Funnel
rem
rem  为什么需要它：Cloudflare 临时隧道的网址每次重启都会变
rem  （实测重启一次网址就换了名字），Cloudflare 也明说这种隧道
rem  没有可用性保证。
rem  Tailscale Funnel 免费版就能给一个永久地址：
rem     https://<机器名>.<你的tailnet>.ts.net
rem  对方不需要装任何东西，浏览器打开就行。
rem
rem  用法： fixed_online.bat <群号> [口令]
rem     fixed_online.bat 123456789 MyPass123
rem
rem  首次运行会：
rem    1) 下载并安装 Tailscale - 会弹 UAC，请点"是"
rem    2) 打开浏览器让你登录授权一次
rem    3) 配好 Funnel 并打印你的固定地址
rem ============================================================

set GROUP=%~1
set PASS=%~2
set PORT=8787

if "%GROUP%"=="" (
  echo.
  echo   [用法] fixed_online.bat ^<群号^> [口令]
  echo   例如：fixed_online.bat 123456789 MyPass123
  echo.
  pause
  exit /b 2
)

if "%PASS%"=="" (
  echo.
  echo   [错误] 必须给一个口令。Funnel 是公开地址，口令是唯一的防线。
  echo   例如：fixed_online.bat %GROUP% MyPass123
  echo.
  pause
  exit /b 2
)

echo [1/3] 准备 Tailscale 安装包 ...
if not exist "tools\bin\tailscale-setup.exe" (
  python tools\get_tailscale.py || (
    echo       下载失败，请按上面的提示手动放置安装包。
    pause
    exit /b 1
  )
)

echo [2/3] 安装并开启 Funnel - 过程中会弹 UAC 和浏览器授权 ...
python tools\setup_funnel.py %PORT%
if errorlevel 1 (
  echo.
  echo   配置未完成，请按上面的提示处理后重新运行本脚本。
  pause
  exit /b 1
)

echo [3/3] 启动相册服务 - 带口令，仅本机监听 ...
set OURS=FREE
for /f "usebackq delims=" %%S in (`powershell -NoProfile -Command "try { $r=Invoke-WebRequest -Uri 'http://127.0.0.1:%PORT%/api/health' -TimeoutSec 3 -UseBasicParsing; $j=$r.Content ^| ConvertFrom-Json; if ($j.service -ne 'qqalbum') {'OTHER'} elseif ($j.auth_required -eq $true) {'OURS_AUTH'} else {'OURS_NOAUTH'} } catch { if (Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue) {'OTHER'} else {'FREE'} }"`) do set OURS=%%S

if /i "!OURS!"=="OURS_AUTH" (
  echo       端口 %PORT% 上已有带口令的实例，直接复用。
  goto :report
)
if /i "!OURS!"=="OURS_NOAUTH" (
  echo.
  echo   [错误] 端口 %PORT% 上是**没有口令**的实例，不能对外。
  echo          先运行 stop_album.bat，再重新运行本脚本。
  echo.
  pause
  exit /b 1
)
if /i "!OURS!"=="OTHER" (
  echo.
  echo   [错误] 端口 %PORT% 被别的程序占用。
  echo.
  pause
  exit /b 1
)

start "qqalbum-service" /min cmd /c "python -u -m qqalbum %GROUP% --host 127.0.0.1 --port %PORT% --no-open --password %PASS%"
echo       等待服务就绪 ...
for /L %%A in (1,1,40) do (
  powershell -NoProfile -Command "Start-Sleep -Milliseconds 800" >nul 2>nul
  set STATE=
  for /f "usebackq delims=" %%S in (`powershell -NoProfile -Command "try { $r=Invoke-WebRequest -Uri 'http://127.0.0.1:%PORT%/api/health' -TimeoutSec 3 -UseBasicParsing; if ($r.StatusCode -eq 200) {'UP'} } catch { 'DOWN' }"`) do set STATE=%%S
  if /i "!STATE!"=="UP" ( goto :report )
)

echo   [错误] 服务没起来，请先单独运行 start_album.bat %GROUP% 看报错。
pause
exit /b 1

:report
echo.
echo ============================================================
if exist "tools\fixed_url.txt" (
  set /p FIXEDURL=<"tools\fixed_url.txt"
  echo   你的固定地址： !FIXEDURL!
  echo   口令： %PASS%
  echo.
  echo   这个地址重启电脑也不会变，可以直接发给别人。
) else (
  echo   固定地址见上面 setup_funnel 的输出。
)
echo ============================================================
echo.
echo 关闭本窗口不影响访问；Funnel 与相册服务都在后台运行。
echo 想彻底停止对外访问： tailscale funnel 443 off
pause
endlocal
