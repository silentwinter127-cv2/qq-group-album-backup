"""Download the Tailscale Windows installer (for the free fixed-address setup).

Tailscale Funnel gives a permanent https://<host>.<tailnet>.ts.net address on the free
Personal plan, so no domain purchase is needed. This only fetches the installer; running
it requires admin rights and a one-time browser sign-in, which the .bat walks through.
"""
import os
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
DEST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin")
DEST = os.path.join(DEST_DIR, "tailscale-setup.exe")
URL = "https://pkgs.tailscale.com/stable/tailscale-setup-latest-amd64.msi"


def main() -> int:
    if os.path.isfile(DEST) and os.path.getsize(DEST) > 5 * 1024 * 1024:
        print(f"已存在，跳过：{DEST} ({os.path.getsize(DEST)/1048576:.1f} MB)")
        return 0
    os.makedirs(DEST_DIR, exist_ok=True)
    print(f"下载 Tailscale 安装包 …\n  {URL}")
    try:
        with urllib.request.urlopen(URL, timeout=300) as resp, open(DEST, "wb") as fh:
            total = 0
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                total += len(chunk)
    except Exception as exc:
        print(f"下载失败：{type(exc).__name__}: {exc}")
        print("可以手动下载后放到 tools/bin/tailscale-setup.exe：")
        print("  https://tailscale.com/download/windows")
        return 1
    print(f"完成：{DEST} ({total/1048576:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
