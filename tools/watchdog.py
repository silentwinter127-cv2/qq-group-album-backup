"""Watchdog: restart the album service and/or the tunnel if either died.

Why this exists: the album service was started by a logon-triggered task, so it came back
after boot — but when it was killed (an OOM crash), nothing brought it back, and the public
address stayed dead until someone noticed. Autostart is not supervision.

Registered as a task that repeats every few minutes, so a killed process self-heals within
one interval. Also runs at startup, independent of whether anyone logs in.

Checks, in order:
  * is /api/health answering on the port? if not -> (re)start the service, with the password
    from album_password.txt (never hardcoded, and not passed on a command line we log)
  * is the tunnel agent alive AND its public URL reaching /api/health? if not -> restart it
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
ROOT = r"C:\path\to\qq-group-album-backup"
TOOLS = os.path.join(ROOT, "tools")
GROUP = "123456789"
PORT = 8787
PW_FILE = os.path.join(ROOT, "album_password.txt")
NGROK = os.path.join(TOOLS, "bin", "ngrok.exe")
NGROK_CFG = os.path.join(TOOLS, "ngrok-config", "ngrok.yml")
NGROK_LOG = os.path.join(TOOLS, "ngrok_agent.log")
STATE = os.path.join(TOOLS, "watchdog.log")


def log(msg: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line)
    try:
        with open(STATE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        # keep the log from growing without bound
        if os.path.getsize(STATE) > 512 * 1024:
            lines = open(STATE, encoding="utf-8", errors="ignore").read().splitlines()
            open(STATE, "w", encoding="utf-8").write("\n".join(lines[-500:]) + "\n")
    except OSError:
        pass


def http_ok(url: str, timeout: int = 15) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def read_password() -> str:
    try:
        return open(PW_FILE, encoding="utf-8-sig").read().strip()
    except OSError:
        return ""


def port_has_listener() -> bool:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(Get-NetTCPConnection -LocalPort {PORT} -State Listen "
         f"-ErrorAction SilentlyContinue | Measure-Object).Count"],
        capture_output=True, text=True, timeout=60)
    try:
        return int((out.stdout or "0").strip()) > 0
    except ValueError:
        return False


def kill_stale_service() -> None:
    """端口被别的（可能是无口令的旧实例）占着时，先清掉再起。"""
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"Get-NetTCPConnection -LocalPort {PORT} -State Listen "
         f"-ErrorAction SilentlyContinue | ForEach-Object "
         f"{{ Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }}"],
        capture_output=True, timeout=60)
    time.sleep(2)


def start_service() -> bool:
    pw = read_password()
    if not pw:
        log("[错误] 拿不到口令，拒绝启动（对外发布必须带口令）")
        return False
    args = [sys.executable, "-u", "-m", "qqalbum", GROUP,
            "--host", "127.0.0.1", "--port", str(PORT),
            "--no-open", "--password", pw]
    with open(os.path.join(TOOLS, "webui_watchdog.log"), "ab") as out:
        subprocess.Popen(args, cwd=ROOT, stdout=out, stderr=out,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    for _ in range(45):
        time.sleep(2)
        if http_ok(f"http://127.0.0.1:{PORT}/api/health"):
            return True
    return False


def tunnel_url() -> str:
    try:
        text = open(NGROK_LOG, encoding="utf-8", errors="ignore").read()
    except OSError:
        return ""
    found = re.findall(r"https://[a-z0-9\-]+\.ngrok[a-z0-9\-\.]*", text)
    return found[-1] if found else ""


def ngrok_alive() -> bool:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-Process ngrok -ErrorAction SilentlyContinue | Measure-Object).Count"],
        capture_output=True, text=True, timeout=60)
    try:
        return int((out.stdout or "0").strip()) > 0
    except ValueError:
        return False


def start_tunnel() -> bool:
    if ngrok_alive():
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Get-Process ngrok -ErrorAction SilentlyContinue | "
                        "Stop-Process -Force -ErrorAction SilentlyContinue"],
                       capture_output=True, timeout=60)
        time.sleep(3)
    try:
        os.remove(NGROK_LOG)
    except OSError:
        pass
    with open(NGROK_LOG, "wb") as out:
        subprocess.Popen([NGROK, "http", str(PORT), "--config", NGROK_CFG,
                          "--log", "stdout", "--log-format", "json"],
                         stdout=out, stderr=out,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    for _ in range(45):
        time.sleep(2)
        url = tunnel_url()
        if url and http_ok(url + "/api/health", timeout=25):
            return True
    return False


def main() -> int:
    acted = False

    if not http_ok(f"http://127.0.0.1:{PORT}/api/health"):
        log("相册服务无响应，重启它")
        if port_has_listener():
            kill_stale_service()
        if start_service():
            log(f"相册服务已恢复（127.0.0.1:{PORT}）")
            acted = True
        else:
            log("[失败] 相册服务起不来，见 tools\\webui_watchdog.log")

    url = tunnel_url()
    if not ngrok_alive() or not url or not http_ok(url + "/api/health", timeout=25):
        log("隧道不可用，重启它")
        if start_tunnel():
            log(f"隧道已恢复：{tunnel_url()}")
            acted = True
        else:
            log("[失败] 隧道起不来，见 tools\\ngrok_agent.log")
    elif not acted:
        log("一切正常")

    # 把当前地址写下来，方便随时查看
    url = tunnel_url()
    if url:
        try:
            with open(os.path.join(TOOLS, "public_url.txt"), "w",
                      encoding="utf-8", newline="\n") as fh:
                fh.write(url + "\n")
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
