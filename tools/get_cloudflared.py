"""Download cloudflared (Cloudflare Tunnel client) for the quick-tunnel helper.

网络不可达时只是打印提示，不影响其它功能 —— 这个脚本是"想让外地朋友访问"时才需要。
"""
import os
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
DEST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin")
DEST = os.path.join(DEST_DIR, "cloudflared.exe")
URL = ("https://github.com/cloudflare/cloudflared/releases/latest/download/"
       "cloudflared-windows-amd64.exe")


def main() -> int:
    if os.path.isfile(DEST) and os.path.getsize(DEST) > 10 * 1024 * 1024:
        print(f"已存在，跳过：{DEST} ({os.path.getsize(DEST)/1048576:.1f} MB)")
        return 0
    os.makedirs(DEST_DIR, exist_ok=True)
    print(f"下载 cloudflared …\n  {URL}")
    try:
        with urllib.request.urlopen(URL, timeout=180) as resp, open(DEST, "wb") as fh:
            total = 0
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                total += len(chunk)
    except Exception as exc:
        print(f"下载失败：{type(exc).__name__}: {exc}")
        print("可以手动下载后放到 tools/bin/cloudflared.exe：")
        print("  https://github.com/cloudflare/cloudflared/releases")
        return 1
    print(f"完成：{DEST} ({total/1048576:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
