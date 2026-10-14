"""Print the current AI progress and database state."""
import json
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\path\to\qq-group-album-backup")
from qqalbum.store import Store  # noqa: E402

import os as _os
G = _os.environ.get("QQALBUM_GROUP", "<群号>")
try:
    with urllib.request.urlopen("http://127.0.0.1:8787/api/ai/status", timeout=60) as r:
        status = json.loads(r.read().decode())
except Exception as exc:
    status = {}
    print("web status unavailable:", exc)

for key, label in (("state", "SigLIP 打标"), ("caption_state", "BLIP 描述")):
    st = status.get(key) or {}
    print(f"{label:12} running={st.get('running')} stage={st.get('stage')} "
          f"{st.get('current')}/{st.get('total')} {st.get('rate')}/s "
          f"eta={(st.get('eta') or 0) / 60:.0f}min error={st.get('error')}")

print(f"\n已索引向量 : {status.get('indexed')} / {status.get('local_files')}")
print(f"已生成描述 : {status.get('captioned')}")
print(f"AI 统计    : {json.dumps(status.get('stats'), ensure_ascii=False)}")

store = Store(r"<备份目录>\qqalbum.sqlite3")
print("\n数据库:")
print("  photos        :", store.query_one("SELECT COUNT(*) c FROM photos")["c"])
print("  ai tags rows  :", store.query_one("SELECT COUNT(*) c FROM photo_ai_tags")["c"])
print("  embeddings    :", store.query_one("SELECT COUNT(*) c FROM photo_embedding")["c"])
print("  captions      :", store.query_one("SELECT COUNT(*) c FROM photo_caption")["c"])
print("  caption fts   :", store.query_one("SELECT COUNT(*) c FROM caption_fts")["c"])
db = r"<备份目录>\qqalbum.sqlite3"
import os  # noqa: E402
print(f"  数据库大小    : {os.path.getsize(db) / 1024 ** 3:.2f} GB")
