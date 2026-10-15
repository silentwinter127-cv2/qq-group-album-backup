"""Full acceptance check: files on disk, DB integrity, and every WebUI endpoint."""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qqalbum.store import Store  # noqa: E402

G = "123456789"
ROOT = r"D:\QQAlbumBackup"
BASE = "http://127.0.0.1:8787"
FAILS = []


def head(title):
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


# ---------------------------------------------------------------- 1. storage
head("1. 存储位置（必须在 D 盘）")
print("db      :", os.path.abspath(os.path.join(ROOT, "qqalbum.sqlite3")))
print("photos  :", os.path.abspath(os.path.join(ROOT, "photos", G)))
print("profile :", os.path.abspath(os.path.join(ROOT, "edge-profile")))
print("all on D:", all(os.path.abspath(p).upper().startswith("D:\\") for p in (
    os.path.join(ROOT, "qqalbum.sqlite3"), os.path.join(ROOT, "photos"),
    os.path.join(ROOT, "edge-profile"))))

# ------------------------------------------------------------------ 2. files
head("2. 图片文件")
files = 0
total = 0
for root, _dirs, names in os.walk(os.path.join(ROOT, "photos", G)):
    for name in names:
        files += 1
        try:
            total += os.path.getsize(os.path.join(root, name))
        except OSError:
            pass
print(f"磁盘文件数: {files}  合计 {total / 1024 ** 3:.2f} GB")
exts = {}
for root, _dirs, names in os.walk(os.path.join(ROOT, "photos", G)):
    for name in names:
        exts[os.path.splitext(name)[1].lower()] = exts.get(os.path.splitext(name)[1].lower(), 0) + 1
print("按扩展名:", dict(sorted(exts.items(), key=lambda kv: -kv[1])))

store = Store(os.path.join(ROOT, "qqalbum.sqlite3"))

# ------------------------------------------------------------- 3. DB checks
head("3. 数据库内容与完整性")
stats = store.stats(G)
print(json.dumps(stats, ensure_ascii=False, indent=2))
checks = [
    ("照片缺上传者", "SELECT COUNT(*) c FROM photos WHERE group_id=? AND (uploader_uin IS NULL OR uploader_uin='')", 0),
    ("照片缺上传时间", "SELECT COUNT(*) c FROM photos WHERE group_id=? AND upload_epoch IS NULL", 0),
    ("照片缺原图地址", "SELECT COUNT(*) c FROM photos WHERE group_id=? AND (raw_url IS NULL OR raw_url='')", 0),
    ("悬空 file_path", "SELECT COUNT(*) c FROM photos WHERE group_id=? AND file_status IN ('done','thumb-only') "
                       "AND (file_path IS NULL OR file_path='')", 0),
    ("未处理照片", "SELECT COUNT(*) c FROM photos WHERE group_id=? AND (file_status IS NULL OR file_status='pending')", 0),
    ("下载失败", "SELECT COUNT(*) c FROM photos WHERE group_id=? AND file_status='error'", 0),
]
for label, sql, expect in checks:
    got = store.query_one(sql.replace("?", "?", 1), (G,))["c"]
    ok = (got == expect)
    print(f"  [{'ok ' if ok else 'FAIL'}] {label:14} = {got} (期望 {expect})")
    if not ok:
        FAILS.append(label)

# file_path really exists on disk (sample)
rows = store.query("SELECT file_path FROM photos WHERE group_id=? AND file_status='done' "
                   "ORDER BY RANDOM() LIMIT 40", (G,))
missing = [r["file_path"] for r in rows if not os.path.isfile(r["file_path"] or "")]
print(f"  [{'ok ' if not missing else 'FAIL'}] 随机 40 个已下载文件存在于磁盘: "
      f"{40 - len(missing)}/40")
if missing:
    FAILS.append("file paths missing on disk")

# no abandoned temp files may remain on disk
parts = []
for root, _dirs, names in os.walk(os.path.join(ROOT, "photos", G)):
    parts += [n for n in names if ".part" in n]
print(f"  [{'ok ' if not parts else 'FAIL'}] 无残留临时下载文件: {len(parts)}")
if parts:
    FAILS.append("leftover .part files")

# file count on disk should match done + thumb-only rows
expected_files = stats["photos_downloaded"] + stats["photos_thumb_only"]
disk_files = sum(len([n for n in names if ".part" not in n])
                 for _r, _d, names in os.walk(os.path.join(ROOT, "photos", G)))
print(f"  [{'ok ' if disk_files >= expected_files else 'FAIL'}] 磁盘文件数 {disk_files} >= "
      f"已完成记录 {expected_files}")
if disk_files < expected_files:
    FAILS.append("file count mismatch")

# ------------------------------------------------------ 4. WebUI endpoints
head("4. WebUI 端点")


def get(path, raw=False):
    try:
        with urllib.request.urlopen(BASE + path, timeout=90) as r:
            body = r.read()
            return r.status, (body if raw else body.decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:200]
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}"


# find a photo with comment text to prove comment search works
sample_comment = store.query_one(
    "SELECT content, batch_id, photo_id FROM comments WHERE group_id=? AND length(content)>3 "
    "ORDER BY COALESCE(comment_epoch,0) DESC LIMIT 1", (G,))
needle = (sample_comment["content"] or "")[:4] if sample_comment else ""

endpoints = [
    ("GET", "/", "QQ 群相册备份"),
    ("GET", "/static/style.css", "--accent"),
    ("GET", "/static/app.js", "openDetail"),
    ("GET", "/api/overview", '"stats"'),
    ("GET", "/api/photos?per_page=3", '"items"'),
    ("GET", "/api/photos?has_comment=1&per_page=2", '"items"'),
    ("GET", "/api/photos?has_like=1&per_page=2", '"items"'),
    ("GET", "/api/photos?downloaded=0&per_page=2", '"items"'),
    ("GET", "/api/photos?downloaded=1&per_page=2", '"items"'),
    ("GET", "/api/photos?downloaded=full&per_page=2", '"items"'),
    ("GET", "/api/photos?thumb_only=1&per_page=2", '"items"'),
    ("GET", "/api/photos?order=comments&per_page=2", '"items"'),
    ("GET", "/api/photos?q=" + urllib.parse.quote(needle), '"items"') if needle else None,
    ("GET", "/api/uploaders", '"items"'),
    ("GET", "/api/top_uploaders", '"items"'),
    ("GET", "/api/tags", '"items"'),
    ("GET", "/api/timeline", '"items"'),
    ("GET", "/api/comments?limit=5", '"items"'),
    ("GET", "/api/gallery?limit=4", '"items"'),
    ("GET", "/api/sync/status", '"stage"'),
    ("GET", "/api/login/status", '"connected"'),
    ("GET", "/api/csv", "photo_id"),
    ("GET", "/api/export", "photo_id"),
]
endpoints = [e for e in endpoints if e]
for method, path, needle_expect in endpoints:
    status, body = get(path)
    ok = status == 200 and needle_expect in body
    print(f"  [{'ok ' if ok else 'FAIL'}] {path[:52]:54} {status} len={len(body)}")
    if not ok:
        FAILS.append(path)

if needle:
    status, body = get("/api/photos?q=" + urllib.parse.quote(needle))
    data = json.loads(body) if status == 200 else {}
    hits = data.get("total", 0)
    ok = hits > 0
    print(f"  [{'ok ' if ok else 'FAIL'}] 按评论内容搜索 “{needle}” -> {hits} 张")
    if not ok:
        FAILS.append("comment full-text search")

# photo detail incl. comments + likes
row = store.query_one("SELECT p.photo_id FROM photos p WHERE p.group_id=? AND p.batch_id IN "
                      "(SELECT batch_id FROM comments WHERE group_id=?) LIMIT 1", (G, G))
if row:
    status, body = get("/api/photo?photo_id=" + urllib.parse.quote(row["photo_id"]))
    detail = json.loads(body)
    ok = status == 200 and len(detail.get("comments", [])) > 0
    print(f"  [{'ok ' if ok else 'FAIL'}] 照片详情含评论: {len(detail.get('comments', []))} 条, "
          f"点赞 {len(detail.get('likes', []))} 人 (总数 {detail.get('like_total')})")
    if not ok:
        FAILS.append("photo detail comments")

# media serving
row = store.query_one("SELECT photo_id, file_path FROM photos WHERE group_id=? AND file_status IN "
                      "('done','thumb-only') LIMIT 1", (G,))
if row:
    for label, path in (("缩略图", f"/api/thumb?photo_id={urllib.parse.quote(row['photo_id'])}&size=200"),
                        ("原图", f"/api/image?photo_id={urllib.parse.quote(row['photo_id'])}"),
                        ("下载", f"/api/file?photo_id={urllib.parse.quote(row['photo_id'])}")):
        status, body = get(path, raw=True)
        ok = status == 200 and body[:2] == b"\xff\xd8" or body[:6] in (b"GIF87a", b"GIF89a")
        print(f"  [{'ok ' if ok else 'FAIL'}] 媒体 {label:4} {status} {len(body)}B 魔数={body[:4]!r}")
        if not ok:
            FAILS.append(f"media {label}")

# album cover
album = store.query_one("SELECT album_id FROM albums WHERE group_id=? LIMIT 1", (G,))
if album:
    status, body = get("/api/cover?album_id=" + urllib.parse.quote(album["album_id"]), raw=True)
    ok = status == 200 and len(body) > 500
    print(f"  [{'ok ' if ok else 'FAIL'}] 相册封面 {status} {len(body)}B")
    if not ok:
        FAILS.append("album cover")

head("结论")
print("失败项:", FAILS or "无")
store.close()
raise SystemExit(1 if FAILS else 0)
