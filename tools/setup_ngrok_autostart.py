"""Register the logon autostart for the FIXED ngrok tunnel, and protect the authtoken.

Two jobs:
  1. a scheduled task at logon runs ngrok_autostart.py, so the stable address comes back
     by itself after a reboot (the tunnel URL no longer changes, which is the whole point)
  2. tighten permissions on tools/ngrok-config/ngrok.yml (it holds the authtoken) and make
     sure it can never be committed
"""
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = r"C:\path\to\qq-group-album-backup"
PY = sys.executable
SCRIPT = os.path.join(ROOT, "tools", "ngrok_autostart.py")
TASK = "QQAlbumBackup-NgrokTunnel"
CFG_DIR = os.path.join(ROOT, "tools", "ngrok-config")
CFG = os.path.join(CFG_DIR, "ngrok.yml")

# ---------------------------------------------------------------- 1) 计划任务
ps_task = f"""
$action = New-ScheduledTaskAction -Execute '{PY}' `
  -Argument '"{SCRIPT}"' -WorkingDirectory '{ROOT}'
$trigger = New-ScheduledTaskTrigger -AtLogOn
$trigger.Delay = 'PT45S'
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\\$env:USERNAME" `
  -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName '{TASK}' -Action $action -Trigger $trigger `
  -Settings $settings -Principal $principal -Force | Out-Null
Get-ScheduledTask -TaskName '{TASK}' | Select-Object TaskName,State | Format-List
"""
res = subprocess.run(["powershell", "-NoProfile", "-Command", ps_task],
                     capture_output=True, text=True, timeout=180)
print("=== 计划任务 ===")
print((res.stdout or "").strip() or (res.stderr or "").strip()[:400])

# ---------------------------------------------------------------- 2) 权限收紧
print("\n=== 收紧 ngrok.yml 权限（含 authtoken）===")
ps_acl = f"""
$path = '{CFG}'
if (Test-Path $path) {{
  icacls $path /inheritance:r | Out-Null
  icacls $path /grant:r "$env:USERNAME:(R,W)" | Out-Null
  icacls $path /remove "Everyone" "Users" "Authenticated Users" 2>$null | Out-Null
  (Get-Acl $path).Access | ForEach-Object {{
    "$($_.IdentityReference)  $($_.FileSystemRights)"
  }}
}} else {{ 'ngrok.yml 不存在' }}
"""
res = subprocess.run(["powershell", "-NoProfile", "-Command", ps_acl],
                     capture_output=True, text=True, timeout=120)
print((res.stdout or "").strip() or (res.stderr or "").strip()[:300])

# ---------------------------------------------------------------- 3) gitignore
print("\n=== 确保 token 不会进版本库 ===")
ignore_file = os.path.join(ROOT, ".gitignore")
lines = ["tools/ngrok-config/", "ngrok.yml", "album_password.txt"]
text = open(ignore_file, encoding="utf-8").read() if os.path.isfile(ignore_file) else ""
added = []
for line in lines:
    if line not in text:
        text = text.rstrip("\n") + "\n" + line + "\n"
        added.append(line)
if added:
    open(ignore_file, "w", encoding="utf-8", newline="\n").write(text)
print("   新增忽略规则:", added or "（无，已存在）")

# 公开副本也要忽略
pub = r"C:\Users\your-name\Documents\qq-group-album-backup\.gitignore"
if os.path.isfile(pub):
    ptext = open(pub, encoding="utf-8").read()
    padd = []
    for line in lines:
        if line not in ptext:
            ptext = ptext.rstrip("\n") + "\n" + line + "\n"
            padd.append(line)
    if padd:
        open(pub, "w", encoding="utf-8", newline="\n").write(ptext)
    print("   公开副本新增:", padd or "（无，已存在）")
