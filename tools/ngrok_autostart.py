"""Keep the FIXED ngrok address alive: start the tunnel if needed, and report the URL.

Why ngrok instead of Cloudflare quick tunnel: the free ngrok plan includes one automatically
assigned "dev domain" that belongs to the account, so the address is stable. Verified
empirically — the hostname was identical before and after restarting the agent:

    <assigned-name>.ngrok-free.dev

Cloudflare's account-less tunnel instead gets a brand new random hostname every start
(measured: nashville-... -> significantly-... after one restart) and Cloudflare states
those have no uptime guarantee.

Free-plan caveat this script cannot remove: ngrok shows an interstitial ("Visit Site")
in front of HTML traffic until the visitor clicks through; a cookie then suppresses it
for 7 days. API calls are unaffected.

The authtoken lives in tools\\ngrok-config\\ngrok.yml, which must stay out of version
control.
"""
import os
import re
import subprocess
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
ROOT = r"C:\path\to\qq-group-album-backup"
NGROK = os.path.join(ROOT, "tools", "bin", "ngrok.exe")
CFG = os.path.join(ROOT, "tools", "ngrok-config", "ngrok.yml")
LOG = os.path.join(ROOT, "tools", "ngrok_agent.log")
URL_FILE = os.path.join(ROOT, "tools", "public_url.txt")
PORT = 8787
DESKTOPS = [os.path.join(os.environ.get("USERPROFILE", ""), "Desktop"), "D:\\桌面"]


def log(msg: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {msg}")
    with open(os.path.join(ROOT, "tools", "ngrok_autostart.log"), "a",
              encoding="utf-8") as fh:
        fh.write(f"[{stamp}] {msg}\n")


def running() -> bool:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-Process ngrok -ErrorAction SilentlyContinue | Measure-Object).Count"],
        capture_output=True, text=True, timeout=60)
    try:
        return int((out.stdout or "0").strip()) > 0
    except ValueError:
        return False


def read_url() -> str:
    try:
        text = open(LOG, encoding="utf-8", errors="ignore").read()
    except OSError:
        text = ""
    found = re.findall(r"https://[a-z0-9\-]+\.ngrok[a-z0-9\-\.]*", text)
    return found[-1] if found else ""


def reachable(url: str) -> bool:
    try:
        with urllib.request.urlopen(url + "/api/health", timeout=30) as r:
            return r.status == 200
    except Exception:
        return False


def write_shortcuts(url: str) -> None:
    ps = f"""
$ws = New-Object -ComObject WScript.Shell
foreach ($d in @('{DESKTOPS[0]}', '{DESKTOPS[1]}')) {{
  if (Test-Path $d) {{
    Set-Content -Path (Join-Path $d '打开群相册（固定网址）.url') `
      -Value "[InternetShortcut]`r`nURL={url}" -Encoding ASCII
  }}
}}
"""
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   capture_output=True, timeout=120)


def main() -> int:
    if not os.path.isfile(NGROK) or not os.path.isfile(CFG):
        log("ngrok 或配置缺失，跳过（先跑 get_ngrok.py 并配好 authtoken）")
        return 1

    url = read_url() if running() else ""
    if url and reachable(url):
        log(f"隧道已在运行：{url}")
    else:
        if running():
            log("已有 ngrok 但地址不通，重启它")
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            "Get-Process ngrok -ErrorAction SilentlyContinue | "
                            "Stop-Process -Force -ErrorAction SilentlyContinue"],
                           capture_output=True, timeout=60)
            time.sleep(3)
        try:
            os.remove(LOG)
        except OSError:
            pass
        log("启动 ngrok …")
        with open(LOG, "wb") as out:
            subprocess.Popen(
                [NGROK, "http", str(PORT), "--config", CFG,
                 "--log", "stdout", "--log-format", "json"],
                stdout=out, stderr=out,
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        for _ in range(60):
            time.sleep(2)
            url = read_url()
            if url and reachable(url):
                break

    if not url:
        log("没拿到 ngrok 地址，见 tools\\ngrok_agent.log")
        return 1

    with open(URL_FILE, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(url + "\n")
    write_shortcuts(url)
    log(f"固定地址：{url}（已写入 tools\\public_url.txt 与桌面快捷方式）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
