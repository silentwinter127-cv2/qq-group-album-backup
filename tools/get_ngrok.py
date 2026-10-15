"""Download ngrok and inspect whether the free tier really offers a STATIC domain.

Decision to verify, not assume: a stable URL without buying a domain is the whole point,
so check the CLI's own help/config for the "static domain" feature before asking the user
to create an account.
"""
import os
import re
import subprocess
import sys
import urllib.request
import zipfile

sys.stdout.reconfigure(encoding="utf-8")
BIN = r"C:\path\to\qq-group-album-backup\tools\bin"
ZIP = os.path.join(BIN, "ngrok.zip")
EXE = os.path.join(BIN, "ngrok.exe")
URL = "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-windows-amd64.zip"

os.makedirs(BIN, exist_ok=True)
if not os.path.isfile(EXE):
    print("下载 ngrok …")
    with urllib.request.urlopen(URL, timeout=600) as resp, open(ZIP, "wb") as fh:
        total = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            total += len(chunk)
    print(f"   {total/1048576:.1f} MB → 解压")
    with zipfile.ZipFile(ZIP) as zf:
        zf.extractall(BIN)
    print("   解压完成:", os.listdir(BIN))
else:
    print("ngrok.exe 已存在")


def run(args, timeout=90):
    p = subprocess.run([EXE, *args], capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="ignore")
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


code, out = run(["version"])
print(f"\n=== 版本 ===\n{out[:300]}")

code, out = run(["http", "--help"])
print("\n=== http 帮助里与固定域名相关的选项 ===")
for line in out.splitlines():
    if re.search(r"domain|url|hostname|region|authtoken|traffic-policy", line, re.I):
        print("   " + line.rstrip()[:120])

code, out = run(["config", "check"])
print(f"\n=== config check（有没有登录凭证）===\n{out[:400]}")
