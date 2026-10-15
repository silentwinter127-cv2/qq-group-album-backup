"""Verify the launcher's password plumbing without touching the running service.

Checks:
  1. the .bat parses album_password.txt the same way (simulated in Python)
  2. the generated value has no stray whitespace / BOM that would corrupt --password
  3. the value we would pass actually authenticates against a throwaway instance
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
ROOT = r"C:\path\to\qq-group-album-backup"
PW_FILE = os.path.join(ROOT, "album_password.txt")

print("=== 1) 文件内容与类型（有没有 BOM / 换行污染）===")
raw = open(PW_FILE, "rb").read()
print(f"   原始字节: {raw!r}")
value = open(PW_FILE, encoding="utf-8-sig").read().strip()
print(f"   解析后  : {value!r}  长度 {len(value)}")
print(f"   含空白  : {any(c.isspace() for c in value)}")
print(f"   有 BOM   : {raw.startswith(b'\\xef\\xbb\\xbf')}")

print("\n=== 2) 模拟 .bat 的解析（for /f delims= 取第一行）===")
first_line = raw.decode("utf-8-sig").splitlines()[0] if raw.strip() else ""
bat_value = first_line.strip()
print(f"   bat 会得到: {bat_value!r}")
print(f"   与解析值一致: {bat_value == value}")

print("\n=== 3) 用这个值起一个临时实例，验证口令真的生效 ===")
PORT = 8801
env = dict(os.environ)
env["QQALBUM_PASSWORD"] = ""
proc = subprocess.Popen(
    [sys.executable, "-u", "-m", "qqalbum", "123456789", "--host", "127.0.0.1",
     "--port", str(PORT), "--no-open", "--password", bat_value],
    cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    ok = False
    for _ in range(60):
        time.sleep(0.5)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health",
                                        timeout=5) as r:
                health = json.loads(r.read().decode())
            ok = True
            break
        except Exception:
            pass
    print(f"   临时实例起来了: {ok}")
    if ok:
        print(f"   auth_required: {health.get('auth_required')}")
        # 未登录应 401
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/members", timeout=10)
            print("   ✗ 免登录可读")
        except urllib.error.HTTPError as e:
            print(f"   ✓ 未登录 → {e.code}")
        # 用从文件解析出的口令登录
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}/api/login", method="POST",
            data=json.dumps({"password": bat_value}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"   ✓ 用文件里的口令登录 → {r.status} {r.read().decode()[:40]}")
finally:
    proc.terminate()
    try:
        proc.communicate(timeout=10)
    except Exception:
        proc.kill()
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    f"Get-NetTCPConnection -LocalPort {PORT} -State Listen "
                    f"-ErrorAction SilentlyContinue | ForEach-Object "
                    f"{{ Stop-Process -Id $_.OwningProcess -Force "
                    f"-ErrorAction SilentlyContinue }}"],
                   capture_output=True)
    print("   临时实例已清理")
