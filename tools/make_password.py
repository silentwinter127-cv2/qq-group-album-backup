"""Generate (or reuse) the WebUI password file, and print the password on stdout.

Launchers call this so the password is never hardcoded in a .bat. Being in a file also
keeps it out of `Get-CimInstance Win32_Process` command lines, unlike passing it inline.

Prints ONLY the password so `for /f` in batch gets a clean value.
"""
import os
import secrets
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PW_FILE = os.path.join(ROOT, "album_password.txt")


def read() -> str:
    if os.path.isfile(PW_FILE):
        try:
            return open(PW_FILE, encoding="utf-8-sig").read().strip()
        except Exception:
            return ""
    return ""


def main() -> int:
    value = read()
    if not value:
        # token_urlsafe(6) ≈ 8 字符，够挡口令暴破（配合失败限速）
        value = secrets.token_urlsafe(6)
        try:
            with open(PW_FILE, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(value + "\n")
        except Exception as exc:
            print(f"无法写入口令文件: {exc}", file=sys.stderr)
            return 1
        print(f"[已生成新口令并保存到 album_password.txt]", file=sys.stderr)
    sys.stdout.write(value + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
