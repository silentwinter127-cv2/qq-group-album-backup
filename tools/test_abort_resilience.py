"""Try to kill the web thread by aborting connections mid-response, then check it survives.

This reproduces the real failure: a client that hangs up while a response is being
written. Before the fix, the handler tried to answer with a 500 to a dead socket,
the secondary exception escaped the request thread, and the single serve_forever
thread died -- after which the whole process exited with code 1.
"""
import json
import socket
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
BASE = "127.0.0.1"
PORT = 8787


def alive(label):
    try:
        with urllib.request.urlopen(f"http://{BASE}:{PORT}/api/overview", timeout=15) as r:
            data = json.loads(r.read().decode())
        print(f"  [{label}] 服务存活 ✓ 相册 {len(data.get('albums') or [])} 个")
        return True
    except Exception as exc:
        print(f"  [{label}] 服务已死 ✗ {type(exc).__name__}: {exc}")
        return False


def abort_mid_response(path, read_bytes=64):
    """发一个请求，读一点点就硬关 socket（模拟浏览器取消请求）。"""
    sock = socket.create_connection((BASE, PORT), timeout=10)
    try:
        sock.sendall(
            f"GET {path} HTTP/1.1\r\nHost: {BASE}\r\nConnection: close\r\n\r\n".encode())
        # 只读一小段就粗暴关闭
        sock.recv(read_bytes)
    finally:
        # SO_LINGER=0 → 直接发 RST，制造 ConnectionAborted 而不是优雅的 FIN
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                            b"\x01\x00\x00\x00\x00\x00\x00\x00")
        except OSError:
            pass
        sock.close()


with urllib.request.urlopen(f"http://{BASE}:{PORT}/api/photos?per_page=1", timeout=60) as r:
    photo_id = json.loads(r.read().decode())["items"][0]["photo_id"]

print("服务初始状态:")
alive("init")

print("\n连续 25 次「读一半就 RST 断开」，路径覆盖取图 / 缩略图 / JSON 接口:")
paths = [
    f"/api/file?photo_id={photo_id}",
    f"/api/thumb?photo_id={photo_id}&size=640",
    "/api/ai/search?q=KTV&top_k=50",
    "/api/overview",
    f"/api/photo?photo_id={photo_id}",
]
for i in range(25):
    abort_mid_response(paths[i % len(paths)], read_bytes=32 if i % 2 else 256)
    if i % 8 == 7:
        time.sleep(0.3)
print("  完成 25 次中断")

time.sleep(1.0)
print("\n中断后:")
ok = alive("after-abort")

print("\n再验证功能完好（不只是端口还在）:")
try:
    with urllib.request.urlopen(f"http://{BASE}:{PORT}/api/ai/search?q=KTV&top_k=5", timeout=120) as r:
        data = json.loads(r.read().decode())
    print(f"  检索接口 ✓ 返回 {len(data.get('items') or [])} 张，sort={data.get('sort')}")
except Exception as exc:
    print(f"  检索接口 ✗ {type(exc).__name__}: {exc}")
    ok = False

try:
    with urllib.request.urlopen(f"http://{BASE}:{PORT}/api/file?photo_id={photo_id}", timeout=60) as r:
        blob = r.read()
    print(f"  取图接口 ✓ {len(blob)} 字节")
except Exception as exc:
    print(f"  取图接口 ✗ {type(exc).__name__}: {exc}")
    ok = False

print("\n结论:", "客户端中断不再影响服务 ✓" if ok else "服务仍不健壮 ✗")
raise SystemExit(0 if ok else 1)
