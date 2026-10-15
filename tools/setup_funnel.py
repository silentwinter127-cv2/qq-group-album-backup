"""Set up a FIXED public address for the album UI using Tailscale Funnel.

Why this exists: Cloudflare's quick tunnel gives a brand-new random hostname on every
start (verified: nashville-... -> significantly-... after one restart), and Cloudflare
states those tunnels have no uptime guarantee. A named Cloudflare tunnel would fix the
hostname but needs a domain you own. Tailscale Funnel is the free way to a permanent
address: https://<host>.<tailnet>.ts.net — friends need no software at all.

The password protection stays ON, because Funnel is public: anyone with the URL reaches
the login page.

Steps this script drives:
  1. install Tailscale (admin prompt)      — skipped if already present
  2. `tailscale up` (one-time browser sign-in)
  3. `tailscale funnel 443 on`
  4. `tailscale serve --bg --https=443 http://127.0.0.1:<port>`
  5. print the permanent URL
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
ROOT = r"C:\path\to\qq-group-album-backup"
EXE_CANDIDATES = [
    r"C:\Program Files\Tailscale\tailscale.exe",
    r"C:\Program Files (x86)\Tailscale\tailscale.exe",
]


def find_tailscale() -> str:
    for path in EXE_CANDIDATES:
        if os.path.isfile(path):
            return path
    found = shutil.which("tailscale")
    return found or ""


def run(args, timeout=180, check=False):
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="ignore")
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if check and proc.returncode != 0:
        raise RuntimeError(out or f"exit {proc.returncode}")
    return proc.returncode, out


def step(text):
    print(f"\n=== {text} ===")


def service_up() -> bool:
    """tailscaled 是否在跑（装了但没启动时，下面的命令会报错）。"""
    code, out = run(["powershell", "-NoProfile", "-Command",
                     "(Get-Service Tailscale -ErrorAction SilentlyContinue).Status"])
    return "Running" in (out or "")


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else "8787"
    exe = find_tailscale()

    step("1/5 检查 Tailscale 是否已安装")
    if not exe:
        print("   未安装。")
        installer = os.path.join(ROOT, "tools", "bin", "tailscale-setup.exe")
        if not os.path.isfile(installer):
            print("   先下载安装包：python tools\\get_tailscale.py")
            return 1
        print(f"   安装 {installer} …（会弹出 UAC，请点“是”）")
        code, out = run(["msiexec", "/i", installer, "/qn", "/norestart"], timeout=600)
        print(f"   msiexec 退出码 {code}  {out[:160]}")
        if code not in (0, 3010):
            print("   安装失败，请手动双击安装包。")
            return 1
        time.sleep(5)
        exe = find_tailscale()
        if not exe:
            print("   装完了但找不到 tailscale.exe，可能装到了别处。")
            return 1
    print(f"   OK：{exe}")

    step("2/5 确认后台服务在跑")
    if not service_up():
        print("   服务没在跑，尝试启动 Tailscale 客户端…")
        if exe:
            subprocess.Popen([exe, "up"], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        time.sleep(8)
    print("   Tailscale 服务：", "运行中" if service_up() else "未运行（后面可能失败）")

    step("3/5 登录（首次会在浏览器里让你授权一次）")
    code, out = run([exe, "status"], timeout=60)
    if "Logged out" in out or "stopped" in out.lower() or code != 0:
        print("   需要登录，正在打开浏览器授权页…")
        print("   ⚠️ 请在浏览器里用 Google/GitHub/微软账号登录并授权，然后回来继续。")
        subprocess.Popen([exe, "up"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for i in range(60):
            time.sleep(5)
            code, out = run([exe, "status"], timeout=60)
            if code == 0 and "Logged out" not in out:
                print(f"   登录成功（等了 {(i + 1) * 5} 秒）")
                break
            if i % 6 == 5:
                print(f"   …仍在等待登录（{(i + 1) * 5} 秒）")
        else:
            print("   一直没登录成功。请手动跑：tailscale up")
            return 1
    else:
        print("   已登录：")
    for line in (out or "").splitlines()[:4]:
        print("     ", line)

    step("4/5 打开 Funnel 并指向本机相册服务")
    code, out = run([exe, "funnel", "443", "on"], timeout=120)
    print("   funnel on :", out[:200] or f"exit {code}")
    code, out = run([exe, "serve", "--bg", "--https=443",
                     f"http://127.0.0.1:{port}"], timeout=120)
    print("   serve     :", out[:200] or f"exit {code}")

    step("5/5 你的固定地址")
    code, out = run([exe, "funnel", "status"], timeout=60)
    print(out or "(取不到状态)")
    url = ""
    m = re.search(r"https://[A-Za-z0-9.\-]+\.ts\.net", out or "")
    if m:
        url = m.group(0)
        print(f"\n   ✅ 固定地址： {url}")
        print("      这个地址重启电脑、重启服务都不会变，可以直接发给别人。")
        print("      （对方打开会看到口令登录页，口令仍是你设的那个。）")
    else:
        print("\n   没解析出 .ts.net 地址，请把上面的 status 输出发我。")

    step("常见报错对照")
    print("   · 'Funnel is not enabled' / 'HTTPS is disabled'")
    print("     → 去 https://login.tailscale.com/admin/dns 勾选 HTTPS Certificates，再重跑")
    print("   · 'funnel not available for this node'")
    print("     → 去 https://login.tailscale.com/admin/acls 确认没禁用 funnel 节点属性")
    print("   · 地址打开了但提示 502")
    print("     → 本机相册服务没在跑；先 start_album.bat 起来再试")
    if url:
        with open(os.path.join(ROOT, "tools", "fixed_url.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write(url + "\n")
        print(f"\n   已记到 tools\\fixed_url.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
