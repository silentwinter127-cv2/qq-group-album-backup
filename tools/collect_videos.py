"""Collect group-album video metadata into the backup database (one shot).

Runs the same code path the crawler uses, so it also proves the wiring works.
"""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\path\to\qq-group-album-backup")

from qqalbum.crawler import SyncState, BackupManager  # noqa: E402
from qqalbum.store import Store  # noqa: E402

import os as _os
GROUP = _os.environ.get("QQALBUM_GROUP", "<群号>")
DB = r"<备份目录>\qqalbum.sqlite3"

store = Store(DB)
print("入库前视频数:", store.video_count(GROUP))

manager = BackupManager(GROUP, store, root_dir=r"<备份目录>",
                        client_factory=lambda: None)
state = manager.state

print("开始收集视频清单（只抓元数据，不下载视频文件）…")
t0 = time.time()
manager._sync_videos(state, [])
print(f"耗时 {time.time() - t0:.0f}s")

snap = state.snapshot()
print("阶段:", snap["stage"], "| 计数:", snap.get("counters"))
stats = store.video_stats(GROUP)
print("视频统计:", stats)
print("库内视频数:", store.video_count(GROUP))

print("\n=== 按相册分布 ===")
for row in store.query(
        "SELECT album_title, COUNT(*) n, SUM(CASE WHEN photo_id IS NOT NULL THEN 1 ELSE 0 END) linked "
        "FROM videos WHERE group_id=? GROUP BY album_id ORDER BY n DESC LIMIT 10", (GROUP,)):
    print(f"   {str(row['album_title'])[:26]:26} {row['n']:4} 个（对上封面 {row['linked']}）")

print("\n=== 样本 ===")
for row in store.query(
        "SELECT video_id, album_title, width, height, file_status, video_url, lloc "
        "FROM videos WHERE group_id=? ORDER BY upload_epoch DESC LIMIT 5", (GROUP,)):
    print(f"   {row['video_id'][:34]}  {row['width']}x{row['height']}  "
          f"状态={row['file_status']}  相册={str(row['album_title'])[:16]}")
    print(f"      url={str(row['video_url'])[:100]}")

store.close()
