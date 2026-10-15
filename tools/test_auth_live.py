"""End-to-end auth check against a real HTTP server: bypass attempts + normal login.

Runs its own instance on a spare port so the live service is untouched.
"""
import http.cookiejar
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
ROOT = r"C:\path\to\qq-group-album-backup"
PORT = 8799
PASSWORD = "test-pw-123"
BASE = f"http://127.0.0.1:{PORT}"

env = dict(os.environ)
env["QQALBUM_PASSWORD"] = PASSWORD
proc = subprocess.Popen(
    [sys.executable, "-m", "qqalbum", "123456789", "--no-open",
     "--port", str(PORT), "--host", "0.0.0.0"],
    cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def raw(path, method="GET", data=None, headers=None, opener=None):
    req = urllib.request.Request(BASE + path, method=method, data=data,
                                 headers=headers or {})
    try:
        # OpenerDirector 上是 .open()，不是 .urlopen()（模块级才有 urlopen）
        with (opener.open(req, timeout=60) if opener
              else urllib.request.urlopen(req, timeout=60)) as r:
            return r.status, r.read().decode("utf-8", "ignore"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore"), dict(e.headers)


try:
    # 等它起来
    ok = False
    for _ in range(60):
        time.sleep(0.5)
        try:
            status, _, _ = raw("/api/health")
            if status == 200:
                ok = True
                break
        except Exception:
            pass
    print("服务已启动:", ok)
    if not ok:
        raise SystemExit("起不来")

    print("\n=== 1) /api/health 必须免登录（自启动与机器人探活靠它）===")
    status, body, _ = raw("/api/health")
    print(f"   无凭证 GET /api/health → {status}  {body[:80]}")

    print("\n=== 2) 其他接口在未登录时必须 401 ===")
    for path in ("/api/overview", "/api/members", "/api/photos?per_page=1",
                 "/api/time-axis", "/api/export", "/api/videos"):
        status, body, _ = raw(path)
        flag = "✓" if status == 401 else "✗ 泄露了！"
        print(f"   {flag} GET {path:26} → {status}  {body[:60]}")

    print("\n=== 3) 首页在未登录时应给登录页 ===")
    status, body, _ = raw("/", headers={"Accept": "text/html"})
    has_login = "需要访问口令" in body
    print(f"   GET / → {status}，是登录页: {has_login}")

    print("\n=== 4) 静态资源也必须挡住（不能靠猜路径绕过）===")
    for path in ("/static/app.js", "/static/index.html"):
        status, body, _ = raw(path)
        leak = "group_id" in body or "renderAxis" in body
        print(f"   GET {path:24} → {status}  内容泄露: {leak}")

    print("\n=== 5) 伪造/乱猜 cookie 无效 ===")
    for cookie in ("qqalbum_session=deadbeef",
                   "qqalbum_session=",
                   "other=x; qqalbum_session=1",
                   "qqalbum_session=1 OR 1=1"):
        status, _, _ = raw("/api/members",
                           headers={"Cookie": cookie})
        print(f"   Cookie {cookie:38} → {status}")

    print("\n=== 6) 错口令被拒 ===")
    for wrong in ("", "wrong", PASSWORD + "x", "TEST-PW-123"):
        status, body, _ = raw("/api/login", method="POST",
                              data=json.dumps({"password": wrong}).encode(),
                              headers={"Content-Type": "application/json"})
        print(f"   口令 {wrong!r:16} → {status}  {body[:70]}")

    print("\n=== 7) 用 curl 风格的原始请求也不能绕过 ===")
    status, _, _ = raw("/api/overview", headers={"X-Forwarded-For": "127.0.0.1",
                                                 "X-Real-IP": "127.0.0.1"})
    print(f"   带 X-Forwarded-For: 127.0.0.1 → {status}")

    print("\n=== 7.5) 继续猜口令会触发 429 ===")
    status, body, _ = raw("/api/login", method="POST",
                          data=json.dumps({"password": "guess-again"}).encode(),
                          headers={"Content-Type": "application/json"})
    print(f"   第 5 次错口令 → {status}  {body[:80]}")
    print(f"   限速生效: {status == 429}")

    print("\n=== 7.6) 关键：限速期间「正确口令」仍然必须放行 ===")
    print("   （否则攻击者刷几次错口令就能把合法用户一起锁在门外 = 拒绝服务）")
    jar_probe = http.cookiejar.CookieJar()
    opener_probe = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar_probe))
    status, body, _ = raw("/api/login", method="POST",
                          data=json.dumps({"password": PASSWORD}).encode(),
                          headers={"Content-Type": "application/json"},
                          opener=opener_probe)
    print(f"   限速窗口内用正确口令 → {status}  {body[:60]}")
    print(f"   正确口令未被限速挡住: {status == 200}")

    print("\n=== 8) 正确口令 → 拿到 cookie 后所有接口放行 ===")
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    status, body, headers = raw("/api/login", method="POST",
                                data=json.dumps({"password": PASSWORD}).encode(),
                                headers={"Content-Type": "application/json"},
                                opener=opener)
    print(f"   登录 → {status}  {body[:60]}")
    set_cookie = headers.get("Set-Cookie", "")
    print(f"   Set-Cookie: {set_cookie[:90]}")
    print(f"   HttpOnly: {'HttpOnly' in set_cookie}   "
          f"没给 http 加 Secure（否则浏览器不回传）: {'Secure' not in set_cookie}")
    for path in ("/api/overview", "/api/members", "/api/time-axis"):
        status, body, _ = raw(path, opener=opener)
        print(f"   带 cookie GET {path:18} → {status}  {body[:64]}")

    print("\n=== 9) 登录后首页才是真正的界面 ===")
    status, body, _ = raw("/", headers={"Accept": "text/html"}, opener=opener)
    print(f"   GET / → {status}  是主界面: {'renderAxis' in body or 'nav-item' in body}")

    print("\n=== 10) 服务端启动日志里打印的内容（给别人看的地址与口令提示）===")
finally:
    proc.terminate()
    try:
        out, _ = proc.communicate(timeout=15)
    except Exception:
        proc.kill()
        out = ""
    print()
    for line in (out or "").splitlines():
        if any(k in line for k in ("WebUI", "访问口令", "本次口令", "局域网访问", "对外访问", "注意")):
            print("   |", line)
