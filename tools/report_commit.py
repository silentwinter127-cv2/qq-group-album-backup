"""Report commit charge vs limit, and the memory footprint of our own service.

Context: the album service was OOM-killed at 01:33 (event log: "系统虚拟内存太低"). The
root cause was the pagefile being effectively 476 MB with "system managed" disabled, so the
commit limit sat only ~6 GB above physical RAM. This script is the before/after check.
"""
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")


def ps(script: str) -> str:
    res = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                         capture_output=True, text=True, timeout=120,
                         encoding="utf-8", errors="ignore")
    return ((res.stdout or "") + (res.stderr or "")).strip()


print(ps(r"""
$os = Get-CimInstance Win32_OperatingSystem
$ramTotal  = [math]::Round($os.TotalVisibleMemorySize/1MB,1)
$ramFree   = [math]::Round($os.FreePhysicalMemory/1MB,1)
$commitLim = [math]::Round($os.TotalVirtualMemorySize/1MB,1)
$commitFree= [math]::Round($os.FreeVirtualMemory/1MB,1)
"物理内存总量 : $ramTotal GB"
"物理内存可用 : $ramFree GB"
"提交上限     : $commitLim GB   (= 物理内存 + 页面文件)"
"提交可用     : $commitFree GB"
"已提交       : $([math]::Round($commitLim - $commitFree,1)) GB"
""
$cs = Get-CimInstance Win32_ComputerSystem
"页面文件自动管理: $($cs.AutomaticManagedPagefile)   （改完需重启才生效）"
Get-CimInstance Win32_PageFileUsage -ErrorAction SilentlyContinue |
  ForEach-Object { "页面文件: $($_.Name)  当前=$($_.CurrentUsage)MB  分配=$($_.AllocatedBaseSize)MB" }
"""))

print("=== 相册服务自身占用（AI 模型按需加载，空闲时很小）===")
print(ps(r"""
$conn = Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
if ($conn) {
  $p = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
  if ($p) {
    "监听进程 PID $($p.Id) : $([math]::Round($p.WorkingSet64/1MB,0)) MB"
  }
} else { "8787 没有监听进程" }
"""))

print("=== 内存大户（这些是别人的进程，我不会去动）===")
print(ps(r"""
Get-CimInstance Win32_Process |
  Where-Object { $_.Name -in @('python.exe','msedge.exe','QQ.exe','node.exe') } |
  Group-Object Name |
  ForEach-Object {
    $sum = ($_.Group | Measure-Object WorkingSetSize -Sum).Sum
    [pscustomobject]@{ 程序=$_.Name; 个数=$_.Count; 合计GB=[math]::Round($sum/1GB,2) }
  } | Sort-Object 合计GB -Descending | Format-Table -AutoSize | Out-String
"""))
