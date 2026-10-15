"""Register the watchdog as a repeating task, and fix the two autostart defects found.

Defects this addresses:
  1. the tunnel task used only an AtLogOn trigger and had never run (LastTaskResult 267011,
     LastRun 1999-11-30) because the logon had already happened before the task existed.
     Now it also triggers AtStartup, so a reboot restores it regardless of logon.
  2. nothing supervised the processes. A logon-triggered task starts them once; when the
     album service was OOM-killed, the public URL stayed dead until someone noticed.
     Now a watchdog task runs every 2 minutes and also at startup.
"""
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = r"C:\path\to\qq-group-album-backup"
PY = sys.executable
WATCHDOG = os.path.join(ROOT, "tools", "watchdog.py")
NGROK_TASK = os.path.join(ROOT, "tools", "ngrok_autostart.py")
TASK_WD = "QQAlbumBackup-Watchdog"
TASK_TUNNEL = "QQAlbumBackup-NgrokTunnel"
USER = f"{os.environ['USERDOMAIN']}\\{os.environ['USERNAME']}"


def ps(script: str, timeout: int = 180):
    res = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                         capture_output=True, text=True, timeout=timeout)
    return ((res.stdout or "") + (res.stderr or "")).strip()


print("=== 1) 看门狗任务（每 2 分钟 + 开机）===")
out = ps(f"""
$action = New-ScheduledTaskAction -Execute '{PY}' -Argument '"{WATCHDOG}"' -WorkingDirectory '{ROOT}'
$t1 = New-ScheduledTaskTrigger -AtStartup
$t2 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes 2) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
$principal = New-ScheduledTaskPrincipal -UserId '{USER}' -LogonType S4U -RunLevel Limited
Register-ScheduledTask -TaskName '{TASK_WD}' -Action $action -Trigger $t1,$t2 `
  -Settings $settings -Principal $principal -Force | Out-Null
Get-ScheduledTask -TaskName '{TASK_WD}' | Select-Object TaskName,State | Format-List
""")
print(out)

print("=== 2) 修好隧道任务（加开机触发 + S4U 免登录运行）===")
out = ps(f"""
$action = New-ScheduledTaskAction -Execute '{PY}' -Argument '"{NGROK_TASK}"' -WorkingDirectory '{ROOT}'
$t1 = New-ScheduledTaskTrigger -AtStartup
$t2 = New-ScheduledTaskTrigger -AtLogOn
$t2.Delay = 'PT45S'
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
$principal = New-ScheduledTaskPrincipal -UserId '{USER}' -LogonType S4U -RunLevel Limited
Register-ScheduledTask -TaskName '{TASK_TUNNEL}' -Action $action -Trigger $t1,$t2 `
  -Settings $settings -Principal $principal -Force | Out-Null
Get-ScheduledTask -TaskName '{TASK_TUNNEL}' | Select-Object TaskName,State | Format-List
""")
print(out)

print("=== 3) 立即跑一次看门狗任务，验证任务本身可用 ===")
out = ps(f"Start-ScheduledTask -TaskName '{TASK_WD}'; Start-Sleep -Seconds 25; "
         f"(Get-ScheduledTaskInfo -TaskName '{TASK_WD}') | "
         f"Select-Object LastRunTime,LastTaskResult,NextRunTime | Format-List")
print(out)

print("=== 4) 全部任务 ===")
out = ps("Get-ScheduledTask -TaskName 'QQAlbumBackup-*' | ForEach-Object { "
         "$i = $_ | Get-ScheduledTaskInfo; "
         "'{0}  State={1}  LastRun={2}  Result={3}' -f $_.TaskName, $_.State, "
         "$i.LastRunTime, $i.LastTaskResult }")
print(out)

print("=== 5) 看门狗日志 ===")
log = os.path.join(ROOT, "tools", "watchdog.log")
if os.path.isfile(log):
    for line in open(log, encoding="utf-8", errors="ignore").read().splitlines()[-6:]:
        print("   ", line)
