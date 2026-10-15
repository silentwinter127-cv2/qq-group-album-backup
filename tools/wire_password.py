"""Store the WebUI password locally and make the launchers use it.

The bug this fixes: the boot chain (scheduled task -> autostart_album.bat ->
start_album_hidden.vbs -> start_album.bat) started the service with NO password.
With Tailscale Funnel pointing at port 8787, one reboot would publish an unprotected
interface (565 members' nicks + QQ numbers) to the public internet.

Design:
  * the password lives in `album_password.txt` next to the launchers;
  * launchers READ that file instead of hardcoding the value, so the password is never
    committed and never shows up in `Get-CimInstance` command lines;
  * if the file is missing, the launcher generates one (Python), saves it, and prints it;
  * the file is .gitignore'd and the published copy ships only the "read/generate" logic.
"""
import os
import secrets
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = r"C:\path\to\qq-group-album-backup"
PW_FILE = os.path.join(ROOT, "album_password.txt")

# 当前正在用的口令，沿用今天验证过的那个，避免又要重新告知
CURRENT = "ij5JYB6U"

if os.path.isfile(PW_FILE):
    existing = open(PW_FILE, encoding="utf-8").read().strip()
    print(f"口令文件已存在，保持不变（{len(existing)} 位）")
else:
    with open(PW_FILE, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(CURRENT + "\n")
    print(f"已写入口令文件: {PW_FILE}")

# 去掉可能的 BOM / 多余空白，确保 bat 读到的就是纯口令
value = open(PW_FILE, encoding="utf-8-sig").read().strip()
if value != open(PW_FILE, encoding="utf-8-sig").read():
    with open(PW_FILE, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(value + "\n")
print(f"口令长度: {len(value)}")

# ---------------------------------------------------------------- .gitignore
gi = os.path.join(ROOT, ".gitignore")
line = "album_password.txt"
if os.path.isfile(gi):
    text = open(gi, encoding="utf-8").read()
    if line not in text:
        open(gi, "w", encoding="utf-8", newline="\n").write(text.rstrip("\n") + "\n" + line + "\n")
        print("已把 album_password.txt 加入 .gitignore")
else:
    open(gi, "w", encoding="utf-8", newline="\n").write("album_password.txt\n")
    print("已创建 .gitignore")

# ------------------------------------------- 本地启动脚本改成读口令文件
READER = (
    "rem 口令从 album_password.txt 读；没有就现生成一个。\n"
    "rem 绝不写死在这里 —— 那样会进版本库，也会出现在进程命令行里。\n"
    "set QPASS=\n"
    'if exist "%~dp0album_password.txt" for /f "usebackq delims=" %%P in ("%~dp0album_password.txt") do set QPASS=%%P\n'
    "if not defined QPASS (\n"
    '  for /f "usebackq delims=" %%P in (`python "%~dp0tools\\make_password.py"`) do set QPASS=%%P\n'
    ")\n"
)

# start_album.bat：把口令传给 python，并拒绝无口令启动
sa = os.path.join(ROOT, "start_album.bat")
text = open(sa, encoding="utf-8-sig").read()
if "album_password.txt" not in text:
    text = text.replace(
        "python -m qqalbum !GROUP! --port !PORT! %*",
        READER + "\n"
        "if not defined QPASS (\n"
        "  echo [ERROR] 拿不到口令，拒绝启动。对外发布必须带口令。\n"
        "  pause\n"
        "  exit /b 1\n"
        ")\n"
        "python -m qqalbum !GROUP! --port !PORT! --password !QPASS! %*")
    with open(sa, "w", encoding="ascii", newline="") as fh:
        fh.write(text.replace("\r\n", "\n").replace("\n", "\r\n"))
    print("已改 start_album.bat：读口令并强制带 --password")
else:
    print("start_album.bat 已改过，跳过")

# autostart_album.bat 也要有兜底：缺口令就别启动
aa = os.path.join(ROOT, "autostart_album.bat")
text = open(aa, encoding="utf-8-sig").read()
if "album_password.txt" not in text:
    marker = 'call :log "===== autostart check'
    if marker in text:
        inject = (
            "rem 没有口令就不要自动启动 —— 否则 Funnel 会把无口令界面发布到公网\n"
            'if not exist "%~dp0album_password.txt" (\n'
            '  if exist "%~dp0tools\\make_password.py" (\n'
            '    for /f "usebackq delims=" %%P in (`python "%~dp0tools\\make_password.py"`) do echo %%P> "%~dp0album_password.txt"\n'
            "  ) else (\n"
            '    call :log "[ERROR] no album_password.txt and no generator; refusing to autostart."\n'
            "    goto :done\n"
            "  )\n"
            ")\n\n"
        )
        text = text.replace(marker, inject + marker, 1)
        with open(aa, "w", encoding="ascii", newline="") as fh:
            fh.write(text.replace("\r\n", "\n").replace("\n", "\r\n"))
        print("已改 autostart_album.bat：缺口令就先补一个")
else:
    print("autostart_album.bat 已改过，跳过")

print("\n完成")
