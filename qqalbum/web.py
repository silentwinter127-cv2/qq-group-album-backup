"""Dependency-free HTTP server: JSON API + static WebUI + media serving."""
from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

from .crawler import BackupManager
from .ai import AiIndexer, AiSearch, CaptionIndexer
from .caption import CAPTION_LABEL, CaptionEngine, CaptionUnavailable
from .siglip import MODEL_LABEL, SiglipEngine, SiglipUnavailable
from .store import Store, now_iso

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
THUMB_DIR_NAME = "_thumbs"

# 检索结果的排序方式（/api/ai/search?sort=…）
#   relevance    —— 默认：混合相似度（语义 + 描述）
#   similar      —— 只看语义相似度（描述撞词但画面不像的会被排到后面）
#   newest/oldest—— 按上传时间
#   uploader     —— 按上传者昵称
#   interactions —— 按点赞+评论数
AI_SORT_KEYS = ("relevance", "similar", "newest", "oldest", "uploader", "interactions")


class Api:
    """Business logic behind the HTTP layer (also usable without the server)."""

    def __init__(self, store: Store, manager: BackupManager, group_id: str, root_dir: str,
                 engine=None, model_dir: Optional[str] = None,
                 caption_engine=None, caption_dir: Optional[str] = None,
                 auth: Optional["Auth"] = None):
        self.store = store
        self.manager = manager
        self.group_id = str(group_id)
        self.root_dir = root_dir
        self.model_dir = model_dir
        self._engine = engine
        self.caption_dir = caption_dir
        self._caption_engine = caption_engine
        # None = 不启用口令（只绑 127.0.0.1 时的默认）
        self.auth = auth
        self.indexer: Optional[AiIndexer] = None
        self.captioner: Optional[CaptionIndexer] = None
        self._searcher: Optional[AiSearch] = None

    # ------------------------------------------------------------ AI plumbing
    def _engine_or_none(self):
        if self._engine is None:
            try:
                self._engine = SiglipEngine.instance(self.model_dir)
            except SiglipUnavailable:
                return None
        return self._engine

    def _caption_engine_or_none(self):
        if self._caption_engine is None:
            try:
                self._caption_engine = CaptionEngine.instance(self.caption_dir)
            except CaptionUnavailable:
                return None
        return self._caption_engine

    @property
    def captioner_obj(self) -> CaptionIndexer:
        if self.captioner is None:
            self.captioner = CaptionIndexer(self.store, self.group_id,
                                            engine=self._caption_engine_or_none(),
                                            model_dir=self.caption_dir,
                                            batch_size=int(os.environ.get("QQALBUM_CAPTION_BATCH") or 12))
        return self.captioner

    @property
    def indexer_obj(self) -> AiIndexer:
        if self.indexer is None:
            self.indexer = AiIndexer(self.store, self.group_id,
                                     engine=self._engine_or_none(),
                                     model_dir=self.model_dir)
        return self.indexer

    @property
    def searcher(self) -> AiSearch:
        if self._searcher is None:
            self._searcher = AiSearch(self.store, self.group_id,
                                      engine=self._engine_or_none(),
                                      model_dir=self.model_dir)
        return self._searcher

    def ai_status(self) -> Dict[str, Any]:
        engine = self._engine_or_none()
        available, reason = (engine.available() if engine else (False, "找不到本地 SigLIP2 模型"))
        caption = self._caption_engine_or_none()
        cap_available, cap_reason = (caption.available() if caption else (False, "找不到本地 BLIP 描述模型"))
        return {
            "available": available,
            "reason": reason,
            "model": MODEL_LABEL,
            "model_dir": getattr(engine, "model_dir", self.model_dir or ""),
            "state": self.indexer_obj.state.snapshot() if self.indexer else None,
            "stats": self.store.ai_stats(self.group_id),
            "indexed": self.store.embedding_count(self.group_id),
            "local_files": int(self.store.query_one(
                "SELECT COUNT(*) c FROM photos WHERE group_id=? AND file_status IN ('done','thumb-only')",
                (self.group_id,))["c"]),
            "caption_available": cap_available,
            "caption_reason": cap_reason,
            "caption_model": CAPTION_LABEL,
            "caption_dir": getattr(caption, "model_dir", self.caption_dir or ""),
            "caption_state": self.captioner_obj.state.snapshot() if self.captioner else None,
            "captioned": self.store.caption_count(self.group_id),
        }

    def ai_vocab(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.store.model_tags(self.group_id, min_photos=1)]

    def ai_engine(self):
        return self._engine_or_none()

    def start_ai_index(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        started = self.indexer_obj.start(limit=int(payload.get("limit") or 0),
                                         retag=bool(payload.get("retag")),
                                         album_id=payload.get("album_id") or None)
        return {"started": started, "reason": "" if started else "AI 任务已在运行"}

    def start_caption(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        started = self.captioner_obj.start(limit=int(payload.get("limit") or 0),
                                           recaption=bool(payload.get("recaption")),
                                           album_id=payload.get("album_id") or None)
        return {"started": started, "reason": "" if started else "描述任务已在运行"}

    # ------------------------------------------------------------------ stats
    def overview(self) -> Dict[str, Any]:
        stats = self.store.stats(self.group_id)
        group = self.store.get_group(self.group_id)
        ai = self.store.ai_stats(self.group_id)
        ai["local_files"] = int(self.store.query_one(
            "SELECT COUNT(*) c FROM photos WHERE group_id=? AND file_status IN ('done','thumb-only')",
            (self.group_id,))["c"])
        albums = self.store.query(
            "SELECT album_id, title, description, cover_url, photo_count, create_time, creator_nick, "
            "(SELECT COUNT(*) FROM photos p WHERE p.group_id=a.group_id AND p.album_id=a.album_id) AS stored, "
            "(SELECT COUNT(*) FROM photos p WHERE p.group_id=a.group_id AND p.album_id=a.album_id "
            " AND p.file_status IN ('done','thumb-only')) AS downloaded "
            "FROM albums a WHERE group_id=? ORDER BY COALESCE(create_epoch,0) DESC, title", (self.group_id,))
        return {
            "group_id": self.group_id,
            "group": dict(group) if group else None,
            "stats": stats,
            "ai": ai,
            "videos": self.store.video_stats(self.group_id),
            "albums": [dict(a) for a in albums],
            "sync": self.manager.state.snapshot(),
            "session": self.manager._client.session_info if self.manager._client else None,
        }

    def uploaders(self) -> List[Dict[str, Any]]:
        rows = self.store.query(
            "SELECT uploader_uin AS uin, MAX(uploader_nick) AS nick, COUNT(*) AS photos "
            "FROM photos WHERE group_id=? AND uploader_uin IS NOT NULL AND uploader_uin<>'' "
            "GROUP BY uploader_uin ORDER BY photos DESC", (self.group_id,))
        return [dict(r) for r in rows]

    def members(self, query_text: str = "") -> List[Dict[str, Any]]:
        """群成员名单（含**群名片 qun_remark**）。

        为什么需要它：photos.uploader_nick 存的是 QQ 昵称（如 UserExample），
        但群里大家叫的是群名片（如「静」）。外部工具（QQ 机器人技能）只有拿到
        qun_remark 才能把「找静传的图」对上号。
        """
        rows = self.store.query(
            "SELECT m.uin, m.nick, m.qun_remark, m.friend_remark, m.role, m.is_creator, "
            "       m.is_manager, "
            "       (SELECT COUNT(*) FROM photos p WHERE p.group_id=m.group_id "
            "         AND p.uploader_uin=m.uin) AS photos "
            "FROM members m WHERE m.group_id=? ORDER BY photos DESC, m.nick",
            (self.group_id,))
        items = [dict(r) for r in rows]
        needle = (query_text or "").strip().lower()
        if needle:
            items = [m for m in items
                     if needle in (m.get("nick") or "").lower()
                     or needle in (m.get("qun_remark") or "").lower()
                     or needle == str(m.get("uin") or "")]
        return items

    def tags(self) -> List[Dict[str, Any]]:
        rows = self.store.query(
            "SELECT t.tag_id, t.name, t.color, COUNT(pt.photo_id) AS photos FROM tags t "
            "LEFT JOIN photo_tags pt ON pt.tag_id=t.tag_id AND pt.group_id=? "
            "GROUP BY t.tag_id ORDER BY t.name", (self.group_id,))
        return [dict(r) for r in rows]

    # ----------------------------------------------------------------- photos
    def photos(self, params: Dict[str, List[str]]) -> Dict[str, Any]:
        def one(key: str, default: str = "") -> str:
            values = params.get(key) or []
            return values[0] if values else default

        page = max(1, int(one("page", "1") or 1))
        per_page = min(200, max(1, int(one("per_page", "60") or 60)))
        album_id = one("album_id")
        uploader = one("uploader")
        tag_id = one("tag_id")
        has_comment = one("has_comment")
        has_like = one("has_like")
        is_video = one("is_video")
        downloaded = one("downloaded")
        thumb_only = one("thumb_only")
        file_status = one("file_status")
        date_from = one("date_from")
        date_to = one("date_to")
        search = one("q").strip()
        order = one("order", "upload_desc")

        where = ["p.group_id=?"]
        args: List[Any] = [self.group_id]
        joins = ""

        if album_id:
            where.append("p.album_id=?")
            args.append(album_id)
        if uploader:
            where.append("p.uploader_uin=?")
            args.append(uploader)
        if tag_id:
            joins += " JOIN photo_tags pt ON pt.group_id=p.group_id AND pt.photo_id=p.photo_id AND pt.tag_id=? "
            args.insert(0, int(tag_id))
        if has_comment == "1":
            where.append("p.comment_count>0")
        if has_like == "1":
            where.append("p.like_count>0")
        if is_video == "1":
            where.append("p.is_video=1")
        elif is_video == "0":
            where.append("p.is_video=0")
        elif is_video == "gif":
            where.append("p.is_animated=1")
        if downloaded == "1":
            # has a local file (full original or the only version QQ keeps)
            where.append("p.file_status IN ('done','thumb-only')")
        elif downloaded == "0":
            # still needs work: no usable local file yet
            where.append("(p.file_status IS NULL OR p.file_status NOT IN ('done','thumb-only'))")
        elif downloaded == "full":
            where.append("p.file_status='done'")
        if thumb_only == "1":
            where.append("p.file_status='thumb-only'")
        elif thumb_only == "0":
            where.append("(p.file_status IS NULL OR p.file_status<>'thumb-only')")
        if file_status == "error":
            where.append("p.file_status='error'")
        ai_tag_ids = [v for v in (params.get("ai_tag") or []) if v.isdigit()]
        for index, tag_id in enumerate(ai_tag_ids):
            alias = f"at{index}"
            joins += (f" JOIN photo_ai_tags {alias} ON {alias}.group_id=p.group_id "
                      f"AND {alias}.photo_id=p.photo_id AND {alias}.tag_id={int(tag_id)} ")
        if ai_tag_ids and one("min_confidence"):
            try:
                threshold = float(one("min_confidence"))
            except ValueError:
                threshold = 0.0
            if threshold > 0:
                where.append(" AND ".join(f"at{i}.confidence>=?" for i in range(len(ai_tag_ids))))
                args.extend([threshold] * len(ai_tag_ids))
        if date_from:
            where.append("p.upload_epoch >= ?")
            args.append(int(parse_date_arg(date_from) or 0))
        if date_to:
            where.append("p.upload_epoch <= ?")
            args.append(int(parse_date_arg(date_to, end_of_day=True) or 0))
        if search:
            like = f"%{search}%"
            where.append("(p.description LIKE ? OR p.name LIKE ? OR p.uploader_nick LIKE ? OR "
                         "p.photo_id LIKE ? OR p.batch_id LIKE ? OR EXISTS (SELECT 1 FROM comments c "
                         "WHERE c.group_id=p.group_id AND (c.batch_id=p.batch_id OR c.photo_id=p.photo_id) "
                         "AND c.content LIKE ?))")
            args.extend([like] * 6)

        orders = {
            "upload_desc": "p.upload_epoch DESC, p.photo_id DESC",
            "upload_asc": "p.upload_epoch ASC, p.photo_id ASC",
            "comments": "p.comment_count DESC, p.upload_epoch DESC",
            "likes": "p.like_count DESC, p.upload_epoch DESC",
            "album": "p.album_id, COALESCE(p.upload_epoch,0) DESC",
        }
        order_sql = orders.get(order, orders["upload_desc"])
        where_sql = " AND ".join(where)

        total_row = self.store.query_one(
            f"SELECT COUNT(DISTINCT p.photo_id) AS n FROM photos p {joins} WHERE {where_sql}", args)
        total = int(total_row["n"] if total_row else 0)

        rows = self.store.query(
            f"SELECT p.*, (SELECT title FROM albums a WHERE a.group_id=p.group_id AND "
            f"a.album_id=p.album_id) AS album_title FROM photos p {joins} WHERE {where_sql} "
            f"ORDER BY {order_sql} LIMIT ? OFFSET ?", args + [per_page, (page - 1) * per_page])

        items = []
        for row in rows:
            item = dict(row)
            item.pop("raw_json", None)
            item["has_file"] = bool(item.get("file_path")) and (item.get("file_status") == "done")
            item["thumb"] = f"/api/thumb?photo_id={urllib.parse.quote(item['photo_id'])}"
            items.append(item)
        return {"total": total, "page": page, "per_page": per_page,
                "pages": (total + per_page - 1) // per_page if per_page else 1, "items": items}

    def photo_detail(self, photo_id: str) -> Dict[str, Any]:
        row = self.store.query_one("SELECT * FROM photos WHERE group_id=? AND photo_id=?",
                                   (self.group_id, photo_id))
        if not row:
            return {"error": "not found"}
        photo = dict(row)
        batch_id = photo.get("batch_id") or ""
        feed = None
        if batch_id:
            feed_row = self.store.query_one("SELECT * FROM feeds WHERE group_id=? AND batch_id=?",
                                            (self.group_id, batch_id))
            if feed_row:
                feed = dict(feed_row)
                feed.pop("raw_json", None)
        comments = self.store.query(
            "SELECT * FROM comments WHERE group_id=? AND (batch_id=? OR photo_id=?) "
            "ORDER BY is_reply, COALESCE(comment_epoch,0) DESC",
            (self.group_id, batch_id, photo_id))
        likes = self.store.query(
            "SELECT * FROM likes WHERE group_id=? AND batch_id=? ORDER BY COALESCE(nick,'')",
            (self.group_id, batch_id)) if batch_id else []
        tags = self.store.query(
            "SELECT t.tag_id, t.name, t.color FROM photo_tags pt JOIN tags t ON t.tag_id=pt.tag_id "
            "WHERE pt.group_id=? AND pt.photo_id=?", (self.group_id, photo_id))
        siblings = self.store.query(
            "SELECT photo_id FROM photos WHERE group_id=? AND album_id=? AND batch_id=? ORDER BY photo_id",
            (self.group_id, photo.get("album_id"), batch_id)) if batch_id else []
        photo["album_title"] = (self.store.query_one(
            "SELECT title FROM albums WHERE group_id=? AND album_id=?",
            (self.group_id, photo.get("album_id"))) or {"title": ""})["title"]
        return {"photo": photo, "feed": feed, "comments": [dict(c) for c in comments],
                "likes": [dict(l) for l in likes], "tags": [dict(t) for t in tags],
                "ai_tags": [dict(t) for t in self.store.ai_tags_of_photo(self.group_id, photo_id)],
                "caption": (self.store.caption_of_photo(self.group_id, photo_id) or {"caption": ""})["caption"],
                "like_total": int((feed or {}).get("like_total") or photo.get("like_count") or 0),
                "comment_total": int((feed or {}).get("comment_total") or photo.get("comment_count") or 0),
                "has_embedding": bool(self.store.query_one(
                    "SELECT 1 x FROM photo_embedding WHERE group_id=? AND photo_id=?",
                    (self.group_id, photo_id))),
                "batch_photos": [s["photo_id"] for s in siblings]}

    # ---------------------------------------------------------------- gallery
    def gallery(self, limit: int = 60) -> Dict[str, Any]:
        rows = self.store.query(
            "SELECT photo_id, album_id, uploader_nick, upload_epoch, comment_count, like_count, "
            "file_status, is_video FROM photos WHERE group_id=? AND file_status='done' "
            "ORDER BY RANDOM() LIMIT ?", (self.group_id, limit))
        return {"items": [dict(r) for r in rows]}

    def timeline(self) -> List[Dict[str, Any]]:
        rows = self.store.query(
            "SELECT strftime('%Y-%m', upload_epoch, 'unixepoch', 'localtime') AS month, COUNT(*) AS photos "
            "FROM photos WHERE group_id=? AND upload_epoch IS NOT NULL GROUP BY month ORDER BY month DESC",
            (self.group_id,))
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ time axis
    def time_axis(self, params: Dict[str, List[str]]) -> Dict[str, Any]:
        """时间轴索引：年 → 月 → 日 的照片数，供日期分组与跳页用。"""
        def one(key: str, default: str = "") -> str:
            values = params.get(key) or []
            return values[0] if values else default

        only_local = one("downloaded") == "1"
        days = self.store.time_axis(self.group_id, album_id=one("album_id") or None,
                                    only_local=only_local)
        years: Dict[str, Dict[str, Any]] = {}
        for row in days:
            year = str(row.get("year") or "")
            month = str(row.get("month") or "")
            if not year or not month:
                continue
            entry = years.setdefault(year, {"year": year, "photos": 0, "months": {}})
            entry["photos"] += int(row["photos"] or 0)
            slot = entry["months"].setdefault(month, {"month": month, "photos": 0})
            slot["photos"] += int(row["photos"] or 0)
        out_years = []
        for year in sorted(years, reverse=True):
            entry = years[year]
            entry["months"] = [entry["months"][m] for m in sorted(entry["months"])]
            out_years.append(entry)
        return {"total": sum(int(d["photos"] or 0) for d in days),
                "days": days, "years": out_years}

    def photos_by_month(self, params: Dict[str, List[str]]) -> Dict[str, Any]:
        def one(key: str, default: str = "") -> str:
            values = params.get(key) or []
            return values[0] if values else default

        data = self.store.photos_by_month(
            self.group_id,
            year=one("year"), month=one("month"),
            album_id=one("album_id") or None,
            only_local=one("downloaded") == "1",
            page=int(one("page", "1") or 1),
            per_page=int(one("per_page", "120") or 120))
        self._decorate_photo_rows(data["items"])
        return data

    def _decorate_photo_rows(self, items: List[Dict[str, Any]]) -> None:
        """给时间轴的照片补上 UI 要用的字段（有没有本地文件、是不是视频封面）。"""
        if not items:
            return
        mapping = self.store.video_ids_for_photos(self.group_id, [i["photo_id"] for i in items])
        for item in items:
            item["has_file"] = bool(item.get("file_path")) and item.get("file_status") in (
                "done", "thumb-only")
            item["is_video_cover"] = item["photo_id"] in mapping
            if item["is_video_cover"]:
                item["video_id"] = mapping[item["photo_id"]]

    def top_uploaders(self, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self.store.query(
            "SELECT uploader_uin AS uin, MAX(uploader_nick) AS nick, COUNT(*) AS photos, "
            "SUM(COALESCE(comment_count,0)) AS comments, SUM(COALESCE(like_count,0)) AS likes "
            "FROM photos WHERE group_id=? GROUP BY uploader_uin ORDER BY photos DESC LIMIT ?",
            (self.group_id, limit))
        return [dict(r) for r in rows]

    def comments_feed(self, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        """Newest comments first.

        The album/photo link is fetched with correlated subqueries: a plain join on
        `batch_id` multiplies rows because one batch can hold hundreds of photos.
        """
        rows = self.store.query(
            "SELECT c.*, "
            "(SELECT p.album_id FROM photos p WHERE p.group_id=c.group_id AND p.batch_id=c.batch_id "
            " LIMIT 1) AS album_id, "
            "(SELECT p.photo_id FROM photos p WHERE p.group_id=c.group_id AND p.batch_id=c.batch_id "
            " LIMIT 1) AS linked_photo_id "
            "FROM comments c WHERE c.group_id=? "
            "ORDER BY COALESCE(c.comment_epoch,0) DESC, c.comment_id DESC LIMIT ? OFFSET ?",
            (self.group_id, limit, offset))
        out = []
        for row in rows:
            item = dict(row)
            item["photo_id"] = item.get("photo_id") or item.get("linked_photo_id") or ""
            out.append(item)
        return out

    # ------------------------------------------------------------------- tags
    def add_tag(self, name: str, color: str = "#1677ff") -> Dict[str, Any]:
        tag_id = self.store.ensure_tag(name, color)
        return {"tag_id": tag_id, "name": name, "color": color}

    def tag_photos(self, tag_id: int, photo_ids: List[str], remove: bool = False) -> int:
        if remove:
            self.store.connection.executemany(
                "DELETE FROM photo_tags WHERE group_id=? AND photo_id=? AND tag_id=?",
                [(self.group_id, str(pid), int(tag_id)) for pid in photo_ids])
        else:
            self.store.connection.executemany(
                "INSERT OR IGNORE INTO photo_tags (group_id, photo_id, tag_id, source) VALUES (?,?,?, 'manual')",
                [(self.group_id, str(pid), int(tag_id)) for pid in photo_ids])
        self.store.commit()
        return len(photo_ids)

    def auto_tag_by_uploader(self) -> int:
        rows = self.store.query(
            "SELECT DISTINCT uploader_uin, uploader_nick FROM photos WHERE group_id=? "
            "AND uploader_uin IS NOT NULL AND uploader_uin<>''", (self.group_id,))
        count = 0
        for row in rows:
            name = f"上传者:{row['uploader_nick'] or row['uploader_uin']}"
            tag_id = self.store.ensure_tag(name, "#52c41a")
            self.store.execute(
                "INSERT OR IGNORE INTO photo_tags (group_id, photo_id, tag_id, source) "
                "SELECT group_id, photo_id, ?, 'auto-uploader' FROM photos "
                "WHERE group_id=? AND uploader_uin=?", (tag_id, self.group_id, row["uploader_uin"]))
            count += 1
        self.store.commit()
        return count

    # --------------------------------------------------------------- backup
    def start_sync(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        ok = self.manager.start_sync(
            albums=bool(payload.get("albums", True)),
            photos=bool(payload.get("photos", True)),
            feeds=bool(payload.get("feeds", True)),
            members=bool(payload.get("members", True)),
            incremental=bool(payload.get("incremental", True)),
            videos=bool(payload.get("videos", False)))
        return {"started": ok, "reason": "" if ok else "已有任务在运行"}

    def start_download(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        ok = self.manager.start_download(album_id=payload.get("album_id") or None,
                                         limit=int(payload.get("limit") or 0),
                                         retry_failed=bool(payload.get("retry_failed", True)))
        return {"started": ok, "reason": "" if ok else "已有任务在运行"}

    # --------------------------------------------------------------- videos
    def videos(self, params: Dict[str, List[str]]) -> Dict[str, Any]:
        def one(key: str, default: str = "") -> str:
            values = params.get(key) or []
            return values[0] if values else default
        data = self.store.videos(self.group_id,
                                 album_id=one("album_id") or None,
                                 downloaded=one("downloaded") or None,
                                 page=int(one("page", "1") or 1),
                                 per_page=int(one("per_page", "60") or 60))
        data["stats"] = self.store.video_stats(self.group_id)
        return data

    def start_video_download(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        ok = self.manager.start_video_download(album_id=payload.get("album_id") or None,
                                               limit=int(payload.get("limit") or 0))
        return {"started": ok, "reason": "" if ok else "已有任务在运行"}

    def video_status(self) -> Dict[str, Any]:
        """视频引擎可用性 + 库内统计（界面据此提示"引擎没开"）。"""
        info: Dict[str, Any] = {"engine_ok": False, "engine": "", "error": ""}
        try:
            from .video import VideoClient
            client = VideoClient(self.group_id, log=lambda _m: None)
            info["engine"] = client.connect()
            info["engine_ok"] = True
        except Exception as exc:
            info["error"] = f"{type(exc).__name__}: {exc}"
        info["stats"] = self.store.video_stats(self.group_id)
        return info

    def cancel(self) -> Dict[str, Any]:
        self.manager.cancel()
        return {"cancelled": True}

    def sync_status(self) -> Dict[str, Any]:
        return self.manager.state.snapshot()

    def login_status(self) -> Dict[str, Any]:
        client = self.manager._client
        if client is None:
            return {"connected": False}
        return {"connected": True, "session": client.session_info}

    def connect(self) -> Dict[str, Any]:
        client = self.manager.client
        return {"session": client.session_info}

    # ---------------------------------------------------------------- export
    def export_rows(self) -> List[Dict[str, Any]]:
        rows = self.store.query(
            "SELECT p.photo_id, p.album_id, a.title AS album_title, p.description, p.uploader_uin, "
            "p.uploader_nick, p.upload_time, p.width, p.height, p.original_width, p.original_height, "
            "p.comment_count, p.like_count, p.batch_id, p.file_path, p.raw_url, "
            "(SELECT COUNT(*) FROM comments c WHERE c.group_id=p.group_id AND c.batch_id=p.batch_id) AS comments_stored "
            "FROM photos p LEFT JOIN albums a ON a.group_id=p.group_id AND a.album_id=p.album_id "
            "WHERE p.group_id=? ORDER BY p.album_id, p.upload_epoch", (self.group_id,))
        return [dict(r) for r in rows]


def annotate_video_covers(api: Any, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """给检索结果标出「这张其实是某个视频的封面」，并附上视频信息。

    为什么能做：视频的封面本身就是一条 photos 记录，且视频表里存了它的 video_id。
    检索命中的是封面照片，但用户想找的其实是视频 —— 所以要能一眼看出来、能直接播。

    抽成模块级函数（而不是 Handler 的方法）是为了能单独测试，不必起整个 HTTP 层。
    """
    if not items:
        return items
    mapping = api.store.video_ids_for_photos(api.group_id, [i["photo_id"] for i in items])
    if not mapping:
        for item in items:
            item["is_video_cover"] = False
        return items
    videos = api.store.videos_by_ids(api.group_id, list(mapping.values()))
    for item in items:
        video_id = mapping.get(item["photo_id"])
        item["is_video_cover"] = bool(video_id)
        if not video_id:
            continue
        video = videos.get(video_id) or {}
        item["video_id"] = video_id
        item["video"] = {
            "video_id": video_id,
            "duration_ms": video.get("duration_ms"),
            "width": video.get("width"),
            "height": video.get("height"),
            "file_status": video.get("file_status"),
            "file_size": video.get("file_size"),
            "has_file": bool(video.get("file_path") and video.get("file_status") == "done"),
        }
    return items


def filter_by_media(api: Any, items: List[Dict[str, Any]], media: str) -> List[Dict[str, Any]]:
    """media: '' 全部 / 'videos' 只看视频 / 'photos' 只看非视频。

    先标注再过滤：视频只占照片总数的不到 1%，所以在"全部"里几乎露不出来，
    必须能显式切到"只看视频"。
    """
    if media not in ("videos", "photos"):
        return items
    annotated = annotate_video_covers(api, items)
    if media == "videos":
        return [i for i in annotated if i.get("is_video_cover")]
    return [i for i in annotated if not i.get("is_video_cover")]


class Auth:
    """口令登录 + 会话 cookie。

    为什么要做这个：这个服务能看到**群里其他人的**昵称、QQ 号、全部照片和评论原文。
    它本来只绑 127.0.0.1，所以以前不需要验证；一旦要让别人访问（局域网或内网穿透），
    没有验证就等于把这些内容公开 —— 所以放开的**前提**是先把这道门装上。

    设计取舍：
    - 口令用 PBKDF2 存哈希、比较用 hmac.compare_digest（防时序侧信道）；
    - 会话是内存里的随机 token，重启即失效（这种本地工具不需要持久会话）；
    - 每个 IP 统计连续失败次数并递增延迟，防口令暴破（会话 token 本身猜不出来）；
    - cookie 默认 HttpOnly + SameSite=Lax；只有在**确实走 HTTPS** 时才加 Secure，
      否则通过 http 访问时浏览器不会回传 cookie，会变成"登录了但一直弹登录框"。
    """

    COOKIE = "qqalbum_session"
    SESSION_TTL = 12 * 3600          # 12 小时
    MAX_FAILS = 5                    # 连续失败这么多次后开始延迟

    def __init__(self, password: str, log: Optional[Callable[[str], None]] = None) -> None:
        self.password_hash = self._hash(password)
        self.log = log or (lambda _m: None)
        self._sessions: Dict[str, float] = {}      # token -> 过期时间戳
        self._fails: Dict[str, List[float]] = {}   # ip -> 失败时间列表
        self._lock = threading.Lock()

    @staticmethod
    def _hash(password: str) -> bytes:
        salt = b"qqalbum-local-ui"      # 本地单人使用，盐固定即可（不是多用户系统）
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)

    # ---------------------------------------------------------------- sessions
    def login(self, password: str) -> Optional[str]:
        if not hmac.compare_digest(self._hash(password), self.password_hash):
            return None
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = time.time() + self.SESSION_TTL
        return token

    def valid(self, token: Optional[str]) -> bool:
        if not token:
            return False
        now = time.time()
        with self._lock:
            # 顺手清掉过期的，避免长期运行越攒越多
            for key in [k for k, exp in self._sessions.items() if exp < now]:
                self._sessions.pop(key, None)
            exp = self._sessions.get(token)
        return bool(exp and exp > now)

    def logout(self, token: Optional[str]) -> None:
        if token:
            with self._lock:
                self._sessions.pop(token, None)

    # ------------------------------------------------------------ rate limit
    def penalty(self, ip: str) -> float:
        """这个 IP 当前要等多少秒才能再试（失败越多等越久）。"""
        now = time.time()
        with self._lock:
            fails = [t for t in self._fails.get(ip, []) if now - t < 300]
            self._fails[ip] = fails
            count = len(fails)
        if count < self.MAX_FAILS:
            return 0.0
        # 第 5 次开始每次 +2 秒，最多 60 秒
        return float(min(60, (count - self.MAX_FAILS + 1) * 2))

    def note_failure(self, ip: str) -> None:
        with self._lock:
            self._fails.setdefault(ip, []).append(time.time())

    def note_success(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)

    def cookie_header(self, token: str, secure: bool) -> str:
        parts = [f"{self.COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Lax",
                 f"Max-Age={self.SESSION_TTL}"]
        if secure:
            parts.append("Secure")
        return "; ".join(parts)


class Handler(BaseHTTPRequestHandler):
    server_version = "QQAlbumBackup/1.0"
    api: Api = None  # type: ignore
    quiet = True

    # ------------------------------------------------------------- utilities
    def log_message(self, fmt: str, *args: Any) -> None:
        if not self.quiet:
            super().log_message(fmt, *args)

    def log_error(self, fmt: str, *args: Any) -> None:
        """不要为「客户端断开」打印 traceback。

        ThreadingHTTPServer 默认把每个请求线程的未处理异常打到 stderr。客户端
        取消请求（切页、滚走缩略图、刷新）属于日常操作，不该在日志里长成一大段
        Traceback —— 那会让人误以为服务崩了，也把真正的错误淹掉。
        """
        message = fmt % args if args else fmt
        if any(code in message for code in ("10053", "10054", "BrokenPipe", "ConnectionAborted")):
            return
        super().log_error(fmt, *args)

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message: str, status: int = 400) -> None:
        self._json({"error": message}, status)

    @staticmethod
    def _is_client_gone(exc: BaseException) -> bool:
        """判断异常是不是「客户端已经断开」。

        为什么必须单独判它：浏览器取消图片请求、缩略图滚动出视野、或用户刷新页面时，
        对端会直接关掉连接，于是写响应时抛 ConnectionAbortedError（WinError 10053）
        或 ConnectionResetError。这些**不是服务端故障**。
        以前的 except 只挡了 BrokenPipeError，结果 WinError 10053 落到兜底的 500 分支，
        又往一个已经死掉的 socket 写错误响应 —— 再抛一次，日志里堆一大段 traceback，
        看起来像服务崩了，实际只是客户端走开了。
        """
        if isinstance(exc, (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)):
            return True
        return isinstance(exc, OSError) and getattr(exc, "winerror", None) in (10053, 10054)

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}

    def _file(self, path: str, cache: bool = True, download_name: Optional[str] = None) -> None:
        if not os.path.isfile(path):
            self._error("file not found", 404)
            return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "public, max-age=86400" if cache else "no-store")
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(256 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    # ------------------------------------------------------------------ verbs
    # ------------------------------------------------------------------ auth
    def _client_ip(self) -> str:
        return self.client_address[0] if self.client_address else "?"

    def _is_secure_request(self) -> bool:
        """是不是 HTTPS（穿透/反代后面可能是）。只有确定是 HTTPS 才给 cookie 加 Secure。"""
        if self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            return True
        if self.headers.get("X-Forwarded-Ssl", "").lower() == "on":
            return True
        return False

    def _session_token(self) -> str:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == Auth.COOKIE and value:
                return value.strip()
        return ""

    def _auth_required(self) -> bool:
        return self.api.auth is not None

    def _authed(self) -> bool:
        if not self._auth_required():
            return True
        return self.api.auth.valid(self._session_token())

    def _need_login(self) -> None:
        """未登录：接口返回 401，页面返回登录页。"""
        accept = (self.headers.get("Accept") or "")
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/api/"):
            return self._json({"error": "需要登录", "auth_required": True}, 401)
        if "text/html" in accept or path == "/" or path == "/index.html":
            return self._file(os.path.join(STATIC_DIR, "login.html"), cache=False)
        return self._json({"error": "需要登录", "auth_required": True}, 401)

    def _handle_login(self) -> None:
        payload = self._read_json()
        password = str(payload.get("password") or "")
        ip = self._client_ip()
        # ⚠️ 顺序很重要：**先验口令，再罚失败**。
        #    反过来的话，别人拿错口令刷几次就能把合法用户一起挡在门外
        #    （限速是按 IP 的，本机和攻击者 NAT 后面可能是同一个 IP），
        #    这等于给了对方一个拒绝服务的开关。正确口令永远放行。
        token = self.api.auth.login(password)
        if token is None:
            self.api.auth.note_failure(ip)
            # 注意顺序：先记这次失败再问罚多久，否则恰好到阈值的那一次不会被拒
            wait = self.api.auth.penalty(ip)
            if wait > 0:
                return self._json(
                    {"error": f"口令不对；尝试过于频繁，请等 {wait:.0f} 秒后再试"}, 429)
            fails = len(self.api.auth._fails.get(ip, []))
            left = max(0, Auth.MAX_FAILS - fails)
            hint = f"（再失败 {left} 次将开始延迟）" if left else ""
            return self._json({"error": "口令不对" + hint}, 401)
        self.api.auth.note_success(ip)
        secure = self._is_secure_request()
        body = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", self.api.auth.cookie_header(token, secure))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _handle_logout(self) -> None:
        self.api.auth.logout(self._session_token())
        body = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie",
                         f"{Auth.COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)
        try:
            # 口令登录页要能匿名拿到，否则登录框都出不来。
            # /api/health 也放行：自启动脚本、机器人技能靠它探活，它们没有会话。
            public = path in ("/login.html", "/api/health")
            if not public and not self._authed():
                return self._need_login()
            if path == "/api/auth-state":
                return self._json({"auth_required": self._auth_required(),
                                   "authed": self._authed()})
            if path.startswith("/api/"):
                return self._api_get(path, params)
            if path == "/login.html":
                return self._file(os.path.join(STATIC_DIR, "login.html"), cache=False)
            if path == "/" or path == "/index.html":
                return self._file(os.path.join(STATIC_DIR, "index.html"), cache=False)
            if path.startswith("/static/"):
                rel = path[len("/static/"):]
                target = os.path.normpath(os.path.join(STATIC_DIR, rel))
                if not target.startswith(STATIC_DIR):
                    return self._error("forbidden", 403)
                # no-store: the UI is edited locally, and a 24h cache served stale JS
                return self._file(target, cache=False)
            return self._error("not found", 404)
        except Exception as exc:  # noqa: BLE001
            if self._is_client_gone(exc):
                return
            try:
                return self._error(f"{type(exc).__name__}: {exc}", 500)
            except Exception:  # 连错误响应都写不出去（客户端已走），不要再抛
                return

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        content_type = self.headers.get("Content-Type") or ""
        try:
            if parsed.path == "/api/login":
                if not self._auth_required():
                    return self._json({"ok": True, "note": "未启用口令"})
                return self._handle_login()
            if not self._authed():
                return self._need_login()
            if parsed.path == "/api/logout":
                return self._handle_logout()
            if parsed.path == "/api/ai/search-image":
                return self._api_search_image(content_type)
            if parsed.path == "/api/ai/index-image":
                return self._api_index_image(content_type)
            payload = self._read_json()
            return self._api_post(parsed.path, payload)
        except Exception as exc:  # noqa: BLE001
            if self._is_client_gone(exc):
                return
            try:
                return self._error(f"{type(exc).__name__}: {exc}", 500)
            except Exception:
                return

    # ------------------------------------------------------- multipart helper
    def _read_multipart(self) -> Tuple[Dict[str, str], Optional[Tuple[str, bytes]]]:
        """Minimal multipart/form-data reader: returns (fields, (filename, data))."""
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}, None
        content_type = self.headers.get("Content-Type") or ""
        marker = "boundary="
        if marker not in content_type:
            return {}, None
        boundary = content_type.split(marker, 1)[1].strip().strip('"').encode()
        body = self.rfile.read(length)
        fields: Dict[str, str] = {}
        upload: Optional[Tuple[str, bytes]] = None
        for part in body.split(b"--" + boundary):
            if not part or part in (b"--\r\n", b"--", b"\r\n"):
                continue
            head, _, data = part.partition(b"\r\n\r\n")
            if not _:
                continue
            data = data.rstrip(b"\r\n")
            if data.endswith(b"--"):
                data = data[:-2].rstrip(b"\r\n")
            text = head.decode("utf-8", "replace")
            if "filename=" in text:
                name = text.split("filename=", 1)[1].split("\r\n", 1)[0].strip().strip('"')
                upload = (name, data)
            else:
                field = text.split("name=", 1)[1].split(";", 1)[0].split("\r\n", 1)[0].strip().strip('"')
                fields[field] = data.decode("utf-8", "replace")
        return fields, upload

    def _api_search_image(self, content_type: str) -> None:
        fields, upload = self._read_multipart()
        if not upload or not upload[1]:
            return self._error("请上传一张图片（字段名 file）", 400)
        engine = self.api.ai_engine()
        if engine is None:
            return self._error("找不到本地 SigLIP2 模型", 503)
        searcher = self.api.searcher
        usable, reason = searcher.usable()
        if not usable:
            return self._error(reason, 503)
        top_k = max(1, int(fields.get("top_k") or 60))
        filters = {key: fields.get(key, "") for key in
                   ("album_id", "uploader", "date_from", "date_to")}
        # 与 /api/ai/search 保持一致：媒体类型 / 排序 / AI 标签也要认，
        # 否则「只看视频」在以图搜图那条路上会被静默忽略。
        media = (fields.get("media") or "").strip().lower()
        if media not in ("videos", "photos"):
            media = ""
        sort = (fields.get("sort") or "relevance").strip().lower()
        if sort not in AI_SORT_KEYS:
            sort = "relevance"
        ai_tag_ids = [int(v) for v in str(fields.get("ai_tag") or "").split(",") if v.strip().isdigit()]
        min_conf = float(fields.get("min_confidence") or 0)
        try:
            image = engine.load_image_bytes(upload[1])
        except Exception as exc:
            return self._error(f"无法解析图片：{exc}", 400)
        pool_k = max(top_k, min(400, top_k * 4))
        items = searcher.search_image(image, top_k=pool_k, filters=filters or None)
        decorated = self._decorate_results(items)
        decorated = self._ai_tag_filter(decorated, ai_tag_ids, min_conf)
        decorated = filter_by_media(self.api, decorated, media)
        decorated = self._ai_sort(decorated, sort)[:top_k]
        return self._json({"mode": "image", "filename": upload[0], "total": len(decorated),
                           "items": decorated, "filters": filters, "sort": sort,
                           "media": media, "ai_tags": ai_tag_ids})

    def _api_index_image(self, content_type: str) -> None:
        """Describe an arbitrary uploaded image with tags (no library lookup)."""
        _fields, upload = self._read_multipart()
        if not upload or not upload[1]:
            return self._error("请上传一张图片（字段名 file）", 400)
        engine = self.api.ai_engine()
        if engine is None:
            return self._error("找不到本地 SigLIP2 模型", 503)
        try:
            image = engine.load_image_bytes(upload[1])
        except Exception as exc:
            return self._error(f"无法解析图片：{exc}", 400)
        vector = engine.encode_image(image)
        tags = engine.tag_image_features(vector, top_k=12)
        return self._json({"filename": upload[0], "tags": tags})

    # -------------------------------------------------------------- api: GET
    def _api_get(self, path: str, params: Dict[str, List[str]]) -> None:
        api = self.api
        if path in ("/api/health", "/api/ping"):
            # 轻量健康检查：给启动脚本、自启任务和外部工具（QQ 机器人技能）用来判断
            # 「服务到底起没起、是不是这个端口」，不碰数据库、不加载模型。
            return self._json({"ok": True, "service": "qqalbum", "group_id": api.group_id,
                               "version": Handler.server_version})
        if path == "/api/overview":
            return self._json(api.overview())
        if path == "/api/photos":
            return self._json(api.photos(params))
        if path == "/api/photo":
            photo_id = (params.get("photo_id") or [""])[0]
            return self._json(api.photo_detail(photo_id))
        if path == "/api/uploaders":
            return self._json({"items": api.uploaders()})
        if path == "/api/members":
            q = (params.get("q") or [""])[0]
            return self._json({"items": api.members(q)})
        if path == "/api/videos":
            return self._json(api.videos(params))
        if path == "/api/videos/status":
            return self._json(api.video_status())
        if path == "/api/tags":
            return self._json({"items": api.tags()})
        if path == "/api/timeline":
            return self._json({"items": api.timeline()})
        if path == "/api/time-axis":
            return self._json(api.time_axis(params))
        if path == "/api/time-photos":
            return self._json(api.photos_by_month(params))
        if path == "/api/top_uploaders":
            return self._json({"items": api.top_uploaders()})
        if path == "/api/comments":
            limit = int((params.get("limit") or ["100"])[0])
            offset = int((params.get("offset") or ["0"])[0])
            return self._json({"items": api.comments_feed(limit, offset)})
        if path == "/api/gallery":
            return self._json(api.gallery(int((params.get("limit") or ["60"])[0])))
        if path == "/api/sync/status":
            return self._json(api.sync_status())
        if path == "/api/ai/status":
            return self._json(api.ai_status())
        if path == "/api/ai/tags":
            return self._json({"items": api.ai_vocab(), "model": MODEL_LABEL})
        if path == "/api/ai/search":
            return self._json(self._ai_search(params))
        if path == "/api/login/status":
            return self._json(api.login_status())
        if path == "/api/export":
            rows = api.export_rows()
            body = json.dumps(rows, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="qqalbum_export.json"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/csv":
            rows = api.export_rows()
            header = list(rows[0].keys()) if rows else []
            lines = [",".join(header)]
            for row in rows:
                lines.append(",".join(_csv_cell(row.get(h)) for h in header))
            body = ("\ufeff" + "\n".join(lines)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="qqalbum_photos.csv"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/file":
            photo_id = (params.get("photo_id") or [""])[0]
            row = self.api.store.query_one("SELECT * FROM photos WHERE group_id=? AND photo_id=?",
                                           (self.api.group_id, photo_id))
            if not row or not row["file_path"]:
                return self._error("no local file", 404)
            return self._file(row["file_path"], download_name=os.path.basename(row["file_path"]))
        if path == "/api/video":
            return self._serve_video((params.get("video_id") or [""])[0], stream=True)
        if path == "/api/video-file":
            return self._serve_video((params.get("video_id") or [""])[0], stream=False)
        if path == "/api/video-cover":
            video_id = (params.get("video_id") or [""])[0]
            size = int((params.get("size") or ["640"])[0])
            row = self.api.store.query_one(
                "SELECT photo_id FROM videos WHERE group_id=? AND video_id=?",
                (self.api.group_id, video_id))
            # 没绑上封面时退回视频自带的 cover_url，别直接 404
            if not row or not row["photo_id"]:
                cover = self.api.store.query_one(
                    "SELECT cover_url FROM videos WHERE group_id=? AND video_id=?",
                    (self.api.group_id, video_id))
                if cover and cover["cover_url"]:
                    return self._redirect_image(cover["cover_url"])
                return self._error("no cover", 404)
            return self._serve_thumb(row["photo_id"], size)
        if path == "/api/video-thumb":
            # 搜索结果是"封面照片"，前端直接用 photo_id 取缩略图
            photo_id = (params.get("photo_id") or [""])[0]
            if not photo_id:
                return self._error("photo_id required", 400)
            return self._serve_thumb(photo_id, int((params.get("size") or ["640"])[0]))
        if path == "/api/image":
            photo_id = (params.get("photo_id") or [""])[0]
            row = self.api.store.query_one("SELECT * FROM photos WHERE group_id=? AND photo_id=?",
                                           (self.api.group_id, photo_id))
            if not row:
                return self._error("not found", 404)
            if row["file_path"] and os.path.isfile(row["file_path"]):
                return self._file(row["file_path"])
            url = row["big_url"] or row["thumb_url"] or row["raw_url"]
            if not url:
                return self._error("no image url", 404)
            self.send_response(302)
            self.send_header("Location", url)
            self.end_headers()
            return
        if path == "/api/thumb":
            return self._serve_thumb((params.get("photo_id") or [""])[0],
                                     int((params.get("size") or ["320"])[0]))
        if path == "/api/cover":
            return self._serve_cover((params.get("album_id") or [""])[0],
                                     int((params.get("size") or ["560"])[0]))
        return self._error("unknown endpoint", 404)

    def _cache_image(self, url: str, cache_path: str) -> bool:
        """Download `url` into `cache_path` (atomically). Returns True on success."""
        if os.path.isfile(cache_path) and os.path.getsize(cache_path) > 512:
            return True
        if not url:
            return False
        import urllib.request
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 Edg/126.0",
                "Referer": "https://h5.qzone.qq.com/",
            })
            with urllib.request.urlopen(req, timeout=40) as resp:
                data = resp.read()
            if len(data) < 512:
                return False
            tmp = cache_path + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, cache_path)
            return True
        except Exception:
            return False

    def _serve_cover(self, album_id: str, size: int) -> None:
        row = self.api.store.query_one("SELECT cover_url FROM albums WHERE group_id=? AND album_id=?",
                                       (self.api.group_id, album_id))
        if not row:
            return self._error("not found", 404)
        cover = (row["cover_url"] or "").replace("http://", "https://", 1)
        if not cover:
            return self._error("no cover", 404)
        cache_dir = os.path.join(self.api.root_dir, "photos", "_covers")
        safe = re.sub(r"[^0-9A-Za-z._@()\-]+", "_", album_id)[:60]
        cache_path = os.path.join(cache_dir, f"{safe}.jpg")
        if self._cache_image(cover, cache_path):
            return self._file(cache_path)
        self.send_response(302)
        self.send_header("Location", cover)
        self.end_headers()

    def _redirect_image(self, url: str) -> None:
        """把 CDN 图片地址 302 给浏览器（顺手把 http 升成 https）。"""
        target = (url or "").replace("http://", "https://", 1)
        if not target:
            return self._error("no image", 404)
        self.send_response(302)
        self.send_header("Location", target)
        self.end_headers()

    def _serve_video(self, video_id: str, stream: bool = True) -> None:
        """播放/下载本地视频。

        `stream=True` 时支持 HTTP Range：浏览器拖动进度条要发 Range 请求，
        一次性把整个文件塞过去的话视频没法拖、也吃内存。
        """
        row = self.api.store.query_one(
            "SELECT video_id, file_path, file_status FROM videos "
            "WHERE group_id=? AND video_id=?", (self.api.group_id, video_id))
        if not row or not row["file_path"] or not os.path.isfile(row["file_path"]):
            return self._error("视频还没下载到本地", 404)
        path = row["file_path"]
        size = os.path.getsize(path)
        start, end = 0, size - 1
        range_header = self.headers.get("Range") or ""
        partial = False
        if range_header.startswith("bytes="):
            spec = range_header[len("bytes="):].split(",")[0].strip()
            first, _, last = spec.partition("-")
            try:
                if first:
                    start = int(first)
                    end = int(last) if last else size - 1
                elif last:                      # bytes=-N → 最后 N 字节
                    start = max(0, size - int(last))
                partial = 0 <= start <= end < size
            except ValueError:
                partial = False
            if not partial:
                start, end = 0, size - 1
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if not stream:
            name = os.path.basename(path)
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.end_headers()
        with open(path, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _serve_thumb(self, photo_id: str, size: int) -> None:
        row = self.api.store.query_one("SELECT * FROM photos WHERE group_id=? AND photo_id=?",
                                       (self.api.group_id, photo_id))
        if not row:
            return self._error("not found", 404)
        size = max(96, min(1024, size))
        thumb_dir = os.path.join(self.api.root_dir, "photos", THUMB_DIR_NAME)
        os.makedirs(thumb_dir, exist_ok=True)
        safe = re.sub(r"[^0-9A-Za-z._@()\-]+", "_", photo_id)[:80]
        cache_path = os.path.join(thumb_dir, f"{safe}_{size}.jpg")
        if os.path.isfile(cache_path):
            return self._file(cache_path)
        source = row["file_path"] if row["file_path"] and os.path.isfile(row["file_path"]) else None
        if source is None:
            # cache the CDN thumbnail locally so the grid works offline afterwards
            remote = (row["thumb_url"] or row["big_url"] or row["raw_url"] or "").replace("http://", "https://", 1)
            raw_dir = os.path.join(self.api.root_dir, "photos", THUMB_DIR_NAME, "_remote")
            raw_path = os.path.join(raw_dir, f"{safe}.img")
            if self._cache_image(remote, raw_path):
                source = raw_path
        if source:
            try:
                from PIL import Image
                with Image.open(source) as img:
                    img.draft("RGB", (size * 2, size * 2))
                    img = img.convert("RGB")
                    img.thumbnail((size, size))
                    img.save(cache_path, "JPEG", quality=82)
                return self._file(cache_path)
            except Exception:
                if source == row["file_path"]:
                    return self._file(source)
        url = row["thumb_url"] or row["big_url"] or row["raw_url"]
        if url:
            self.send_response(302)
            self.send_header("Location", url.replace("http://", "https://", 1))
            self.end_headers()
            return
        return self._error("no thumbnail", 404)

    # ------------------------------------------------------------- api: POST
    def _ai_filters(self, params: Dict[str, List[str]]) -> Dict[str, Any]:
        def one(key: str, default: str = "") -> str:
            values = params.get(key) or []
            return values[0] if values else default
        return {
            "album_id": one("album_id"),
            "uploader": one("uploader"),
            "date_from": one("date_from"),
            "date_to": one("date_to"),
        }

    def _decorate_results(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Attach the photo rows the UI needs, preserving similarity order."""
        if not items:
            return []
        ids = [i["photo_id"] for i in items]
        marks = ",".join("?" for _ in ids)
        rows = {r["photo_id"]: dict(r) for r in self.api.store.query(
            f"SELECT p.*, a.title AS album_title FROM photos p LEFT JOIN albums a "
            f"ON a.group_id=p.group_id AND a.album_id=p.album_id "
            f"WHERE p.group_id=? AND p.photo_id IN ({marks})",
            [self.api.group_id, *ids])}
        out = []
        for item in items:
            row = rows.get(item["photo_id"])
            if not row:
                continue
            row.pop("raw_json", None)
            row["score"] = item.get("score")
            row["has_file"] = bool(row.get("file_path")) and row.get("file_status") in ("done", "thumb-only")
            row["tags"] = [dict(t) for t in self.api.store.ai_tags_of_photo(
                self.api.group_id, item["photo_id"], limit=6, min_confidence=0.35)]
            caption = item.get("caption") or ""
            if not caption:
                stored = self.api.store.caption_of_photo(self.api.group_id, item["photo_id"])
                caption = (stored["caption"] if stored else "") or ""
            row["caption"] = caption
            out.append(row)
        return out

    def _ai_search(self, params: Dict[str, List[str]]) -> Dict[str, Any]:
        def one(key: str, default: str = "") -> str:
            values = params.get(key) or []
            return values[0] if values else default

        top_k = int(one("top_k", "60") or 60)
        query = one("q").strip()
        similar_to = one("similar_to").strip()
        ai_tag_ids = [int(v) for v in (params.get("ai_tag") or []) if v.isdigit()]
        min_conf = float(one("min_confidence", "0") or 0)
        filters = self._ai_filters(params)
        # 媒体类型：'' 全部 / videos 只看视频（靠封面照片命中）/ photos 只看非视频
        media = (one("media") or "").strip().lower()
        if media not in ("videos", "photos"):
            media = ""
        # 排序：relevance（默认，相似度）| similar（只看语义）| newest | oldest | uploader | interactions
        sort = (one("sort", "relevance") or "relevance").strip().lower()
        if sort not in AI_SORT_KEYS:
            sort = "relevance"
        # 标签/排序/媒体过滤会在候选池上再筛一道，所以要多取一些，
        # 否则 top_k=10 又要"只看视频"时可能筛完只剩 2 张
        pool_k = max(top_k, min(400, top_k * 4))

        def finish(items: List[Dict[str, Any]], key: str = "",
                   extra: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
            """统一收尾：媒体过滤 → 排序 → 截断（顺序不能反）。"""
            items = filter_by_media(self.api, items, media)
            items = self._ai_sort(items, key or sort)[:top_k]
            return items


        searcher = self.api.searcher
        usable, reason = searcher.usable()
        if not usable and (query or similar_to):
            # caption-only search still works without the vector index
            if query:
                caption_items = searcher.search_captions(query, top_k=pool_k, filters=filters)
                if caption_items:
                    decorated = self._decorate_results(caption_items)
                    return self._json({"mode": "caption", "total": len(decorated),
                                       "items": finish(decorated), "query": query,
                                       "filters": filters, "sort": sort, "media": media,
                                       "ai_tags": ai_tag_ids, "note": reason})
            return self._json({"error": reason}, 503)

        if query:
            combined = searcher.search_combined(query, top_k=pool_k, filters=filters)
            merged = combined["merged"]
            decorated = self._decorate_results(merged)
            for row, entry in zip(decorated, merged):
                row["match"] = entry.get("match")
                row["vector_score"] = entry.get("vector_score", 0.0)
                row["caption_score"] = entry.get("caption_score", 0.0)
                row["caption"] = entry.get("caption") or searcher._captions.get(row["photo_id"], "")
            items = self._ai_tag_filter(decorated, ai_tag_ids, min_conf)
            items = finish(items)
            return self._json({"mode": "text", "total": len(items), "items": items,
                               "query": query, "filters": filters,
                               "sort": sort, "media": media, "ai_tags": ai_tag_ids,
                               "semantic_count": len(combined["semantic"]),
                               "caption_count": len(combined["captions"])})
        elif similar_to:
            items = searcher.search_similar_to(similar_to, top_k=pool_k, filters=filters)
            mode = "similar"
        else:
            items = []
            mode = "browse"

        if mode == "browse":
            # no semantic query: just browse by AI tag / filters, newest first
            photo_ids: Optional[List[str]] = None
            if ai_tag_ids:
                sets = [set(searcher.photos_by_ai_tag(t, min_conf, limit=100000)) for t in ai_tag_ids]
                photo_ids = list(set.intersection(*sets)) if sets else []
            sql = ("SELECT p.*, a.title AS album_title FROM photos p LEFT JOIN albums a "
                   "ON a.group_id=p.group_id AND a.album_id=p.album_id "
                   "WHERE p.group_id=? AND p.file_status IN ('done','thumb-only')")
            sql_params: List[Any] = [self.api.group_id]
            if media == "videos":
                # 只看视频时直接按封面收敛，不必扫全表
                sql += (" AND p.photo_id IN (SELECT pv.photo_id FROM videos v "
                        "JOIN photos pv ON pv.group_id=v.group_id AND pv.photo_id=v.photo_id "
                        "WHERE v.group_id=?)")
                sql_params.append(self.api.group_id)
            sql += " ORDER BY p.upload_epoch DESC, p.photo_id LIMIT 4000"
            rows = self.api.store.query(sql, sql_params)
            out = []
            for row in rows:
                if photo_ids is not None and row["photo_id"] not in set(photo_ids):
                    continue
                item = dict(row)
                item.pop("raw_json", None)
                item["score"] = None
                item["tags"] = [dict(t) for t in self.api.store.ai_tags_of_photo(
                    self.api.group_id, item["photo_id"])[:5]]
                out.append(item)
                if len(out) >= pool_k:
                    break
            items = finish(out, "newest" if sort == "relevance" else sort)
            return self._json({"mode": mode, "total": len(items), "items": items,
                               "filters": filters, "sort": sort, "media": media,
                               "ai_tags": ai_tag_ids})

        items = self._ai_tag_filter(items, ai_tag_ids, min_conf)
        decorated = self._decorate_results(items)
        decorated = finish(decorated)
        return self._json({"mode": mode, "total": len(decorated), "items": decorated,
                           "query": query, "similar_to": similar_to, "sort": sort,
                           "media": media, "filters": filters, "ai_tags": ai_tag_ids})

    def _ai_tag_filter(self, items: List[Dict[str, Any]], ai_tag_ids: List[int],
                       min_conf: float) -> List[Dict[str, Any]]:
        """按 AI 标签取交集（放在截断之前，避免筛完剩不下几张）。"""
        if not ai_tag_ids:
            return items
        searcher = self.api.searcher
        allowed: Optional[set] = None
        for tag_id in ai_tag_ids:
            hits = set(searcher.photos_by_ai_tag(tag_id, min_conf, limit=100000))
            allowed = hits if allowed is None else allowed & hits
        allowed = allowed or set()
        return [i for i in items if i["photo_id"] in allowed]

    def _ai_sort(self, items: List[Dict[str, Any]], sort: str) -> List[Dict[str, Any]]:
        """给检索结果排序。

        为什么必须做：语义检索的相似度是**相对**的（SigLIP2 余弦普遍挤在 0.85~1.0），
        分数本身没什么区分度 —— 用户经常想按「最新的」「传得最多的」「有评论的」重排。
        """
        if not items:
            return items
        if sort == "similar":
            return sorted(items, key=lambda x: (-(x.get("vector_score") or 0.0),
                                                -(x.get("score") or 0.0), x["photo_id"]))
        if sort == "newest":
            return sorted(items, key=lambda x: (-(x.get("upload_epoch") or 0), x["photo_id"]))
        if sort == "oldest":
            return sorted(items, key=lambda x: ((x.get("upload_epoch") or 0), x["photo_id"]))
        if sort == "uploader":
            return sorted(items, key=lambda x: (str(x.get("uploader_nick") or x.get("uploader_uin") or ""),
                                                x["photo_id"]))
        if sort == "interactions":
            return sorted(items, key=lambda x: (-((x.get("like_count") or 0) + (x.get("comment_count") or 0)),
                                                -(x.get("upload_epoch") or 0), x["photo_id"]))
        # relevance
        return sorted(items, key=lambda x: (-(x.get("score") or 0.0),
                                            -(x.get("vector_score") or 0.0), x["photo_id"]))

    def _api_post(self, path: str, payload: Dict[str, Any]) -> None:
        api = self.api
        if path == "/api/sync":
            return self._json(api.start_sync(payload))
        if path == "/api/download":
            return self._json(api.start_download(payload))
        if path == "/api/download-videos":
            return self._json(api.start_video_download(payload))
        if path == "/api/cancel":
            return self._json(api.cancel())
        if path == "/api/connect":
            return self._json(api.connect())
        if path == "/api/tags":
            name = (payload.get("name") or "").strip()
            if not name:
                return self._error("tag name required")
            return self._json(api.add_tag(name, payload.get("color") or "#1677ff"))
        if path == "/api/tags/assign":
            photo_ids = payload.get("photo_ids") or []
            if not photo_ids and payload.get("photo_id"):
                photo_ids = [payload["photo_id"]]
            return self._json({"updated": api.tag_photos(int(payload["tag_id"]), photo_ids,
                                                        bool(payload.get("remove")))})
        if path == "/api/tags/auto":
            return self._json({"tags": api.auto_tag_by_uploader()})
        if path == "/api/ai/index":
            return self._json(api.start_ai_index(payload))
        if path == "/api/ai/caption":
            return self._json(api.start_caption(payload))
        if path == "/api/ai/cancel":
            api.indexer_obj.cancel()
            api.captioner_obj.cancel()
            return self._json({"cancelled": True})
        return self._error("unknown endpoint", 404)


def _csv_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    if any(ch in text for ch in ',"\n'):
        return '"' + text.replace('"', '""') + '"'
    return text


def find_free_port(preferred: int = 8787, host: str = "127.0.0.1") -> int:
    for port in [preferred] + list(range(preferred + 1, preferred + 40)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue
    raise RuntimeError("no free port found")


def serve(api: Api, host: str = "127.0.0.1", port: int = 8787,
          open_browser: bool = True) -> Tuple[ThreadingHTTPServer, str]:
    handler = type("BoundHandler", (Handler,), {"api": api})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    # 绑 0.0.0.0 时别把 "http://0.0.0.0:8787/" 当地址给浏览器 —— 那个地址打不开。
    # 本机自己访问永远用 127.0.0.1；对外的地址由调用方打印。
    shown = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    url = f"http://{shown}:{port}/"

    def _serve_forever() -> None:
        """把 serve_forever 包一层，异常绝不静默。

        serve_forever 跑在 daemon 线程里，主线程只是等一个 Event ——
        这个线程一旦异常退出，端口就没人监听了，而主线程还傻等在那里：
        表现为「进程还在、网页打不开」，没有任何报错。所以这里兜住异常并打印，
        让故障一眼可见（正常情况永远不会走到 except）。
        """
        try:
            httpd.serve_forever()
        except Exception as exc:  # noqa: BLE001
            print(f"\n[web] 服务线程异常退出：{type(exc).__name__}: {exc}", file=sys.stderr)
            import traceback
            traceback.print_exc()

    thread = threading.Thread(target=_serve_forever, name="qqalbum-web", daemon=True)
    thread.start()
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    return httpd, url
