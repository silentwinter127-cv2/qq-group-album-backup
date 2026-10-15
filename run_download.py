"""Download original images for stored photos (E2E test driver)."""
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qqalbum.crawler import BackupManager  # noqa: E402
from qqalbum.qzone import QzoneClient  # noqa: E402
from qqalbum.store import Store  # noqa: E402

GROUP = "123456789"
ROOT = r"D:\QQAlbumBackup"
LIMIT = next((int(a) for a in sys.argv[1:] if a.isdigit()), 0)

store = Store(os.path.join(ROOT, "qqalbum.sqlite3"))
manager = BackupManager(GROUP, store, lambda: QzoneClient(GROUP, port=9222), root_dir=ROOT,
                        download_workers=6)
print("start download, limit:", LIMIT, flush=True)
manager.start_download(limit=LIMIT, retry_failed=True)

last = ""
while manager.state.running:
    snap = manager.state.snapshot()
    line = f"{snap['stage']} {snap['current']}/{snap['total']} {snap['message'][:60]}"
    if line != last:
        print(line, flush=True)
        last = line
    time.sleep(1.5)
snap = manager.state.snapshot()
print("final:", snap["stage"], snap.get("error"), flush=True)
print("stats:", store.stats(GROUP), flush=True)
