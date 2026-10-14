"""SQLite storage for the QQ group album backup (full metadata, comments, likes)."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

def default_root() -> str:
    """Where backups live by default.

    $QQALBUM_ROOT wins; otherwise a `backup` folder next to the checkout. Deliberately
    not a fixed drive letter, so a fresh clone works anywhere.
    """
    env = os.environ.get("QQALBUM_ROOT")
    if env:
        return os.path.abspath(env)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "backup")


DEFAULT_DB = os.path.join(default_root(), "qqalbum.sqlite3")

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS groups (
    group_id      TEXT PRIMARY KEY,
    name          TEXT,
    owner_uin     TEXT,
    member_count  INTEGER DEFAULT 0,
    album_count   INTEGER DEFAULT 0,
    photo_count   INTEGER DEFAULT 0,
    created_at    TEXT,
    last_sync_at  TEXT,
    last_sync_epoch INTEGER,
    server_time_offset INTEGER DEFAULT 0,
    raw_json      TEXT
);

CREATE TABLE IF NOT EXISTS members (
    group_id   TEXT NOT NULL,
    uin        TEXT NOT NULL,
    nick       TEXT,
    qun_remark TEXT,
    friend_remark TEXT,
    role       INTEGER DEFAULT 0,
    is_creator INTEGER DEFAULT 0,
    is_manager INTEGER DEFAULT 0,
    raw_json   TEXT,
    updated_at TEXT,
    PRIMARY KEY (group_id, uin)
);

CREATE TABLE IF NOT EXISTS albums (
    group_id     TEXT NOT NULL,
    album_id     TEXT NOT NULL,
    title        TEXT,
    description  TEXT,
    cover_url    TEXT,
    photo_count  INTEGER DEFAULT 0,
    privacy      INTEGER,
    creator_uin  TEXT,
    creator_nick TEXT,
    create_time  TEXT,
    create_epoch INTEGER,
    update_time  TEXT,
    update_epoch INTEGER,
    bwidth       INTEGER,
    bheight      INTEGER,
    cp_x         INTEGER,
    cp_y         INTEGER,
    downloaded   INTEGER DEFAULT 0,
    last_sync_at TEXT,
    raw_json     TEXT,
    PRIMARY KEY (group_id, album_id)
);

CREATE TABLE IF NOT EXISTS photos (
    group_id     TEXT NOT NULL,
    album_id     TEXT NOT NULL,
    photo_id     TEXT NOT NULL,
    lloc         TEXT,
    name         TEXT,
    description  TEXT,
    uploader_uin TEXT,
    uploader_nick TEXT,
    upload_epoch INTEGER,
    upload_time  TEXT,
    shot_time    TEXT,
    batch_id     TEXT,
    batch_num    INTEGER,
    width        INTEGER,
    height       INTEGER,
    original_width  INTEGER,
    original_height INTEGER,
    original_size   INTEGER,
    has_raw      INTEGER DEFAULT 0,
    media_type   INTEGER DEFAULT 1,
    is_video     INTEGER DEFAULT 0,
    is_animated  INTEGER DEFAULT 0,
    video_url    TEXT,
    video_status INTEGER,
    comment_count INTEGER DEFAULT 0,
    like_count   INTEGER DEFAULT 0,
    is_liked     INTEGER DEFAULT 0,
    thumb_url    TEXT,
    big_url      TEXT,
    raw_url      TEXT,
    file_path    TEXT,
    file_size    INTEGER,
    file_status  TEXT DEFAULT 'pending',
    file_error   TEXT,
    downloaded_at TEXT,
    raw_json     TEXT,
    PRIMARY KEY (group_id, photo_id)
);

CREATE INDEX IF NOT EXISTS idx_photos_album   ON photos (group_id, album_id);
CREATE INDEX IF NOT EXISTS idx_photos_uploader ON photos (group_id, uploader_uin);
CREATE INDEX IF NOT EXISTS idx_photos_batch   ON photos (group_id, batch_id);
-- lloc 索引：视频列表要按封面 pickey 关联到照片（videos.lloc = photos.lloc）。
-- 没有它时这个 JOIN 会退化成全表扫描，实测 60 条要 13 秒，加上后 3 毫秒。
CREATE INDEX IF NOT EXISTS idx_photos_lloc    ON photos (group_id, lloc);
CREATE INDEX IF NOT EXISTS idx_photos_time    ON photos (upload_epoch);
CREATE INDEX IF NOT EXISTS idx_photos_status  ON photos (file_status);

CREATE TABLE IF NOT EXISTS feeds (
    group_id      TEXT NOT NULL,
    batch_id      TEXT NOT NULL,
    feed_id       TEXT,
    org_like_key  TEXT,
    feed_epoch    INTEGER,
    feed_time     TEXT,
    uploader_uin  TEXT,
    uploader_nick TEXT,
    album_id      TEXT,
    album_name    TEXT,
    album_desc    TEXT,
    photo_num     INTEGER DEFAULT 0,
    summary       TEXT,
    comment_total INTEGER DEFAULT 0,
    like_total    INTEGER DEFAULT 0,
    is_liked      INTEGER DEFAULT 0,
    busi_param    TEXT,
    raw_json      TEXT,
    crawled_at    TEXT,
    PRIMARY KEY (group_id, batch_id)
);

CREATE INDEX IF NOT EXISTS idx_feeds_album ON feeds (group_id, album_id);
CREATE INDEX IF NOT EXISTS idx_feeds_time  ON feeds (feed_epoch);

CREATE TABLE IF NOT EXISTS feed_photos (
    group_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    pic_key  TEXT NOT NULL,
    position INTEGER,
    raw_url  TEXT,
    width    INTEGER,
    height   INTEGER,
    is_video INTEGER DEFAULT 0,
    PRIMARY KEY (group_id, batch_id, pic_key)
);

CREATE TABLE IF NOT EXISTS comments (
    group_id    TEXT NOT NULL,
    comment_id  TEXT NOT NULL,
    batch_id    TEXT,
    album_id    TEXT,
    photo_id    TEXT,
    parent_id   TEXT,
    content     TEXT,
    author_uin  TEXT,
    author_nick TEXT,
    comment_epoch INTEGER,
    comment_time  TEXT,
    reply_total INTEGER DEFAULT 0,
    is_reply    INTEGER DEFAULT 0,
    source      TEXT,
    raw_json    TEXT,
    PRIMARY KEY (group_id, comment_id, is_reply)
);

CREATE INDEX IF NOT EXISTS idx_comments_batch ON comments (group_id, batch_id);
CREATE INDEX IF NOT EXISTS idx_comments_photo ON comments (group_id, photo_id);
CREATE INDEX IF NOT EXISTS idx_comments_author ON comments (group_id, author_uin);

CREATE TABLE IF NOT EXISTS likes (
    group_id   TEXT NOT NULL,
    batch_id   TEXT NOT NULL,
    uin        TEXT NOT NULL,
    nick       TEXT,
    like_key   TEXT,
    source     TEXT,
    created_at TEXT,
    PRIMARY KEY (group_id, batch_id, uin)
);

CREATE INDEX IF NOT EXISTS idx_likes_uin ON likes (group_id, uin);

CREATE TABLE IF NOT EXISTS tags (
    tag_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT UNIQUE NOT NULL,
    color      TEXT DEFAULT '#1677ff',
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS photo_tags (
    group_id TEXT NOT NULL,
    photo_id TEXT NOT NULL,
    tag_id   INTEGER NOT NULL,
    source   TEXT DEFAULT 'manual',
    PRIMARY KEY (group_id, photo_id, tag_id),
    FOREIGN KEY (tag_id) REFERENCES tags (tag_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS downloads (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id   TEXT,
    photo_id   TEXT,
    album_id   TEXT,
    url        TEXT,
    status     TEXT,
    bytes      INTEGER,
    error      TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_downloads_photo ON downloads (group_id, photo_id);

CREATE TABLE IF NOT EXISTS sync_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id   TEXT,
    album_id   TEXT,
    stage      TEXT,
    message    TEXT,
    created_at TEXT
);

-- ------------------------------------------------------------------ videos
-- 群相册里的视频。
--
-- 为什么不能塞进 photos 表：相册网页接口（qun_list_photo_v2）**不返回视频地址**，
-- 我们只能把视频的封面图当普通照片存下来（btype=1）。真正的播放地址来自
-- 机器人引擎（SnowLuma）的 get_group_album_media_list，字段结构也完全不同
-- （video.id / video.url / video.cover / 时长），所以单开一张表。
--
-- ⚠️ video_url 是**带签名且会过期**的（dis_k/dis_t），所以入库的地址只当参考，
-- 真正下载时必须重新调接口换一条新的。
CREATE TABLE IF NOT EXISTS videos (
    group_id     TEXT NOT NULL,
    video_id     TEXT NOT NULL,
    album_id     TEXT,
    album_title  TEXT,
    lloc         TEXT,            -- 与 photos.lloc 对齐用（同一张封面）
    photo_id     TEXT,            -- 匹配到 photos 时的 photo_id
    uploader_uin TEXT,
    uploader_nick TEXT,
    upload_epoch INTEGER,
    upload_time  TEXT,
    width        INTEGER,
    height       INTEGER,
    duration_ms  INTEGER,
    cover_url    TEXT,
    video_url    TEXT,
    file_status  TEXT,            -- NULL/pending | done | failed | skipped
    file_path    TEXT,
    file_size    INTEGER,
    file_error   TEXT,
    downloaded_at TEXT,
    raw_json     TEXT,
    crawled_at   TEXT,
    PRIMARY KEY (group_id, video_id)
);
CREATE INDEX IF NOT EXISTS idx_videos_album  ON videos (group_id, album_id);
CREATE INDEX IF NOT EXISTS idx_videos_status ON videos (group_id, file_status);
CREATE INDEX IF NOT EXISTS idx_videos_lloc   ON videos (group_id, lloc);

CREATE VIRTUAL TABLE IF NOT EXISTS photo_fts USING fts5(
    photo_id UNINDEXED,
    group_id UNINDEXED,
    content,
    uploader,
    album,
    tokenize='unicode61'
);

-- ---------------------------------------------------------------- AI tagging
CREATE TABLE IF NOT EXISTS model_tags (
    tag_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL,
    name_en  TEXT,
    kind     TEXT DEFAULT 'vocab',     -- vocab | free
    color    TEXT DEFAULT '#7d5cff',
    UNIQUE (name)
);

CREATE TABLE IF NOT EXISTS photo_ai_tags (
    group_id   TEXT NOT NULL,
    photo_id   TEXT NOT NULL,
    tag_id     INTEGER NOT NULL,
    score      REAL,                   -- cosine similarity with the tag prompt
    confidence REAL,                   -- 0..1 calibrated, relative within a photo
    rank       INTEGER,
    model      TEXT,
    tagged_at  TEXT,
    PRIMARY KEY (group_id, photo_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_ai_tags_tag   ON photo_ai_tags (tag_id);
CREATE INDEX IF NOT EXISTS idx_ai_tags_photo ON photo_ai_tags (group_id, photo_id);
CREATE INDEX IF NOT EXISTS idx_ai_tags_score ON photo_ai_tags (score);

-- one 768-d normalized float32 vector per photo (little-endian bytes)
CREATE TABLE IF NOT EXISTS photo_embedding (
    group_id  TEXT NOT NULL,
    photo_id  TEXT NOT NULL,
    model     TEXT,
    dim       INTEGER,
    vector    BLOB,
    created_at TEXT,
    PRIMARY KEY (group_id, photo_id)
);

-- BLIP captions (English) + the translated/expanded text used for text search
CREATE TABLE IF NOT EXISTS photo_caption (
    group_id     TEXT NOT NULL,
    photo_id     TEXT NOT NULL,
    caption      TEXT,
    caption_en   TEXT,
    model        TEXT,
    source       TEXT DEFAULT 'blip',
    created_at   TEXT,
    PRIMARY KEY (group_id, photo_id)
);
CREATE INDEX IF NOT EXISTS idx_caption_photo ON photo_caption (group_id, photo_id);

-- full-text index over captions so Chinese/English keywords can be matched
CREATE VIRTUAL TABLE IF NOT EXISTS caption_fts USING fts5(
    photo_id UNINDEXED,
    group_id UNINDEXED,
    body,
    tokenize='unicode61'
);
"""


def now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def epoch_to_text(epoch: Optional[int], offset: int = 0) -> Optional[str]:
    if not epoch:
        return None
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(epoch) + offset))
    except (OverflowError, OSError, ValueError):
        return None


def parse_date_arg(value: Any, end_of_day: bool = False) -> Optional[int]:
    """Parse a user supplied date/datetime/epoch into an epoch integer.

    Accepts `2025-01-31`, `2025/01/31 12:30`, `2025-01-31T12:30:00` or a raw epoch.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace("T", " ")
    if not text:
        return None
    if text.lstrip("-").isdigit():
        return int(text)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            parsed = time.strptime(text, fmt)
        except ValueError:
            continue
        try:
            epoch = int(time.mktime(parsed))
        except (OverflowError, OSError, ValueError):
            # pre-1970 dates make mktime raise on Windows; fall back to UTC
            import calendar  # noqa: PLC0415
            epoch = int(calendar.timegm(parsed))
        if end_of_day and fmt in ("%Y-%m-%d", "%Y/%m/%d"):
            epoch += 86399
        return epoch
    return None


class Store:
    """Thread-safe-ish SQLite wrapper (one connection per thread)."""

    def __init__(self, path: str = DEFAULT_DB):
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        with self._init_lock:
            conn = self.connection
            conn.executescript(SCHEMA)
            conn.commit()
            self._migrate(conn)
            self._set_meta("schema_version", str(SCHEMA_VERSION))

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add columns introduced after the first release (plain ADD COLUMN)."""
        wanted = {
            "photos": {
                "media_type": "INTEGER DEFAULT 1",
                "is_animated": "INTEGER DEFAULT 0",
            },
        }
        for table, columns in wanted.items():
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        conn.commit()

    # ----------------------------------------------------------- connection
    @property
    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self.connection.execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
        return list(self.connection.execute(sql, params))

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def commit(self) -> None:
        self.connection.commit()

    # ----------------------------------------------------------------- meta
    def _set_meta(self, key: str, value: str) -> None:
        self.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self.commit()

    def set_meta(self, key: str, value: Any) -> None:
        self._set_meta(key, json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value)

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else default

    # ---------------------------------------------------------------- group
    def upsert_group(self, group_id: str, **fields: Any) -> None:
        data = dict(fields)
        data["group_id"] = str(group_id)
        data.setdefault("last_sync_at", now_iso())
        data["last_sync_epoch"] = int(time.time())
        columns = ", ".join(data.keys())
        placeholders = ", ".join("?" for _ in data)
        updates = ", ".join(f"{k}=excluded.{k}" for k in data if k != "group_id")
        self.execute(f"INSERT INTO groups ({columns}) VALUES ({placeholders}) "
                     f"ON CONFLICT(group_id) DO UPDATE SET {updates}", list(data.values()))
        self.commit()

    def get_group(self, group_id: str) -> Optional[sqlite3.Row]:
        return self.query_one("SELECT * FROM groups WHERE group_id=?", (str(group_id),))

    # -------------------------------------------------------------- members
    def upsert_members(self, group_id: str, members: Iterable[Dict[str, Any]]) -> int:
        rows = []
        for m in members:
            uin = str(m.get("uin") or m.get("id") or "")
            if not uin:
                continue
            rows.append((str(group_id), uin, m.get("nick") or m.get("nickname") or "",
                         m.get("qun_remark") or "", m.get("firend_remark") or m.get("friend_remark") or "",
                         int(m.get("role") or 0), int(bool(m.get("iscreator") or m.get("is_creator"))),
                         int(bool(m.get("ismanager") or m.get("is_manager"))),
                         json.dumps(m, ensure_ascii=False), now_iso()))
        if not rows:
            return 0
        self.connection.executemany(
            "INSERT INTO members (group_id, uin, nick, qun_remark, friend_remark, role, is_creator, "
            "is_manager, raw_json, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(group_id, uin) DO UPDATE SET nick=excluded.nick, qun_remark=excluded.qun_remark, "
            "friend_remark=excluded.friend_remark, role=excluded.role, is_creator=excluded.is_creator, "
            "is_manager=excluded.is_manager, raw_json=excluded.raw_json, updated_at=excluded.updated_at", rows)
        self.commit()
        return len(rows)

    def member_names(self, group_id: str) -> Dict[str, str]:
        return {r["uin"]: r["nick"] for r in
                self.query("SELECT uin, nick FROM members WHERE group_id=?", (str(group_id),))}

    # --------------------------------------------------------------- albums
    def upsert_album(self, group_id: str, album: Dict[str, Any], server_offset: int = 0) -> None:
        create_epoch = None
        for key in ("createtime", "create_time"):
            if album.get(key):
                create_epoch = self._parse_epoch(album[key]) or create_epoch
        update_epoch = None
        for key in ("updatetime", "modifytime", "update_time"):
            if album.get(key):
                update_epoch = self._parse_epoch(album[key]) or update_epoch
        self.execute(
            "INSERT INTO albums (group_id, album_id, title, description, cover_url, photo_count, privacy, "
            "creator_uin, creator_nick, create_time, create_epoch, update_time, update_epoch, bwidth, bheight, "
            "cp_x, cp_y, last_sync_at, raw_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(group_id, album_id) DO UPDATE SET title=excluded.title, description=excluded.description, "
            "cover_url=excluded.cover_url, photo_count=excluded.photo_count, privacy=excluded.privacy, "
            "creator_uin=excluded.creator_uin, creator_nick=excluded.creator_nick, create_time=excluded.create_time, "
            "create_epoch=excluded.create_epoch, update_time=excluded.update_time, update_epoch=excluded.update_epoch, "
            "bwidth=excluded.bwidth, bheight=excluded.bheight, cp_x=excluded.cp_x, cp_y=excluded.cp_y, "
            "last_sync_at=excluded.last_sync_at, raw_json=excluded.raw_json",
            (str(group_id), str(album.get("id") or album.get("albumid") or ""),
             album.get("title") or album.get("name") or "", album.get("desc") or album.get("description") or "",
             album.get("coverurl") or album.get("cover_url") or "",
             int(album.get("photocnt") or album.get("photo_count") or 0),
             int(album.get("privacy") or 0) if str(album.get("privacy") or "0").lstrip("-").isdigit() else None,
             str(album.get("createuin") or album.get("creator_uin") or ""),
             album.get("createnickname") or "", epoch_to_text(create_epoch), create_epoch,
             epoch_to_text(update_epoch), update_epoch,
             album.get("bwidth"), album.get("bheight"), album.get("cp_x"), album.get("cp_y"),
             now_iso(), json.dumps(album, ensure_ascii=False)))
        self.commit()

    @staticmethod
    def _parse_epoch(value: Any) -> Optional[int]:
        if value is None or value == "":
            return None
        if isinstance(value, (int, float)):
            return int(value)
        text = str(value).strip()
        if text.isdigit():
            return int(text)
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S"):
            try:
                return int(time.mktime(time.strptime(text, fmt)))
            except ValueError:
                continue
        return None

    # --------------------------------------------------------------- photos
    def upsert_photos(self, group_id: str, photos: Iterable[Dict[str, Any]],
                      names: Optional[Dict[str, str]] = None,
                      server_offset: int = 0, commit: bool = True) -> int:
        names = names or {}
        rows = []
        for p in photos:
            photo_id = str(p.get("id") or p.get("lloc") or "")
            if not photo_id:
                continue
            album_id = str(p.get("_album_id") or p.get("albumId") or p.get("album_id") or "")
            upload_epoch = self._parse_epoch(p.get("uploadtime") or p.get("upload_time"))
            uploader = str(p.get("owner") or p.get("uploader") or "")
            raw_url = self._raw_photo_url(p)
            # QZone btype: 1 = 照片, 2 = 动图(GIF), 3+ = 其它（含视频）
            btype = int(p.get("btype") or 1)
            video_url = (p.get("videodata") or {}).get("videourl") or ""
            is_video = bool(video_url)
            is_animated = btype == 2
            rows.append((
                str(group_id), album_id, photo_id, p.get("lloc") or photo_id,
                p.get("name") or "", p.get("desc") or p.get("description") or "",
                uploader, p.get("ownername") or names.get(uploader, ""),
                upload_epoch, epoch_to_text(upload_epoch),
                epoch_to_text(self._parse_epoch(p.get("shottime") or p.get("shot_time"))),
                str(p.get("batchid") or ""), int(p.get("batchNum") or p.get("batchnum") or 0) or None,
                int(p.get("bwidth") or 0) or None, int(p.get("bheight") or 0) or None,
                int(p.get("origin_width") or 0) or None, int(p.get("origin_height") or 0) or None,
                int(p.get("origin_size") or 0) or None,
                int(p.get("hasraw") or 0), btype, int(is_video), int(is_animated),
                video_url,
                int((p.get("videodata") or {}).get("videostatus") or 0),
                int(p.get("commentcnt") or 0), int(p.get("likecnt") or 0), int(p.get("isliked") or 0),
                p.get("burl") or "", (p.get("burl") or "").replace("/200/", "/800/"), raw_url,
                json.dumps(p, ensure_ascii=False)))
        if not rows:
            return 0
        self.connection.executemany(
            "INSERT INTO photos (group_id, album_id, photo_id, lloc, name, description, uploader_uin, "
            "uploader_nick, upload_epoch, upload_time, shot_time, batch_id, batch_num, width, height, "
            "original_width, original_height, original_size, has_raw, media_type, is_video, is_animated, "
            "video_url, video_status, comment_count, like_count, is_liked, thumb_url, big_url, raw_url, "
            "raw_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(group_id, photo_id) DO UPDATE SET album_id=excluded.album_id, lloc=excluded.lloc, "
            "name=excluded.name, description=excluded.description, uploader_uin=excluded.uploader_uin, "
            "uploader_nick=excluded.uploader_nick, upload_epoch=excluded.upload_epoch, upload_time=excluded.upload_time, "
            "shot_time=excluded.shot_time, batch_id=excluded.batch_id, batch_num=excluded.batch_num, "
            "width=excluded.width, height=excluded.height, original_width=excluded.original_width, "
            "original_height=excluded.original_height, original_size=excluded.original_size, has_raw=excluded.has_raw, "
            "media_type=excluded.media_type, is_video=excluded.is_video, is_animated=excluded.is_animated, "
            "video_url=excluded.video_url, video_status=excluded.video_status, "
            "comment_count=excluded.comment_count, like_count=excluded.like_count, is_liked=excluded.is_liked, "
            "thumb_url=excluded.thumb_url, big_url=excluded.big_url, raw_url=excluded.raw_url, raw_json=excluded.raw_json",
            rows)
        if commit:
            self.commit()
        return len(rows)

    @staticmethod
    def _raw_photo_url(p: Dict[str, Any]) -> str:
        """Build the best-known original image URL for a photo."""
        for key in ("rawurl", "raw_url", "origin_url", "originurl"):
            if p.get(key):
                return str(p[key])
        burl = p.get("burl") or ""
        if not burl:
            return ""
        base = burl.split("?")[0]
        for size in ("/200", "/800", "/0"):
            if base.endswith(size):
                base = base[: -len(size)]
                break
        return base + "/0"

    def photos_missing_files(self, group_id: str, album_id: Optional[str] = None,
                             only_original: bool = True, limit: int = 0) -> List[sqlite3.Row]:
        sql = ("SELECT * FROM photos WHERE group_id=? AND (file_status IS NULL OR file_status NOT IN "
               "('done','skipped','thumb-only'))")
        params: List[Any] = [str(group_id)]
        if album_id:
            sql += " AND album_id=?"
            params.append(str(album_id))
        if only_original:
            sql += " AND is_video=0"
        sql += " ORDER BY upload_epoch DESC, photo_id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.query(sql, params)

    def set_photo_file(self, group_id: str, photo_id: str, status: str, path: Optional[str] = None,
                       size: Optional[int] = None, error: Optional[str] = None) -> None:
        self.execute("UPDATE photos SET file_status=?, file_path=COALESCE(?, file_path), "
                     "file_size=COALESCE(?, file_size), file_error=?, downloaded_at=? "
                     "WHERE group_id=? AND photo_id=?",
                     (status, path, size, error, now_iso(), str(group_id), str(photo_id)))
        self.commit()

    def log_download(self, group_id: str, photo_id: str, album_id: str, url: str,
                     status: str, size: int = 0, error: str = "") -> None:
        self.execute("INSERT INTO downloads (group_id, photo_id, album_id, url, status, bytes, error, created_at) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     (str(group_id), str(photo_id), str(album_id), url, status, size, error, now_iso()))
        self.commit()

    # --------------------------------------------------------------- videos
    def upsert_videos(self, group_id: str, videos: Iterable[Dict[str, Any]]) -> int:
        """写入/更新视频元数据（来自 get_group_album_media_list）。

        只更新元数据字段：**不碰 file_status / file_path**，否则每次重新抓取元数据
        都会把「已下载」标记抹掉，增量就永远重下。
        """
        rows = []
        for video in videos:
            video_id = str(video.get("video_id") or "").strip()
            if not video_id:
                continue
            rows.append((
                str(group_id), video_id,
                str(video.get("album_id") or ""), str(video.get("album_title") or ""),
                str(video.get("lloc") or ""), str(video.get("photo_id") or "") or None,
                str(video.get("uploader_uin") or ""), str(video.get("uploader_nick") or ""),
                int(video.get("upload_epoch") or 0) or None, str(video.get("upload_time") or ""),
                int(video.get("width") or 0) or None, int(video.get("height") or 0) or None,
                int(video.get("duration_ms") or 0) or None,
                str(video.get("cover_url") or ""), str(video.get("video_url") or ""),
                json.dumps(video.get("raw"), ensure_ascii=False) if video.get("raw") is not None else None,
                now_iso(),
            ))
        if not rows:
            return 0
        self.connection.executemany(
            "INSERT INTO videos (group_id, video_id, album_id, album_title, lloc, photo_id, "
            "uploader_uin, uploader_nick, upload_epoch, upload_time, width, height, duration_ms, "
            "cover_url, video_url, raw_json, crawled_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(group_id, video_id) DO UPDATE SET "
            "album_id=excluded.album_id, album_title=excluded.album_title, "
            "lloc=COALESCE(NULLIF(excluded.lloc,''), videos.lloc), "
            "photo_id=COALESCE(excluded.photo_id, videos.photo_id), "
            "uploader_uin=COALESCE(NULLIF(excluded.uploader_uin,''), videos.uploader_uin), "
            "uploader_nick=COALESCE(NULLIF(excluded.uploader_nick,''), videos.uploader_nick), "
            "upload_epoch=COALESCE(excluded.upload_epoch, videos.upload_epoch), "
            "upload_time=COALESCE(NULLIF(excluded.upload_time,''), videos.upload_time), "
            "width=COALESCE(excluded.width, videos.width), "
            "height=COALESCE(excluded.height, videos.height), "
            "duration_ms=COALESCE(excluded.duration_ms, videos.duration_ms), "
            "cover_url=COALESCE(NULLIF(excluded.cover_url,''), videos.cover_url), "
            "video_url=COALESCE(NULLIF(excluded.video_url,''), videos.video_url), "
            "raw_json=COALESCE(excluded.raw_json, videos.raw_json), crawled_at=excluded.crawled_at",
            rows)
        self.commit()
        return len(rows)

    def videos_missing_files(self, group_id: str, album_id: Optional[str] = None,
                             limit: int = 0) -> List[sqlite3.Row]:
        sql = ("SELECT * FROM videos WHERE group_id=? "
               "AND (file_status IS NULL OR file_status NOT IN ('done','skipped'))")
        params: List[Any] = [str(group_id)]
        if album_id:
            sql += " AND album_id=?"
            params.append(str(album_id))
        sql += " ORDER BY upload_epoch DESC, video_id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.query(sql, params)

    def set_video_file(self, group_id: str, video_id: str, status: str,
                       path: Optional[str] = None, size: Optional[int] = None,
                       error: Optional[str] = None) -> None:
        self.execute("UPDATE videos SET file_status=?, file_path=COALESCE(?, file_path), "
                     "file_size=COALESCE(?, file_size), file_error=?, downloaded_at=? "
                     "WHERE group_id=? AND video_id=?",
                     (status, path, size, error, now_iso(), str(group_id), str(video_id)))
        self.commit()

    def set_video_url(self, group_id: str, video_id: str, url: str) -> None:
        """存下最新拿到的播放地址（它会过期，仅作参考/排错用）。"""
        self.execute("UPDATE videos SET video_url=? WHERE group_id=? AND video_id=?",
                     (str(url or ""), str(group_id), str(video_id)))
        self.commit()

    def video_count(self, group_id: str) -> int:
        row = self.query_one("SELECT COUNT(*) AS c FROM videos WHERE group_id=?", (str(group_id),))
        return int(row["c"] if row else 0)

    def video_stats(self, group_id: str) -> Dict[str, int]:
        row = self.query_one(
            "SELECT COUNT(*) AS total, "
            "  SUM(CASE WHEN file_status='done' THEN 1 ELSE 0 END) AS done, "
            "  SUM(CASE WHEN file_status='failed' THEN 1 ELSE 0 END) AS failed, "
            "  SUM(CASE WHEN file_status IS NULL OR file_status='pending' THEN 1 ELSE 0 END) AS pending, "
            "  COALESCE(SUM(file_size),0) AS bytes "
            "FROM videos WHERE group_id=?", (str(group_id),))
        if not row:
            return {"total": 0, "done": 0, "failed": 0, "pending": 0, "bytes": 0}
        return {k: int(row[k] or 0) for k in ("total", "done", "failed", "pending", "bytes")}

    def videos(self, group_id: str, album_id: Optional[str] = None,
               downloaded: Optional[str] = None, page: int = 1, per_page: int = 60,
               ) -> Dict[str, Any]:
        """视频列表（供 WebUI 展示）。downloaded: '1' 已下载 / '0' 未下载 / 空=全部。"""
        where = ["v.group_id=?"]
        params: List[Any] = [str(group_id)]
        if album_id:
            where.append("v.album_id=?")
            params.append(str(album_id))
        if downloaded == "1":
            where.append("v.file_status='done'")
        elif downloaded == "0":
            where.append("(v.file_status IS NULL OR v.file_status<>'done')")
        clause = " AND ".join(where)
        total = int(self.query_one(
            f"SELECT COUNT(*) AS c FROM videos v WHERE {clause}", params)["c"])
        per_page = max(1, min(200, int(per_page or 60)))
        page = max(1, int(page or 1))
        rows = self.query(
            f"SELECT v.*, a.title AS album_title2, p.file_path AS cover_file, "
            f"       p.file_status AS cover_status "
            f"FROM videos v "
            f"LEFT JOIN albums a ON a.group_id=v.group_id AND a.album_id=v.album_id "
            f"LEFT JOIN photos p ON p.group_id=v.group_id AND p.lloc=v.lloc "
            f"WHERE {clause} "
            f"ORDER BY v.upload_epoch DESC, v.video_id LIMIT ? OFFSET ?",
            [*params, per_page, (page - 1) * per_page])
        items = []
        for row in rows:
            item = dict(row)
            item.pop("raw_json", None)
            item["album_title"] = item.get("album_title") or item.get("album_title2") or ""
            items.append(item)
        return {"total": total, "page": page, "per_page": per_page, "items": items}

    def link_videos_to_photos(self, group_id: str) -> int:
        """按 lloc 把视频和它自己的封面照片对上（photos.lloc 与 video 的 cover.lloc 同源）。"""
        cur = self.execute(
            "UPDATE videos SET photo_id = ("
            "  SELECT p.photo_id FROM photos p "
            "  WHERE p.group_id=videos.group_id AND p.lloc=videos.lloc LIMIT 1"
            ") WHERE group_id=? AND photo_id IS NULL AND lloc<>'' "
            "AND EXISTS (SELECT 1 FROM photos p WHERE p.group_id=videos.group_id AND p.lloc=videos.lloc)",
            (str(group_id),))
        self.commit()
        return cur.rowcount or 0

    # ---------------------------------------------------------------- feeds
    def upsert_feed(self, group_id: str, feed: Dict[str, Any], server_offset: int = 0) -> None:
        batch_id = str(feed.get("_batch_id") or feed.get("fid") or "")
        if not batch_id:
            return
        album = feed.get("album") or {}
        user = feed.get("user") or {}
        epoch = int(feed.get("time") or 0)
        self.execute(
            "INSERT INTO feeds (group_id, batch_id, feed_id, org_like_key, feed_epoch, feed_time, "
            "uploader_uin, uploader_nick, album_id, album_name, album_desc, photo_num, summary, "
            "comment_total, like_total, is_liked, busi_param, raw_json, crawled_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(group_id, batch_id) DO UPDATE SET org_like_key=excluded.org_like_key, "
            "feed_epoch=excluded.feed_epoch, feed_time=excluded.feed_time, uploader_uin=excluded.uploader_uin, "
            "uploader_nick=excluded.uploader_nick, album_id=excluded.album_id, album_name=excluded.album_name, "
            "album_desc=excluded.album_desc, photo_num=excluded.photo_num, summary=excluded.summary, "
            "comment_total=excluded.comment_total, like_total=excluded.like_total, is_liked=excluded.is_liked, "
            "busi_param=excluded.busi_param, raw_json=excluded.raw_json, crawled_at=excluded.crawled_at",
            (str(group_id), batch_id, str(feed.get("fid") or ""), feed.get("org_like_key") or "",
             epoch, epoch_to_text(epoch, server_offset), str(user.get("uin") or ""), user.get("nick") or "",
             str(album.get("album_id") or ""), album.get("album_name") or "", album.get("album_desc") or "",
             int(album.get("photo_num") or 0), feed.get("summary") or "",
             int(feed.get("comment_total") or 0), int(feed.get("like_total") or 0),
             int(feed.get("is_liked") or 0), json.dumps(feed.get("busi_param") or {}, ensure_ascii=False,),
             json.dumps(feed.get("_raw") or feed, ensure_ascii=False), now_iso()))
        self.commit()

    def upsert_feed_photos(self, group_id: str, batch_id: str, pics: Iterable[Dict[str, Any]]) -> None:
        rows = []
        for i, pic in enumerate(pics or []):
            key = str(pic.get("pic_key") or "")
            if not key:
                continue
            rows.append((str(group_id), str(batch_id), key, i, pic.get("url") or "",
                         pic.get("width"), pic.get("height"), int(pic.get("videoflag") or 0)))
        if not rows:
            return
        self.connection.executemany(
            "INSERT INTO feed_photos (group_id, batch_id, pic_key, position, raw_url, width, height, is_video) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(group_id, batch_id, pic_key) DO UPDATE SET "
            "position=excluded.position, raw_url=excluded.raw_url, width=excluded.width, height=excluded.height, "
            "is_video=excluded.is_video", rows)
        self.commit()

    def link_photos_to_feeds(self, group_id: str) -> int:
        cur = self.execute(
            "UPDATE photos SET comment_count = COALESCE((SELECT f.comment_total FROM feeds f "
            "  WHERE f.group_id=photos.group_id AND f.batch_id=photos.batch_id), comment_count), "
            "like_count = COALESCE((SELECT f.like_total FROM feeds f "
            "  WHERE f.group_id=photos.group_id AND f.batch_id=photos.batch_id), like_count), "
            "is_liked = COALESCE((SELECT f.is_liked FROM feeds f "
            "  WHERE f.group_id=photos.group_id AND f.batch_id=photos.batch_id), is_liked) "
            "WHERE group_id=? AND batch_id IS NOT NULL AND batch_id<>''", (str(group_id),))
        self.commit()
        return cur.rowcount or 0

    # ------------------------------------------------------------- comments
    def upsert_comments(self, group_id: str, comments: Iterable[Dict[str, Any]],
                        server_offset: int = 0, commit: bool = True) -> int:
        rows = []
        for c in comments:
            cid = str(c.get("id") or c.get("comment_id") or "")
            if not cid:
                continue
            epoch = int(c.get("time") or 0)
            rows.append((str(group_id), cid, str(c.get("_batch_id") or c.get("batch_id") or ""),
                         str(c.get("_album_id") or c.get("album_id") or ""),
                         str(c.get("_photo_id") or c.get("photo_id") or ""),
                         str(c.get("_parent_id") or c.get("parent_id") or ""),
                         c.get("content") or "", str(c.get("uin") or c.get("author_uin") or ""),
                         c.get("nick") or c.get("author_nick") or "", epoch,
                         epoch_to_text(epoch, server_offset), int(c.get("reply_total") or 0),
                         int(c.get("is_reply") or 0), c.get("source") or "feed",
                         json.dumps(c, ensure_ascii=False)))
        if not rows:
            return 0
        self.connection.executemany(
            "INSERT INTO comments (group_id, comment_id, batch_id, album_id, photo_id, parent_id, content, "
            "author_uin, author_nick, comment_epoch, comment_time, reply_total, is_reply, source, raw_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(group_id, comment_id, is_reply) DO UPDATE SET batch_id=excluded.batch_id, "
            "album_id=excluded.album_id, photo_id=excluded.photo_id, parent_id=excluded.parent_id, "
            "content=excluded.content, author_uin=excluded.author_uin, author_nick=excluded.author_nick, "
            "comment_epoch=excluded.comment_epoch, comment_time=excluded.comment_time, "
            "reply_total=excluded.reply_total, source=excluded.source, raw_json=excluded.raw_json", rows)
        if commit:
            self.commit()
        return len(rows)

    def upsert_likes(self, group_id: str, batch_id: str, likes: Iterable[Dict[str, Any]],
                     like_key: str = "", commit: bool = True) -> int:
        rows = []
        for like in likes or []:
            uin = str(like.get("uin") or "")
            if not uin:
                continue
            rows.append((str(group_id), str(batch_id), uin, like.get("nick") or "",
                         like.get("id") or like_key or "", "feed", now_iso()))
        if not rows:
            return 0
        self.connection.executemany(
            "INSERT INTO likes (group_id, batch_id, uin, nick, like_key, source, created_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(group_id, batch_id, uin) DO UPDATE SET "
            "nick=excluded.nick, like_key=excluded.like_key, source=excluded.source", rows)
        if commit:
            self.commit()
        return len(rows)

    # ---------------------------------------------------------------- search
    def rebuild_fts(self, group_id: str) -> None:
        self.execute("DELETE FROM photo_fts WHERE group_id=?", (str(group_id),))
        self.execute(
            "INSERT INTO photo_fts (photo_id, group_id, content, uploader, album) "
            "SELECT p.photo_id, p.group_id, "
            "COALESCE(p.description,'') || ' ' || COALESCE(p.name,'') || ' ' || COALESCE(p.uploader_nick,'') "
            "|| ' ' || COALESCE(p.raw_json,''), "
            "COALESCE(p.uploader_nick,''), COALESCE(a.title,'') "
            "FROM photos p LEFT JOIN albums a ON a.group_id=p.group_id AND a.album_id=p.album_id "
            "WHERE p.group_id=?", (str(group_id),))
        self.commit()

    # ----------------------------------------------------------------- stats
    def stats(self, group_id: str) -> Dict[str, Any]:
        def one(sql: str, params: Sequence[Any] = ()) -> Any:
            row = self.query_one(sql, params)
            return list(row)[0] if row else 0

        gid = str(group_id)
        return {
            "albums": one("SELECT COUNT(*) FROM albums WHERE group_id=?", (gid,)),
            "photos": one("SELECT COUNT(*) FROM photos WHERE group_id=?", (gid,)),
            "photos_downloaded": one("SELECT COUNT(*) FROM photos WHERE group_id=? AND file_status='done'", (gid,)),
            "photos_thumb_only": one("SELECT COUNT(*) FROM photos WHERE group_id=? AND file_status='thumb-only'", (gid,)),
            "photos_failed": one("SELECT COUNT(*) FROM photos WHERE group_id=? AND file_status='error'", (gid,)),
            "downloaded_bytes": one("SELECT COALESCE(SUM(file_size),0) FROM photos WHERE group_id=? AND file_status='done'", (gid,)),
            "feeds": one("SELECT COUNT(*) FROM feeds WHERE group_id=?", (gid,)),
            "comments": one("SELECT COUNT(*) FROM comments WHERE group_id=?", (gid,)),
            "likes": one("SELECT COUNT(*) FROM likes WHERE group_id=?", (gid,)),
            "members": one("SELECT COUNT(*) FROM members WHERE group_id=?", (gid,)),
            "uploaders": one("SELECT COUNT(DISTINCT uploader_uin) FROM photos WHERE group_id=?", (gid,)),
            "earliest": one("SELECT MIN(upload_epoch) FROM photos WHERE group_id=?", (gid,)),
            "latest": one("SELECT MAX(upload_epoch) FROM photos WHERE group_id=?", (gid,)),
        }

    # ------------------------------------------------------------------ tags
    def ensure_tag(self, name: str, color: str = "#1677ff") -> int:
        self.execute("INSERT INTO tags (name, color, created_at) VALUES (?,?,?) "
                     "ON CONFLICT(name) DO UPDATE SET name=excluded.name", (name, color, now_iso()))
        self.commit()
        row = self.query_one("SELECT tag_id FROM tags WHERE name=?", (name,))
        return int(row["tag_id"])

    # --------------------------------------------------------------- AI tags
    def ensure_model_tag(self, name: str, name_en: str = "", color: str = "#7d5cff",
                         kind: str = "vocab") -> int:
        self.execute(
            "INSERT INTO model_tags (name, name_en, kind, color) VALUES (?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET name_en=excluded.name_en, color=excluded.color",
            (name, name_en, kind, color))
        self.commit()
        row = self.query_one("SELECT tag_id FROM model_tags WHERE name=?", (name,))
        return int(row["tag_id"])

    def model_tag_ids(self, names: Sequence[str]) -> Dict[str, int]:
        """Look up tag ids for a vocabulary (rows must already exist)."""
        out: Dict[str, int] = {}
        for row in self.query("SELECT tag_id, name FROM model_tags"):
            out[row["name"]] = int(row["tag_id"])
        return {n: out[n] for n in names if n in out}

    def clear_ai_tags(self, group_id: str, photo_ids: Optional[Sequence[str]] = None) -> int:
        if photo_ids:
            marks = ",".join("?" for _ in photo_ids)
            cur = self.execute(f"DELETE FROM photo_ai_tags WHERE group_id=? AND photo_id IN ({marks})",
                               [str(group_id), *[str(p) for p in photo_ids]])
        else:
            cur = self.execute("DELETE FROM photo_ai_tags WHERE group_id=?", (str(group_id),))
        self.commit()
        return cur.rowcount or 0

    def upsert_ai_tags(self, group_id: str, photo_id: str, entries: Sequence[Dict[str, Any]],
                       model: str = "", commit: bool = True) -> int:
        rows = [(str(group_id), str(photo_id), int(e["tag_id"]), float(e.get("score") or 0.0),
                 float(e.get("confidence") or 0.0), int(e.get("rank") or 0), model, now_iso())
                for e in entries if e.get("tag_id")]
        if not rows:
            return 0
        self.connection.executemany(
            "INSERT INTO photo_ai_tags (group_id, photo_id, tag_id, score, confidence, rank, model, tagged_at) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(group_id, photo_id, tag_id) DO UPDATE SET "
            "score=excluded.score, confidence=excluded.confidence, rank=excluded.rank, "
            "model=excluded.model, tagged_at=excluded.tagged_at", rows)
        if commit:
            self.commit()
        return len(rows)

    def ai_tags_of_photo(self, group_id: str, photo_id: str, limit: int = 0,
                         min_confidence: float = 0.0) -> List[sqlite3.Row]:
        sql = ("SELECT t.tag_id, t.name, t.name_en, t.color, a.score, a.confidence, a.rank "
               "FROM photo_ai_tags a JOIN model_tags t ON t.tag_id=a.tag_id "
               "WHERE a.group_id=? AND a.photo_id=? AND a.confidence>=? "
               "ORDER BY a.score DESC, a.rank")
        params: List[Any] = [str(group_id), str(photo_id), float(min_confidence)]
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.query(sql, params)

    def model_tags(self, group_id: str, min_photos: int = 1) -> List[sqlite3.Row]:
        return self.query(
            "SELECT t.tag_id, t.name, t.name_en, t.color, COUNT(*) AS photos, AVG(a.score) AS avg_score "
            "FROM photo_ai_tags a JOIN model_tags t ON t.tag_id=a.tag_id "
            "WHERE a.group_id=? GROUP BY t.tag_id HAVING photos>=? ORDER BY photos DESC, t.name",
            (str(group_id), int(min_photos)))

    # ----------------------------------------------------------- embeddings
    def upsert_embeddings(self, group_id: str, items: Sequence[Tuple[str, bytes]],
                          model: str, dim: int, commit: bool = True) -> int:
        rows = [(str(group_id), str(pid), model, int(dim), sqlite3.Binary(blob), now_iso())
                for pid, blob in items if pid and blob]
        if not rows:
            return 0
        self.connection.executemany(
            "INSERT INTO photo_embedding (group_id, photo_id, model, dim, vector, created_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(group_id, photo_id) DO UPDATE SET "
            "model=excluded.model, dim=excluded.dim, vector=excluded.vector, "
            "created_at=excluded.created_at", rows)
        if commit:
            self.commit()
        return len(rows)

    def embedding_count(self, group_id: str, model: Optional[str] = None) -> int:
        if model:
            return int(self.query_one(
                "SELECT COUNT(*) c FROM photo_embedding WHERE group_id=? AND model=?",
                (str(group_id), model))["c"])
        return int(self.query_one("SELECT COUNT(*) c FROM photo_embedding WHERE group_id=?",
                                  (str(group_id),))["c"])

    def all_embeddings(self, group_id: str, model: Optional[str] = None
                       ) -> Tuple[List[str], List[bytes]]:
        sql = "SELECT photo_id, vector FROM photo_embedding WHERE group_id=?"
        params: List[Any] = [str(group_id)]
        if model:
            sql += " AND model=?"
            params.append(model)
        rows = self.query(sql, params)
        return [r["photo_id"] for r in rows], [bytes(r["vector"]) for r in rows]

    def photos_with_local_file(self, group_id: str, album_id: Optional[str] = None,
                               limit: int = 0) -> List[sqlite3.Row]:
        sql = ("SELECT photo_id, file_path, album_id, uploader_uin, uploader_nick, upload_epoch "
               "FROM photos WHERE group_id=? AND file_status IN ('done','thumb-only') "
               "AND file_path IS NOT NULL AND file_path<>''")
        params: List[Any] = [str(group_id)]
        if album_id:
            sql += " AND album_id=?"
            params.append(str(album_id))
        sql += " ORDER BY upload_epoch DESC, photo_id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.query(sql, params)

    def photos_without_embedding(self, group_id: str, model: str, limit: int = 0) -> List[sqlite3.Row]:
        sql = ("SELECT p.photo_id, p.file_path, p.album_id, p.uploader_uin, p.uploader_nick, p.upload_epoch "
               "FROM photos p LEFT JOIN photo_embedding e ON e.group_id=p.group_id "
               "AND e.photo_id=p.photo_id AND e.model=? "
               "WHERE p.group_id=? AND p.file_status IN ('done','thumb-only') "
               "AND p.file_path IS NOT NULL AND p.file_path<>'' AND e.photo_id IS NULL "
               "ORDER BY p.upload_epoch DESC, p.photo_id")
        params: List[Any] = [model, str(group_id)]
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.query(sql, params)

    def ai_stats(self, group_id: str) -> Dict[str, Any]:
        gid = str(group_id)
        return {
            "embedded": self.embedding_count(gid),
            "tagged_photos": int(self.query_one(
                "SELECT COUNT(DISTINCT photo_id) c FROM photo_ai_tags WHERE group_id=?", (gid,))["c"]),
            "ai_tag_rows": int(self.query_one(
                "SELECT COUNT(*) c FROM photo_ai_tags WHERE group_id=?", (gid,))["c"]),
            "vocab_size": int(self.query_one("SELECT COUNT(*) c FROM model_tags")["c"]),
            "tags_used": int(self.query_one(
                "SELECT COUNT(DISTINCT tag_id) c FROM photo_ai_tags WHERE group_id=?", (gid,))["c"]),
            "captioned": self.caption_count(gid),
        }

    # ------------------------------------------------------------- captions
    def upsert_captions(self, group_id: str, items: Sequence[Tuple[str, str, str]],
                        model: str = "", source: str = "blip", commit: bool = True) -> int:
        """`items` are (photo_id, caption, extra_search_text) triples."""
        rows = [(str(group_id), str(pid), cap or "", extra or cap or "", model, source, now_iso())
                for pid, cap, extra in items if pid]
        if not rows:
            return 0
        self.connection.executemany(
            "INSERT INTO photo_caption (group_id, photo_id, caption, caption_en, model, source, created_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(group_id, photo_id) DO UPDATE SET "
            "caption=excluded.caption, caption_en=excluded.caption_en, model=excluded.model, "
            "source=excluded.source, created_at=excluded.created_at", rows)
        # refresh the searchable text for these photos
        self.connection.executemany(
            "DELETE FROM caption_fts WHERE group_id=? AND photo_id=?",
            [(str(group_id), pid) for pid, _cap, _extra in items if pid])
        self.connection.executemany(
            "INSERT INTO caption_fts (photo_id, group_id, body) VALUES (?,?,?)",
            [(str(pid), str(group_id), f"{cap or ''} {extra or ''}".strip())
             for pid, cap, extra in items if pid])
        if commit:
            self.commit()
        return len(rows)

    def caption_of_photo(self, group_id: str, photo_id: str) -> Optional[sqlite3.Row]:
        return self.query_one("SELECT * FROM photo_caption WHERE group_id=? AND photo_id=?",
                              (str(group_id), str(photo_id)))

    def caption_count(self, group_id: str) -> int:
        return int(self.query_one("SELECT COUNT(*) c FROM photo_caption WHERE group_id=?",
                                  (str(group_id),))["c"])

    def photos_without_caption(self, group_id: str, model: str, limit: int = 0) -> List[sqlite3.Row]:
        sql = ("SELECT p.photo_id, p.file_path, p.album_id, p.uploader_uin, p.upload_epoch "
               "FROM photos p LEFT JOIN photo_caption c ON c.group_id=p.group_id "
               "AND c.photo_id=p.photo_id AND c.model=? "
               "WHERE p.group_id=? AND p.file_status IN ('done','thumb-only') "
               "AND p.file_path IS NOT NULL AND p.file_path<>'' AND c.photo_id IS NULL "
               "ORDER BY p.upload_epoch DESC, p.photo_id")
        params: List[Any] = [model, str(group_id)]
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.query(sql, params)

    def clear_captions(self, group_id: str) -> int:
        cur = self.execute("DELETE FROM photo_caption WHERE group_id=?", (str(group_id),))
        self.execute("DELETE FROM caption_fts WHERE group_id=?", (str(group_id),))
        self.commit()
        return cur.rowcount or 0

    def search_captions(self, group_id: str, text: str, limit: int = 200) -> List[Tuple[str, float]]:
        """FTS search over captions; returns (photo_id, score) best-first.

        Falls back to a LIKE scan when the FTS query is malformed (e.g. punctuation only).
        """
        cleaned = " ".join(re.findall(r"[\w\u4e00-\u9fff]+", text or ""))
        if not cleaned:
            return []
        rows: List[sqlite3.Row] = []
        try:
            query = " OR ".join(f'"{token}"' for token in cleaned.split() if len(token) > 1)
            if query:
                rows = self.query(
                    "SELECT photo_id, bm25(caption_fts) AS rank FROM caption_fts "
                    "WHERE group_id=? AND caption_fts MATCH ? ORDER BY rank LIMIT ?",
                    (str(group_id), query, int(limit)))
        except sqlite3.OperationalError:
            rows = []
        if not rows:
            like = f"%{text.strip()}%"
            rows = self.query(
                "SELECT photo_id, -1.0 AS rank FROM photo_caption "
                "WHERE group_id=? AND (caption LIKE ? OR caption_en LIKE ?) LIMIT ?",
                (str(group_id), like, like, int(limit)))
        out: List[Tuple[str, float]] = []
        for index, row in enumerate(rows):
            # bm25 is negative-is-better; convert to a 0..1 style relevance
            relevance = 1.0 / (1.0 + index)
            out.append((row["photo_id"], relevance))
        return out
