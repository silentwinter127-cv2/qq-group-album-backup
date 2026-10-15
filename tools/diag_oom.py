"""Find out what crashed and why — look for OOM / resource-exhaustion evidence.

The user reported "software OOM caused a process crash". Worth knowing which process and
whether it is something we can prevent, rather than just papering over it with a watchdog.
"""
import json
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")


def ps(script: str, timeout: int = 180) -> str:
    res = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                         capture_output=True, text=True, timeout=timeout,
                         encoding="utf-8", errors="ignore")
    return ((res.stdout or "") + (res.stderr or "")).strip()


print("=== 1) 应用错误 / 崩溃事件（最近 3 小时）===")
out = ps("""
$since = (Get-Date).AddHours(-3)
Get-WinEvent -FilterHashtable @{LogName='Application'; StartTime=$since} -ErrorAction SilentlyContinue |
  Where-Object { $_.LevelDisplayName -in @('Error','Critical','Warning') } |
  Select-Object -First 12 TimeCreated,LevelDisplayName,ProviderName,@{n='Msg';e={($_.Message -split "`n")[0..1] -join ' | '}} |
  Format-List | Out-String -Width 200
""")
print(out[:3000] if out else "   （无相关事件）")

print("\n=== 2) 系统日志里的内存/资源告警 ===")
out = ps("""
$since = (Get-Date).AddHours(-3)
Get-WinEvent -FilterHashtable @{LogName='System'; StartTime=$since} -ErrorAction SilentlyContinue |
  Where-Object { $_.Message -match 'memory|内存|resource|OOM|exhaust' } |
  Select-Object -First 8 TimeCreated,LevelDisplayName,ProviderName,@{n='Msg';e={($_.Message -split "`n")[0]}} |
  Format-List | Out-String -Width 200
""")
print(out[:2500] if out else "   （无内存相关告警）")

print("\n=== 3) 当前内存状况 ===")
out = ps("""
$os = Get-CimInstance Win32_OperatingSystem
$total = [math]::Round($os.TotalVisibleMemorySize/1MB,1)
$free  = [math]::Round($os.FreePhysicalMemory/1MB,1)
"总内存: $total GB   可用: $free GB   已用: $([math]::Round($total-$free,1)) GB"
"--- 占用最多的 8 个进程 ---"
Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 8 `
  @{n='进程';e={$_.ProcessName}},@{n='内存GB';e={[math]::Round($_.WorkingSet64/1GB,2)}} |
  Format-Table -AutoSize | Out-String
""")
print(out)

print("=== 4) 页面文件 / 提交内存 ===")
out = ps("""
Get-CimInstance Win32_PageFileUsage -ErrorAction SilentlyContinue |
  Select-Object Name,@{n='当前MB';e={$_.CurrentUsage}},@{n='峰值MB';e={$_.PeakUsage}} |
  Format-Table -AutoSize | Out-String
$cs = Get-CimInstance Win32_ComputerSystem
"自动管理页面文件: $($cs.AutomaticManagedPagefile)"
""")
print(out)
