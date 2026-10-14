"""Backup orchestration: crawl albums/photos/feeds/comments/likes into SQLite, then download files."""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional
from .cdp import CDPError, is_timeout_error
from .downloader import PhotoDownloader, VideoDownloader
from .qzone import NotLoggedIn, QzoneClient, QzoneError
from .store import Store, epoch_to_text, now_iso
from .video import VideoClient, VideoUnavailable


class SyncState:
    """Observable progress state shared with the WebUI."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.stage = "idle"
        self.message = ""
        self.error: Optional[str] = None
        self.current = 0
        self.total = 0
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.cancel = False
        self.counters: Dict[str, int] = {}
        self.log: List[str] = []

    def update(self, **kwargs: Any) -> None:
        with self.lock:
            for key, value in kwargs.items():
                setattr(self, key, value)

    def add_log(self, text: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {text}"
        with self.lock:
            self.log.append(line)
            self.log = self.log[-400:]
            self.message = text

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            elapsed = (self.finished_at or time.time()) - self.started_at if self.started_at else 0
            percent = (self.current / self.total * 100.0) if self.total else 0.0
            return {
                "running": self.running, "stage": self.stage, "message": self.message,
                "error": self.error, "current": self.current, "total": self.total,
                "percent": round(percent, 2), "elapsed": round(elapsed, 1),
                "counters": dict(self.counters), "log": list(self.log[-80:]),
            }


class BackupManager:
    """Owns the crawl + download pipeline for one group."""

    def __init__(self, group_id: str, store: Store, client_factory: Callable[[], QzoneClient],
                 root_dir: str, page_size: int = 300, feed_page_size: int = 10,
                 download_workers: int = 4, max_feeds: int = 200000):
        self.group_id = str(group_id)
        self.store = store
        self.client_factory = client_factory
        self.root_dir = root_dir
        self.page_size = page_size
        self.feed_page_size = feed_page_size
        self.download_workers = download_workers
        self.max_feeds = int(max_feeds)
        self.state = SyncState()
        self._client: Optional[QzoneClient] = None
        self._thread: Optional[threading.Thread] = None
        self._download_thread: Optional[threading.Thread] = None
        self._video_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------- lifecycle
    @property
    def client(self) -> QzoneClient:
        if self._client is None:
            self._client = self.client_factory()
            self._client.connect()
        return self._client

    def is_running(self) -> bool:
        return bool(self.state.running)

    def cancel(self) -> None:
        self.state.cancel = True
        self.state.add_log("已请求取消，将在当前步骤结束后停止…")

    def check_login(self) -> Dict[str, Any]:
        client = self.client
        return client.session_info

    # ---------------------------------------------------------------- threads
    def start_sync(self, albums: bool = True, photos: bool = True, feeds: bool = True,
                   members: bool = True, incremental: bool = True,
                   videos: bool = False) -> bool:
        if self.state.running:
            return False
        self._reset_state("crawl")
        self._thread = threading.Thread(target=self._sync_worker, name="qqalbum-sync",
                                        args=(albums, photos, feeds, members, incremental, videos),
                                        daemon=True)
        self._thread.start()
        return True

    def start_download(self, album_id: Optional[str] = None, limit: int = 0,
                       retry_failed: bool = True) -> bool:
        if self.state.running:
            return False
        self._reset_state("download")
        self._download_thread = threading.Thread(target=self._download_worker, name="qqalbum-download",
                                                 args=(album_id, limit, retry_failed), daemon=True)
        self._download_thread.start()
        return True

    def start_video_download(self, album_id: Optional[str] = None, limit: int = 0) -> bool:
        """下载视频。

        与图片下载分开的理由：视频地址是引擎现场签发的、会过期，而且需要机器人引擎在线。
        混进图片下载会让「引擎没开」把整批图片也带崩。
        """
        if self.state.running:
            return False
        self._reset_state("video-download")
        self._video_thread = threading.Thread(target=self._video_download_worker,
                                              name="qqalbum-video-download",
                                              args=(album_id, limit), daemon=True)
        self._video_thread.start()
        return True

    def _reset_state(self, stage: str) -> None:
        self.state.update(running=True, stage=stage, error=None, current=0, total=0,
                          started_at=time.time(), finished_at=None, cancel=False, counters={})
        self.state.log = []
        self.state.add_log(f"开始 {stage} …")

    def _page_responsive(self, client: QzoneClient) -> bool:
        try:
            return client.page.eval("1+1", timeout=10) == 2
        except (CDPError, QzoneError):
            return False

    def _with_restart(self, client: QzoneClient, state: SyncState, label: str, fn) -> Any:
        """Run one crawl step, restarting the browser if the page wedged, then retry.

        A hung QZone page cannot always be probed (the probe itself hangs), so a
        timeout-shaped error restarts unconditionally; other errors only restart when
        the page is confirmed dead. Login state lives in the profile dir, so a restart
        is always safe.
        """
        try:
            return fn()
        except (CDPError, QzoneError) as exc:
            state.add_log(f"{label} 失败：{exc}")
            if not is_timeout_error(exc) and self._page_responsive(client):
                state.add_log("页面仍可响应，按原错误处理")
                raise
            if is_timeout_error(exc):
                state.add_log("疑似页面卡死，重启浏览器后重试该步骤…")
            else:
                state.add_log("页面无响应，重启浏览器后重试该步骤…")
            client.hard_restart()
            state.add_log("浏览器已重启，继续")
            return fn()

    # ------------------------------------------------------------------- sync
    def _sync_members(self, client: QzoneClient, state: SyncState) -> None:
        """Resolve every QQ number we have seen (members + photo uploaders) to a nickname.

        `getGroupMember` only returns the current login user, while
        `qun_list_member_info` returns the whole roster in batches of ~60.
        """
        members = client.get_members()
        if members:
            self.store.upsert_members(self.group_id, members)
            state.add_log(f"群成员接口返回 {len(members)} 人（当前登录者）")

        uins: List[str] = []
        seen: set = set()

        def add(uin: Any) -> None:
            text = str(uin or "").strip()
            if text and text not in seen:
                seen.add(text)
                uins.append(text)

        for row in self.store.query(
                "SELECT DISTINCT uploader_uin FROM photos WHERE group_id=? AND uploader_uin<>''",
                (self.group_id,)):
            add(row["uploader_uin"])
        for row in self.store.query("SELECT uin FROM members WHERE group_id=?", (self.group_id,)):
            add(row["uin"])
        for row in self.store.query(
                "SELECT DISTINCT author_uin FROM comments WHERE group_id=? AND author_uin<>''",
                (self.group_id,)):
            add(row["author_uin"])

        if not uins:
            state.add_log("暂无可解析的成员")
            return

        total = 0
        batch_size = 60
        for start in range(0, len(uins), batch_size):
            if state.cancel:
                break
            chunk = uins[start:start + batch_size]
            try:
                infos = client.get_member_info(chunk)
            except QzoneError as exc:
                state.add_log(f"成员信息批次失败（{start}）：{exc}")
                continue
            if infos:
                total += self.store.upsert_members(self.group_id, infos)
            if start and start % 300 == 0:
                state.add_log(f"已解析 {start}/{len(uins)} 个 QQ 号昵称")
        state.counters["members"] = total
        state.add_log(f"已解析 {total} 个成员昵称")

    def _enrich_feeds(self, client: QzoneClient, state: SyncState, max_feeds: int = 600) -> None:
        """Fill in complete like lists / comments for feeds the list endpoint truncated.

        QZone's feed list returns only a few `likemans` per feed; the detail endpoint has
        them all but is rate limited, so a bounded number is fetched per run and the rest
        is picked up next time (`photos.like_count` already reflects the true total).
        """
        todo = self.store.query(
            "SELECT f.batch_id, f.album_id, f.busi_param, f.like_total, f.comment_total, "
            "(SELECT COUNT(*) FROM likes l WHERE l.group_id=f.group_id AND l.batch_id=f.batch_id) AS stored "
            "FROM feeds f WHERE f.group_id=? AND (f.like_total>0 OR f.comment_total>0) "
            "AND (SELECT COUNT(*) FROM likes l WHERE l.group_id=f.group_id AND l.batch_id=f.batch_id) "
            "< f.like_total ORDER BY f.feed_epoch DESC LIMIT ?", (self.group_id, int(max_feeds)))
        if not todo:
            state.add_log("评论/点赞明细已是最全，无需补充")
            return
        state.add_log(f"补充 {len(todo)} 条动态的完整点赞/评论明细（服务端限流，可多次运行逐步补全）")
        ok = 0
        pending_likers: List[str] = []
        for index, row in enumerate(todo, 1):
            if state.cancel:
                break
            try:
                busi = json.loads(row["busi_param"] or "{}")
            except (TypeError, ValueError):
                busi = {}
            try:
                detail = client.feed_detail(row["album_id"] or "", busi)
            except (QzoneError, CDPError) as exc:
                state.add_log(f"明细补充被中断（第 {index} 条）：{exc}")
                if not client._page_responsive():
                    state.add_log("页面无响应，重启浏览器后继续补充明细…")
                    try:
                        client.hard_restart()
                        continue
                    except (QzoneError, CDPError):
                        break
                break
            batch_id = row["batch_id"]
            if detail["likes"]:
                self.store.upsert_likes(self.group_id, batch_id,
                                        [dict(l, _batch_id=batch_id) for l in detail["likes"]])
                pending_likers.extend(l["uin"] for l in detail["likes"] if not l.get("nick"))
            if detail["comments"]:
                rows = []
                for comment in detail["comments"]:
                    rows.append(dict(comment, _batch_id=batch_id, source="feed-detail"))
                    for reply in comment.get("replies") or []:
                        rows.append(dict(reply, _batch_id=batch_id, is_reply=1,
                                         _parent_id=comment["id"], source="feed-detail-reply"))
                self.store.upsert_comments(self.group_id, rows)
            ok += 1
            if index % 25 == 0:
                state.add_log(f"已补充 {index}/{len(todo)} 条动态明细")
        state.add_log(f"本次补充完成 {ok} 条动态的评论/点赞明细")
        if pending_likers:
            # like lists carry QQ numbers only: resolve names in bulk
            unique = sorted(set(pending_likers))
            state.add_log(f"解析 {len(unique)} 个点赞者昵称…")
            resolved = 0
            for start in range(0, len(unique), 60):
                chunk = unique[start:start + 60]
                try:
                    infos = client.get_member_info(chunk)
                except (QzoneError, CDPError):
                    break
                if infos:
                    resolved += self.store.upsert_members(self.group_id, infos)
            state.add_log(f"点赞者昵称已解析 {resolved} 条")
            self.store.execute(
                "UPDATE likes SET nick=COALESCE((SELECT m.nick FROM members m WHERE m.group_id=likes.group_id "
                "AND m.uin=likes.uin AND m.nick<>''), nick) "
                "WHERE group_id=? AND (nick IS NULL OR nick='')", (self.group_id,))
            self.store.commit()

    def _flush_likes(self, likes: List[Dict[str, Any]]) -> int:
        """Store buffered likes, grouped by the feed they belong to."""
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for like in likes:
            batch_id = str(like.get("_batch_id") or "")
            if not batch_id:
                continue
            grouped.setdefault(batch_id, []).append(like)
        total = 0
        for batch_id, items in grouped.items():
            total += self.store.upsert_likes(self.group_id, batch_id, items, commit=False)
        if total:
            self.store.commit()
        return total

    def _sync_worker(self, do_albums: bool, do_photos: bool, do_feeds: bool,
                     do_members: bool, incremental: bool, do_videos: bool = False) -> None:
        state = self.state
        try:
            client = self.client
            session = client.session_info
            if not session.get("logged_in"):
                raise NotLoggedIn("浏览器尚未登录 QQ 空间，请先在自动打开的 Edge 窗口中扫码登录")
            state.add_log(f"已连接浏览器会话，uin={session.get('uin')}")

            server_time = client.get_server_time()
            offset = int(server_time - time.time()) if server_time else 0
            self.store.set_meta("server_time_offset", offset)
            state.add_log(f"服务器时间偏移 {offset}s")

            album_list: List[Dict[str, Any]] = []
            if do_albums:
                state.update(stage="albums")

                def _crawl_albums() -> None:
                    album_list.clear()
                    for album in client.iter_albums(page_size=100):
                        if state.cancel:
                            raise KeyboardInterrupt
                        album_list.append(album)
                        self.store.upsert_album(self.group_id, album, offset)

                self._with_restart(client, state, "相册列表", _crawl_albums)
                state.counters["albums"] = len(album_list)
                state.add_log(f"相册 {len(album_list)} 个")
            else:
                album_list = [dict(r) for r in self.store.query(
                    "SELECT * FROM albums WHERE group_id=?", (self.group_id,))]
                album_list = [{"id": a["album_id"], "title": a["title"], "photocnt": a["photo_count"]}
                              for a in album_list]

            if do_members:
                state.update(stage="members")
                try:
                    self._sync_members(client, state)
                except QzoneError as exc:
                    state.add_log(f"群成员获取失败（忽略）：{exc}")

            names = self.store.member_names(self.group_id)

            if do_photos:
                state.update(stage="photos")
                state.total = sum(int(a.get("photocnt") or a.get("photo_count") or 0) for a in album_list) or 0
                state.current = 0
                seen_photos = 0

                def _crawl_photos() -> int:
                    """Crawl albums from `start`; returns the index reached before failing."""
                    nonlocal seen_photos
                    idx = 0
                    for idx, album in enumerate(album_list, 1):
                        if state.cancel:
                            raise KeyboardInterrupt
                        album_id = str(album.get("id") or album.get("album_id") or "")
                        title = album.get("title") or album_id
                        expected = int(album.get("photocnt") or album.get("photo_count") or 0)
                        cursor_key = f"album_cursor:{self.group_id}:{album_id}"
                        cursor = 0
                        if incremental:
                            try:
                                cursor = int(self.store.get_meta(cursor_key, 0) or 0)
                            except (TypeError, ValueError):
                                cursor = 0
                        state.add_log(f"[{idx}/{len(album_list)}] 相册《{title}》 预计 {expected} 张"
                                      + (f"，增量从 {epoch_to_text(cursor)} 之后" if cursor else ""))
                        seen_ids: set = set()
                        newest = cursor
                        pending: List[Dict[str, Any]] = []

                        def _crawl_album() -> int:
                            """Fetch one album; safe to re-run after a browser restart."""
                            nonlocal newest
                            batch = pending
                            while batch:
                                self.store.upsert_photos(self.group_id, batch, names, offset,
                                                         commit=False)
                                batch = []
                            self.store.commit()
                            got = 0
                            batch = []
                            for photo in client.iter_photos(album_id, expected=expected,
                                                            page_size=self.page_size):
                                if state.cancel:
                                    raise KeyboardInterrupt
                                photo_key = str(photo.get("id") or "")
                                if photo_key in seen_ids:
                                    continue
                                seen_ids.add(photo_key)
                                if cursor:
                                    epoch = int(photo.get("uploadtime") or 0)
                                    if epoch and epoch <= cursor:
                                        # photos are newest-first: the rest is already stored
                                        state.add_log("    增量：到达已有数据边界"
                                                      f"（{epoch_to_text(epoch)}），停止该相册")
                                        break
                                batch.append(photo)
                                newest = max(newest, int(photo.get("uploadtime") or 0))
                                if len(batch) >= 200:
                                    self.store.upsert_photos(self.group_id, batch, names, offset,
                                                             commit=False)
                                    got += len(batch)
                                    batch = []
                                    self.store.commit()
                                    state.current = seen_photos + got
                                    state.counters["photos"] = state.current
                            pending.clear()
                            pending.extend(batch)  # kept for a possible retry
                            self.store.upsert_photos(self.group_id, batch, names, offset, commit=False)
                            self.store.commit()
                            pending.clear()
                            got += len(batch)
                            return got

                        got = self._with_restart(client, state, f"相册《{title}》", _crawl_album)
                        if newest:
                            self.store.set_meta(cursor_key, newest)
                        self.store.execute(
                            "UPDATE albums SET photo_count=MAX(COALESCE(photo_count,0), ?), "
                            "downloaded=1, last_sync_at=? WHERE group_id=? AND album_id=?",
                            (expected, now_iso(), self.group_id, album_id))
                        self.store.commit()
                        seen_photos += got
                        state.current = seen_photos
                        state.counters["photos"] = seen_photos
                        state.add_log(f"    《{title}》 已入库 {got} 张")
                    return idx

                self._with_restart(client, state, "照片抓取", _crawl_photos)

            if do_feeds:
                state.update(stage="feeds")
                cursor = self.store.get_meta(f"feed_cursor:{self.group_id}", "") if incremental else ""
                state.add_log("开始抓取群动态（评论/点赞/上传者）…")

                def feed_progress(count: int, total: int) -> None:
                    state.current = count
                    state.total = total or state.total
                    state.counters["feeds"] = count

                comments: List[Dict[str, Any]] = []
                likes: List[Dict[str, Any]] = []
                feeds_seen = 0
                stop_flag = {"stop": False}
                feed_error: Optional[str] = None
                if cursor:
                    state.add_log(f"增量模式：从 attach_info {str(cursor)[:60]} 继续")
                restart_tries = 0
                while True:
                    try:
                        for feed in client.iter_feeds(page_size=self.feed_page_size,
                                                      progress=feed_progress,
                                                      max_items=self.max_feeds - feeds_seen,
                                                      start_attach=str(cursor or ""),
                                                      should_stop=lambda: state.cancel or stop_flag["stop"]):
                            if state.cancel:
                                raise KeyboardInterrupt
                            batch_id = feed.get("_batch_id") or ""
                            self.store.upsert_feed(self.group_id, feed, offset)
                            self.store.upsert_feed_photos(self.group_id, batch_id, feed.get("pics") or [])
                            for comment in feed.get("comments") or []:
                                comment["_batch_id"] = batch_id
                                comment["_album_id"] = (feed.get("album") or {}).get("album_id") or ""
                                comments.append(comment)
                                for reply in comment.get("replies") or []:
                                    reply_row = dict(reply)
                                    reply_row["is_reply"] = 1
                                    reply_row["_batch_id"] = batch_id
                                    reply_row["_album_id"] = comment["_album_id"]
                                    reply_row["_parent_id"] = comment["id"]
                                    reply_row["source"] = "feed-reply"
                                    comments.append(reply_row)
                            for like in feed.get("likes") or []:
                                like["_batch_id"] = batch_id
                                likes.append(like)
                            if len(comments) >= 500:
                                self.store.upsert_comments(self.group_id, comments, offset, commit=False)
                                comments = []
                            if len(likes) >= 500:
                                self._flush_likes(likes)
                                likes = []
                            feeds_seen += 1
                            # Only move the cursor for genuinely new content, otherwise an
                            # interrupted run would replay already-imported pages.
                            if getattr(client, "new_feeds_since_chunk", 0) > 0:
                                candidate = getattr(client, "last_feed_attach_before", "") or cursor
                                if candidate:
                                    cursor = candidate
                            if feeds_seen % 200 == 0:
                                if cursor:
                                    self.store.set_meta(f"feed_cursor:{self.group_id}", cursor)
                                state.add_log(f"动态进度 {feeds_seen} 条（游标已保存）")
                        break
                    except CDPError as exc:
                        # a wedged page or a dropped browser session: restart and resume
                        restart_tries += 1
                        if restart_tries > 6:
                            raise
                        self.store.upsert_comments(self.group_id, comments, offset, commit=False)
                        if likes:
                            self._flush_likes(likes)
                        comments = []
                        likes = []
                        self.store.set_meta(f"feed_cursor:{self.group_id}", cursor or "")
                        state.add_log(f"浏览器会话中断（{exc}），第 {restart_tries} 次重启浏览器后续传…")
                        try:
                            client.hard_restart()
                        except (CDPError, QzoneError) as restart_exc:
                            state.add_log(f"重启失败：{restart_exc}")
                            raise exc
                    except QzoneError as exc:
                        text = str(exc)
                        if "请求超时" in text or "timeout" in text.lower() or "HTML" in text:
                            restart_tries += 1
                            if restart_tries > 6:
                                feed_error = text
                                state.add_log(f"动态抓取反复失败，停止：{text}")
                                break
                            state.add_log(f"动态接口异常（{text}），重启浏览器后续传（第 {restart_tries} 次）…")
                            try:
                                client.hard_restart()
                            except (CDPError, QzoneError):
                                feed_error = text
                                break
                            continue
                        # server-side throttling ("非该群成员…"): keep what we have
                        feed_error = text
                        state.add_log(f"动态抓取在第 {feeds_seen} 条被服务端中断：{text}")
                        state.add_log("已保留已抓取的数据，稍后可再次运行继续。")
                        break
                if comments:
                    self.store.upsert_comments(self.group_id, comments, offset)
                    comments = []
                if likes:
                    self._flush_likes(likes)
                    likes = []
                state.counters["feeds"] = feeds_seen
                state.add_log(f"动态 {feeds_seen} 条" + (f"（中断：{feed_error}）" if feed_error else ""))
                if not feed_error:
                    self.store.set_meta(f"feed_cursor:{self.group_id}", "")

            if do_photos:
                linked = self.store.link_photos_to_feeds(self.group_id)
                state.add_log(f"已把 {linked} 张照片的评论/点赞数关联到动态记录")
                state.update(stage="feed-detail")
                self._enrich_feeds(client, state)
            if do_videos:
                self._sync_videos(state, album_list)

            state.add_log("重建全文搜索索引…")
            self.store.rebuild_fts(self.group_id)

            stats = self.store.stats(self.group_id)
            vstats = self.store.video_stats(self.group_id)
            self.store.upsert_group(self.group_id, album_count=stats["albums"], photo_count=stats["photos"],
                                    member_count=stats["members"] or state.counters.get("members", 0),
                                    server_time_offset=offset,
                                    last_sync_at=now_iso(), last_sync_epoch=int(time.time()))
            state.add_log(f"完成：相册 {stats['albums']}，照片 {stats['photos']}，评论 {stats['comments']}，"
                          f"点赞 {stats['likes']}"
                          + (f"，视频 {vstats['total']}" if do_videos else ""))
            state.update(running=False, stage="done", finished_at=time.time())
        except KeyboardInterrupt:
            self.state.update(running=False, stage="cancelled", finished_at=time.time())
            self.state.add_log("已取消")
        except NotLoggedIn as exc:
            self.state.update(running=False, stage="error", error=str(exc), finished_at=time.time())
            self.state.add_log(f"错误：{exc}")
        except (QzoneError, CDPError, OSError) as exc:
            self.state.update(running=False, stage="error", error=f"{type(exc).__name__}: {exc}",
                              finished_at=time.time())
            self.state.add_log(f"错误：{type(exc).__name__}: {exc}")
        except Exception as exc:  # keep the UI informed instead of dying silently
            self.state.update(running=False, stage="error", error=f"{type(exc).__name__}: {exc}",
                              finished_at=time.time())
            self.state.add_log(f"未预期错误：{type(exc).__name__}: {exc}")

    # --------------------------------------------------------------- videos
    def _sync_videos(self, state: SyncState, album_list: List[Dict[str, Any]]) -> None:
        """收集视频元数据（来自机器人引擎 SnowLuma）。

        设计取舍：**引擎不可用时只记一行日志、不算失败** —— 视频是附加能力，
        不该让整个元数据抓取失败（那会让用户以为照片也没抓到）。
        """
        state.update(stage="videos")
        state.add_log("开始抓取视频清单（来自机器人引擎 SnowLuma）…")
        try:
            client = VideoClient(self.group_id, log=state.add_log)
            client.connect()
        except VideoUnavailable as exc:
            state.add_log(f"跳过视频清单：{exc}")
            return
        except Exception as exc:
            state.add_log(f"跳过视频清单：{type(exc).__name__}: {exc}")
            return

        try:
            albums = album_list or client.albums()
        except Exception as exc:
            state.add_log(f"取相册列表失败，跳过视频：{type(exc).__name__}: {exc}")
            return

        total_albums = len(albums)
        seen: Dict[str, Dict[str, Any]] = {}
        for index, album in enumerate(albums, 1):
            if state.cancel:
                break
            album_id = str(album.get("id") or album.get("album_id") or "")
            title = str(album.get("title") or album.get("name") or album_id)
            if not album_id:
                continue
            state.update(current=index, total=total_albums,
                         message=f"视频清单 {index}/{total_albums}《{title}》")
            try:
                for video in client.album_videos(album_id, title):
                    # 同一个视频可能挂在多个相册下，按 video_id 去重
                    seen.setdefault(video["video_id"], video)
            except Exception as exc:
                state.add_log(f"《{title}》视频清单失败：{type(exc).__name__}: {exc}")
            if index % 10 == 0:
                state.add_log(f"视频清单进度 {index}/{total_albums}，已发现 {len(seen)} 个视频")

        if seen:
            self.store.upsert_videos(self.group_id, list(seen.values()))
            linked = self.store.link_videos_to_photos(self.group_id)
            state.add_log(f"视频 {len(seen)} 个已入库，其中 {linked} 个对上了封面照片")
        else:
            state.add_log("没有发现视频（也可能引擎里没有该群的视频信息）")
        state.counters["videos"] = len(seen)

    def _video_download_worker(self, album_id: Optional[str], limit: int) -> None:
        state = self.state
        try:
            rows = self.store.videos_missing_files(self.group_id, album_id, limit=limit)
            state.total = len(rows)
            if not rows:
                state.update(running=False, stage="done", finished_at=time.time())
                state.add_log("没有待下载的视频")
                return
            state.add_log(f"待下载 {len(rows)} 个视频")

            # 地址会过期，所以提供一个"现场重取"的回调；引擎连不上时给 None，只试入库的地址
            client: Optional[VideoClient] = None
            try:
                client = VideoClient(self.group_id, log=state.add_log)
                client.connect()
            except VideoUnavailable as exc:
                state.add_log(f"警告：{exc}；只能尝试数据库里存过的地址（多半已过期）")
            except Exception as exc:
                state.add_log(f"警告：视频引擎不可用（{type(exc).__name__}: {exc}）")

            def refresh(row: Any) -> str:
                if client is None:
                    return ""
                try:
                    url = client.fresh_url(str(row["album_id"] or ""), str(row["video_id"]),
                                           str(row["album_title"] or ""))
                    if url:
                        self.store.set_video_url(self.group_id, str(row["video_id"]), url)
                    return url
                except Exception as exc:
                    state.add_log(f"重取地址失败：{type(exc).__name__}: {exc}")
                    return ""

            downloader = VideoDownloader(self.store, self.root_dir, workers=2,
                                         log=state.add_log, refresh=refresh)
            done = downloader.run(rows, group_id=self.group_id,
                                  should_stop=lambda: state.cancel,
                                  progress=lambda n, total: state.update(current=n, total=total))
            stats = self.store.video_stats(self.group_id)
            state.counters["videos_downloaded"] = done
            state.update(running=False, stage="done", finished_at=time.time())
            state.add_log(f"视频下载结束：本次成功 {done} 个，"
                          f"库内共 {stats['done']}/{stats['total']} 已下载")
        except Exception as exc:
            state.update(running=False, stage="error", error=f"{type(exc).__name__}: {exc}",
                         finished_at=time.time())
            state.add_log(f"视频下载错误：{type(exc).__name__}: {exc}")

    # --------------------------------------------------------------- download
    def _download_worker(self, album_id: Optional[str], limit: int, retry_failed: bool) -> None:
        state = self.state
        try:
            rows = self.store.photos_missing_files(self.group_id, album_id, only_original=True, limit=limit)
            if not retry_failed:
                rows = [r for r in rows if (r["file_status"] or "") != "error"]
            state.total = len(rows)
            state.add_log(f"待下载 {len(rows)} 个原图")
            downloader = PhotoDownloader(self.store, self.root_dir, workers=self.download_workers,
                                         log=state.add_log)
            done = downloader.run(rows, group_id=self.group_id,
                                  should_stop=lambda: state.cancel,
                                  progress=lambda n, total: state.update(current=n, total=total))
            state.counters["downloaded"] = done
            state.update(running=False, stage="done", finished_at=time.time())
            state.add_log(f"下载结束：成功 {done} 个")
        except Exception as exc:
            state.update(running=False, stage="error", error=f"{type(exc).__name__}: {exc}",
                         finished_at=time.time())
            state.add_log(f"下载错误：{type(exc).__name__}: {exc}")
