"""Rewrite launcher .bat files with CRLF (cmd.exe needs it) and check for cmd hazards.

教训：.bat 用 LF 结尾时 cmd.exe 会把脚本切成乱码，报一堆
"'载' is not recognized as an internal or external command"。
另外 echo 里的中文全角括号在某些代码页下也会被误解析，所以提示文字里避免用它。
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = r"C:\path\to\qq-group-album-backup"
FILES = ["share_online.bat", "run_backup.bat", "start_album.bat",
         "autostart_album.bat", "stop_album.bat"]

for name in FILES:
    path = os.path.join(ROOT, name)
    if not os.path.isfile(path):
        print(f"   （不存在 {name}）")
        continue
    raw = open(path, "rb").read()
    text = raw.decode("utf-8")
    fixed = text.replace("\r\n", "\n").replace("\n", "\r\n")
    if not fixed.endswith("\r\n"):
        fixed += "\r\n"
    open(path, "wb").write(fixed.encode("utf-8"))
    raw2 = open(path, "rb").read()
    crlf = raw2.count(b"\r\n")
    bare = raw2.count(b"\n") - crlf
    print(f"   {name:22} CRLF={crlf:3} 裸LF={bare}")

print("\n检查 echo 里的全角括号（有些代码页下会报错）：")
for name in FILES:
    path = os.path.join(ROOT, name)
    if not os.path.isfile(path):
        continue
    for i, line in enumerate(open(path, encoding="utf-8"), 1):
        stripped = line.strip().lower()
        if stripped.startswith(("echo", "rem")) and ("（" in line or "）" in line):
            print(f"   {name}:{i}  {line.strip()[:70]}")
print("   （无输出即没有风险行）")
