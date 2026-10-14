"""Report whether any backup task is currently running (sync / download / AI index / captions)."""
import json
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
BASE = "http://127.0.0.1:8787"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=120) as r:
        return json.loads(r.read().decode())


ov = get("/api/overview")
sync = ov.get("sync") or {}
print("=== 抓取（元数据）任务 ===")
print(f"  running = {sync.get('running')}")
print(f"  stage   = {sync.get('stage')}")
print(f"  message = {sync.get('message')}")
print(f"  progress= {sync.get('current')}/{sync.get('total')}")
print(f"  计数    = {json.dumps(sync.get('counts'), ensure_ascii=False)}")
print(f"  error   = {sync.get('error')}")
print(f"  上次结束= {sync.get('ended_at') or sync.get('finished_at')}")

dl = ov.get("download") or {}
print("\n=== 下载任务 ===")
if dl:
    for key in ("running", "stage", "message", "current", "total", "error"):
        print(f"  {key:8}= {dl.get(key)}")
else:
    print("  （overview 里没有 download 字段）", sorted(ov.keys()))

status = get("/api/ai/status")
print("\n=== AI 索引（打标 + 向量）===")
st = status.get("state") or {}
for key in ("running", "stage", "current", "total", "rate", "eta", "message", "error"):
    print(f"  {key:8}= {st.get(key)}")

print("\n=== 图片描述（BLIP）===")
cap = status.get("caption_state") or {}
for key in ("running", "stage", "current", "total", "rate", "eta", "message", "error"):
    print(f"  {key:8}= {cap.get(key)}")

print("\n=== 覆盖情况 ===")
print(f"  向量   : {status.get('indexed')} / {status.get('local_files')}")
print(f"  描述   : {status.get('captioned')} / {status.get('local_files')}")

stats = ov.get("stats") or {}
print("\n=== 库内数据 ===")
for key in ("albums", "photos", "photos_downloaded", "photos_thumb_only",
            "photos_pending", "photos_failed", "comments", "likes", "feeds", "members"):
    if key in stats:
        print(f"  {key:20}= {stats[key]}")

print("\n=== 结论 ===")
busy = [name for name, snap in (("抓取", sync), ("下载", dl), ("AI索引", st), ("图片描述", cap))
        if isinstance(snap, dict) and snap.get("running")]
print("  正在运行的任务:", "、".join(busy) if busy else "无 —— 当前没有任何任务在跑")
