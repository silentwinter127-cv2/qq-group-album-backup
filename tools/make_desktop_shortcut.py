"""Create a desktop shortcut that opens the fixed URL with the proxy bypassed.

Why a shortcut instead of a .bat: double-clicking a .bat flashes a console window, and
the .bat must pass --user-data-dir + --no-proxy-server. A .lnk does the same job silently
and is easier to find.

The bypass is required on THIS machine only: a VPN client's PAC routes everything through
a proxy with no bypass rules, and Chrome does not fall back to DIRECT when configured via
PAC. The Funnel hostname resolves to a Tailscale 100.x address, so the proxy drops it.
"""
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = r"C:\path\to\qq-group-album-backup"
URL = "https://silentwinter.tailf5fb77.ts.net"
PROFILE = os.path.join(os.environ["LOCALAPPDATA"], "qqalbum-browser")

EDGE = ""
for candidate in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                  r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
    if os.path.isfile(candidate):
        EDGE = candidate
        break
if not EDGE:
    raise SystemExit("找不到 Edge")

desktop = os.path.join(os.environ["USERPROFILE"], "Desktop")
# 中文系统上桌面目录可能被重定向，取注册表里的真实路径
try:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "[Environment]::GetFolderPath('Desktop')"],
        capture_output=True, text=True, timeout=60)
    real = (out.stdout or "").strip()
    if real and os.path.isdir(real):
        desktop = real
except Exception:
    pass

os.makedirs(PROFILE, exist_ok=True)
link = os.path.join(desktop, "群相册备份（固定地址）.lnk")
args = (f'--user-data-dir="{PROFILE}" --no-proxy-server --no-first-run '
        f'--no-default-browser-check --new-window "{URL}"')

ps = f"""
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut('{link}')
$sc.TargetPath = '{EDGE}'
$sc.Arguments = '{args}'
$sc.WorkingDirectory = '{desktop}'
$sc.Description = '打开群相册备份的固定公网地址（绕过本机代理）'
$sc.Save()
Write-Output 'created'
"""
out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                     capture_output=True, text=True, timeout=120)
print("PowerShell:", (out.stdout or "").strip(), (out.stderr or "").strip()[:200])
print("快捷方式存在:", os.path.isfile(link))
print("路径:", link)
print("目标:", EDGE)
print("参数:", args)
