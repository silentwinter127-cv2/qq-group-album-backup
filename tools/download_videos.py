"""Download all pending group-album videos (resumable; refreshes expired URLs)."""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\path\to\qq-group-album-backup")

from qqalbum.crawler import BackupManager  # noqa: E402
from qqalbum.store import Store  # noqa: E402

GROUP = "123456789"
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 0

store = Store(r"D:\QQAlbumBackup\qqalbum.sqlite3")
print("待下载视频:", len(store.videos_missing_files(GROUP, limit=LIMIT)))

manager = BackupManager(GROUP, store, root_dir=r"D:\QQAlbumBackup",
                        client_factory=lambda: None)
state = manager.state
manager.start_video_download(limit=LIMIT)

last = ""
t0 = time.time()
while state.running:
    snap = state.snapshot()
    line = f"{snap['current']}/{snap['total']} {snap['stage']}"
    if line != last:
        print(f"  {line}", flush=True)
        last = line
    time.sleep(2)

snap = state.snapshot()
print("结束阶段:", snap["stage"], "| 错误:", snap.get("error"))
for line in (snap.get("log") or [])[-6:]:
    print("   ", line[:150])
stats = store.video_stats(GROUP)
print(f"视频：已下载 {stats['done']}/{stats['total']}，失败 {stats['failed']}，"
      f"合计 {stats['bytes']/1048576:.1f} MB，耗时 {time.time()-t0:.0f}s")
store.close()
