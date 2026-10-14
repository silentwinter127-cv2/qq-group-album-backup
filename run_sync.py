"""Run a real backup sync on the target group and print progress (used for E2E testing)."""
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qqalbum.crawler import BackupManager  # noqa: E402
from qqalbum.qzone import QzoneClient  # noqa: E402
from qqalbum.store import Store  # noqa: E402

GROUP = sys.argv[1] if len(sys.argv) > 1 else "123456789"
ROOT = r"<备份目录>"
DB = os.path.join(ROOT, "qqalbum.sqlite3")
MODE = sys.argv[2] if len(sys.argv) > 2 else "all"

store = Store(DB)
FEED_LIMIT = next((int(a) for a in sys.argv[1:] if a.isdigit() and len(a) > 3), 200000)
manager = BackupManager(GROUP, store, lambda: QzoneClient(GROUP, port=9222), root_dir=ROOT,
                        page_size=300, feed_page_size=10, download_workers=3,
                        max_feeds=FEED_LIMIT)

print("connecting to browser…", flush=True)
session = manager.check_login()
print("session:", json.dumps(session, ensure_ascii=False), flush=True)

opts = {
    "all": dict(albums=True, photos=True, feeds=True, members=True),
    "meta": dict(albums=True, photos=True, feeds=False, members=True),
    "feeds": dict(albums=False, photos=False, feeds=True, members=False),
    "small": dict(albums=True, photos=True, feeds=False, members=True),
}[MODE]

INCREMENTAL = "--full" not in sys.argv
manager.state.add_log = manager.state.add_log  # keep type checkers calm
started = manager.start_sync(incremental=INCREMENTAL, **opts)
print("started:", started, "incremental:", INCREMENTAL, flush=True)

last = ""
while manager.state.running:
    snap = manager.state.snapshot()
    line = (f"{snap['stage']:>8} {snap['current']}/{snap['total']} "
            f"{snap['message'][:70]}")
    if line != last:
        print(line, flush=True)
        last = line
    time.sleep(1.0)

snap = manager.state.snapshot()
print("\nfinal:", json.dumps({k: v for k, v in snap.items() if k != "log"}, ensure_ascii=False), flush=True)
print("\n".join(snap["log"][-25:]), flush=True)
print("stats:", json.dumps(store.stats(GROUP), ensure_ascii=False), flush=True)
print("server_time_offset:", store.get_meta("server_time_offset"), flush=True)
