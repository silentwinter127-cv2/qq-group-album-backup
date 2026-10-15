"""QZone group-album API client driven through a logged-in browser page (CDP)."""
from __future__ import annotations

import html
import json
import time
import urllib.parse
from typing import Any, Callable, Dict, Iterable, List, Optional

from .cdp import Browser, CDPError, PageSession, is_timeout_error
from .js_bridge import batch_id_of, flatten_feed, js_bridge, js_call, parse_attach_info

H5_BASE = "https://h5.qzone.qq.com"
INDEX_URL = H5_BASE + "/groupphoto/index?inqq=3&groupId={gid}"
LOGIN_URL = "https://i.qq.com/"
DEFAULT_PROFILE = r"D:\QQAlbumBackup\edge-profile"


class QzoneError(RuntimeError):
    pass


class NotLoggedIn(QzoneError):
    pass


def unwrap_response(result: Any) -> Dict[str, Any]:
    """The bridge returns CDP {value: {...}}; normalize to a plain dict."""
    if isinstance(result, dict) and "value" in result and isinstance(result["value"], dict):
        return result["value"]
    if isinstance(result, dict):
        return result
    raise QzoneError(f"unexpected bridge result: {result!r}")


def _unescape(value: Any) -> Any:
    if isinstance(value, str):
        return html.unescape(value)
    return value


def normalize_adapter_feed(feed: Dict[str, Any]) -> Dict[str, Any]:
    """Convert QZone's adapter feed object into the crawler's internal shape.

    The adapter emits ``{fid, time, user, msg:{album_id, album_name, pics[]},
    comments[], like[], comment_total, like_total, is_liked, busi_param}``.
    """
    msg = feed.get("msg") or {}
    pics = []
    for pic in msg.get("pics") or []:
        pics.append({
            "pic_key": _unescape(pic.get("pic_key") or ""),
            "url": _unescape(pic.get("url") or ""),
            "width": pic.get("bwidth") or pic.get("width"),
            "height": pic.get("bheight") or pic.get("height"),
            "videoflag": pic.get("videoflag") or 0,
            "videostatus": pic.get("videostatus") or 0,
            "videourl": _unescape(pic.get("videourl") or ""),
        })

    comments = []
    for comment in feed.get("comments") or []:
        comments.append({
            "id": str(comment.get("id") or ""),
            "content": _unescape(comment.get("content") or ""),
            "time": int(comment.get("time") or 0),
            "reply_total": int(comment.get("reply_total") or 0),
            "uin": str((comment.get("user") or {}).get("uin") or ""),
            "nick": _unescape((comment.get("user") or {}).get("nick") or ""),
            "replies": [{
                "id": str(reply.get("id") or ""),
                "content": _unescape(reply.get("content") or ""),
                "time": int(reply.get("time") or 0),
                "uin": str((reply.get("user") or {}).get("uin") or ""),
                "nick": _unescape((reply.get("user") or {}).get("nick") or ""),
            } for reply in (comment.get("replies") or [])],
        })

    likes = []
    for like in feed.get("like") or []:
        likes.append({
            "uin": str(like.get("uin") or (like.get("user") or {}).get("uin") or ""),
            "nick": _unescape(like.get("nick") or (like.get("user") or {}).get("nickname") or ""),
            "id": like.get("id") or "",
        })

    busi = feed.get("busi_param") or {}
    if isinstance(busi, str):
        try:
            busi = json.loads(busi)
        except (ValueError, TypeError):
            busi = {}
    batch_id = str(busi.get("21") or feed.get("fid") or "")
    user = feed.get("user") or {}
    # `user` may be a dict (adapter) or a list (raw singlefeed)
    if isinstance(user, list):
        user = user[0] if user else {}

    normalized = {
        "fid": str(feed.get("fid") or ""),
        "batch_id": batch_id,
        "_batch_id": batch_id,
        "org_like_key": (likes[0]["id"] if likes else ""),
        "time": int(feed.get("time") or 0),
        "user": {"uin": str(user.get("uin") or ""), "nick": _unescape(user.get("nick") or "")},
        "summary": _unescape(msg.get("text") or feed.get("summary") or ""),
        "album": {
            "album_id": str(msg.get("album_id") or ""),
            "album_name": _unescape(msg.get("album_name") or ""),
            "album_desc": _unescape(msg.get("fdesc") or ""),
            "photo_num": int(msg.get("photo_num") or 0),
        },
        "pics": pics,
        "comments": comments,
        "comment_total": int(feed.get("comment_total") or 0),
        "likes": likes,
        "like_total": int(feed.get("like_total") or 0),
        "is_liked": int(feed.get("is_liked") or 0),
        "busi_param": busi,
    }
    normalized["_raw"] = feed
    return normalized



class QzoneClient:
    """Talks to QZone group albums using a real browser session."""

    def __init__(self, group_id: str, profile_dir: str = DEFAULT_PROFILE,
                 port: int = 9222, browser_path: Optional[str] = None,
                 headless: bool = False, log: Optional[Callable[[str], None]] = None):
        self.group_id = str(group_id)
        self.index_url = INDEX_URL.format(gid=self.group_id)
        self.browser = Browser(port=port, profile_dir=profile_dir, browser_path=browser_path,
                               headless=headless)
        self._page: Optional[PageSession] = None
        self._owns_page = False
        self.log = log or (lambda msg: None)
        self.session_info: Dict[str, Any] = {}
        self.last_feed_attach: str = ""
        self.last_feed_attach_before: str = ""
        self.new_feeds_since_chunk: int = 0

    # ------------------------------------------------------------------ setup
    def connect(self) -> Dict[str, Any]:
        """Attach to (or start) the browser and open the group album page."""
        self.browser.launch(self.index_url)
        page = self.browser.page(self.index_url)
        page.enable_domains()
        if "groupphoto" not in (page.url() or ""):
            page.navigate(self.index_url, wait=3.0)
        page.wait_ready(45)
        self._page = page
        self._owns_page = True
        # wait for the page bootstrap to define its globals
        for _ in range(20):
            if page.eval("!!window.GroupZone", timeout=10):
                break
            time.sleep(0.5)
        page.eval(js_bridge(), timeout=60)
        self.session_info = self._probe_session()
        if not self._page_responsive():
            self.log("页面无响应，正在重启浏览器以恢复…")
            self.hard_restart()
        return self.session_info

    def _page_responsive(self, timeout: float = 20.0) -> bool:
        try:
            return self.page.eval("1+1", timeout=timeout) == 2
        except (CDPError, QzoneError):
            return False

    def hard_restart(self) -> Dict[str, Any]:
        """Restart the browser process and re-open the album page (recovers wedged fetch)."""
        deadline = time.time() + 15
        while self.browser.is_up() and time.time() < deadline:
            time.sleep(0.3)
        self._page = None
        self._owns_page = False
        self.browser._browser_ws = None
        self.browser.restart(self.index_url)
        self.browser._browser_ws = None
        return self.connect()


    def _probe_session(self) -> Dict[str, Any]:
        info = self.page.eval("JSON.stringify(window.QQAlbum.tk())", timeout=30)
        data = json.loads(info or "{}")
        data["url"] = self.page.url()
        data["logged_in"] = bool(data.get("uin")) and "i.qq.com" not in (data["url"] or "")
        return data

    @property
    def page(self) -> PageSession:
        if self._page is None:
            raise QzoneError("call connect() first")
        return self._page

    def close(self) -> None:
        if self._owns_page and self._page is not None:
            self._page.detach()
        self._page = None

    def call(self, method: str, *args: Any, timeout: float = 180.0) -> Dict[str, Any]:
        """Invoke a bridge method and return its JS object.

        A wedged QZone page (or a CDP session invalidated by a browser restart) is
        detected and healed by restarting the browser and re-attaching, so callers do
        not need their own recovery logic for single calls.
        """
        expr = "(async()=>JSON.stringify(await %s))()" % js_call(method, *args)
        try:
            raw = self.page.eval(expr, timeout=timeout)
        except (CDPError, QzoneError) as exc:
            if not is_timeout_error(exc):
                raise
            self.log(f"{method}: 浏览器无响应（{exc}），重启浏览器并重新附加…")
            self.hard_restart()
            raw = self.page.eval(expr, timeout=timeout)
        if raw is None:
            raise QzoneError(f"{method}: empty result (page may have navigated)")
        return json.loads(raw)

    def reload_bridge(self) -> None:
        self.page.eval(js_bridge(), timeout=60)

    def _guard(self, res: Dict[str, Any], what: str) -> Dict[str, Any]:
        status = res.get("status")
        raw = str(res.get("raw") or "")
        if raw and ("<html" in raw.lower() or "</html>" in raw.lower()):
            raise QzoneError(f"{what}: 登录态失效或未登录（返回了 HTML 页面）")
        if status not in (200, None) and not res.get("json"):
            raise QzoneError(f"{what}: HTTP {status} {raw[:200]}")
        code = res.get("code")
        if code not in (0, None):
            raise QzoneError(f"{what}: code={code} message={res.get('message')}")
        return res

    # ----------------------------------------------------------------- albums
    def iter_albums(self, page_size: int = 100, max_pages: int = 100) -> Iterable[Dict[str, Any]]:
        seen: set = set()
        for _ in range(max_pages):
            res = self._guard(self.call("getAlbums", self.group_id, 0, page_size), "getAlbums")
            albums = res.get("album") or []
            fresh = [a for a in albums if str(a.get("id")) not in seen]
            for a in fresh:
                seen.add(str(a.get("id")))
            for a in fresh:
                yield a
            if len(albums) < page_size or not fresh:
                break

    def get_albums(self, page_size: int = 100) -> List[Dict[str, Any]]:
        return list(self.iter_albums(page_size=page_size))

    # ----------------------------------------------------------------- photos
    def iter_photos(self, album_id: str, expected: Optional[int] = None,
                    page_size: int = 500, progress: Optional[Callable[[int, int], None]] = None,
                    should_stop: Optional[Callable[[], bool]] = None) -> Iterable[Dict[str, Any]]:
        start = 0
        total = 0
        while True:
            res = self._guard(self.call("getPhotos", self.group_id, album_id, start, page_size),
                              f"getPhotos({album_id})")
            photos = res.get("photos") or []
            if not photos:
                break
            for p in photos:
                p["_album_id"] = album_id
                yield p
            total += len(photos)
            if progress:
                progress(total, expected or 0)
            if should_stop and should_stop():
                return
            if len(photos) < page_size:
                break
            if expected and total >= expected:
                break
            start += len(photos)
        return

    def count_photos(self, album_id: str) -> int:
        try:
            res = self._guard(self.call("getPhotos", self.group_id, album_id, 0, 1), "countPhotos")
        except QzoneError:
            return 0
        return int(res.get("total") or 0)

    # ------------------------------------------------------------------ feeds
    def iter_feeds(self, page_size: int = 10, max_items: int = 200000,
                   progress: Optional[Callable[[int, int], None]] = None,
                   should_stop: Optional[Callable[[], bool]] = None,
                   feed_type: int = 1, start_attach: str = "",
                   chunk_pages: int = 20) -> Iterable[Dict[str, Any]]:
        """Walk the group feed backwards (newest first) collecting comments/likes.

        QZone serves the group feed through a JSONP endpoint that rejects direct
        fetch/XHR calls from outside its own wrapper, so the page's own
        ``GroupZone.GPHOTO.logic.getFeeds()`` is used. The server caps a page at 10
        items, and the ``attach_info`` cursor carries the continuation offset.
        Because one page costs one CDP round trip, several pages are fetched inside
        a single page-side loop (`chunk_pages`).
        """
        attach = start_attach or ""
        seen: set = set()
        count = 0
        total = 0
        empty_rounds = 0
        self.last_feed_attach = attach

        while count < max_items:
            if should_stop and should_stop():
                return
            attach_before = attach
            self.new_feeds_since_chunk = 0
            chunk = self._feed_chunk(attach, feed_type, page_size, chunk_pages)
            feeds = chunk.get("feeds") or []
            attach = chunk.get("attach") or ""
            info = parse_attach_info(attach)
            if info.get("total"):
                total = int(info["total"])
            self.last_feed_attach = attach
            self.last_feed_attach_before = attach_before
            if progress:
                progress(count, total)
            if not feeds:
                empty_rounds += 1
                if empty_rounds >= 2 or not attach:
                    break
            fresh = 0
            for feed in feeds:
                key = feed.get("_batch_id") or feed.get("fid") or ""
                if not key or key in seen:
                    continue
                seen.add(key)
                fresh += 1
                self.new_feeds_since_chunk = fresh
                count += 1
                yield feed
                if count >= max_items:
                    return
            if not fresh:
                # no new content in this chunk: the cursor itself moved forward
                self.last_feed_attach_before = attach
            if should_stop and should_stop():
                return
            offset = int(info.get("offset") or 0)
            if offset <= 0 or (total and offset >= total):
                break
            if not attach:
                break
        return

    def _feed_chunk(self, attach: str, feed_type: int, page_size: int,
                    pages: int) -> Dict[str, Any]:
        """Fetch up to `pages` feed pages inside one CDP round trip."""
        expr = (
            "(async()=>{try{"
            "const G=window.GroupZone.GPHOTO;"
            "let at=%s,all=[],guard=0;"
            "while(guard<%d){guard++;"
            "const d=await G.logic.getFeeds({gid:G.groupId,type:%d,getcnt:%d,curreuestinfo:at});"
            "const f=(d&&d.feeds)||[];"
            "const next=String((d&&d.curreuestinfo)||'');"
            "for(const x of f) all.push(x);"
            "if(!f.length||!next||next===at) break;"
            "at=next;"
            "const m=at.match(/offset=(\\d+)/);"
            "const t=at.match(/total=(\\d+)/);"
            "if(m&&t&&parseInt(m[1])>=parseInt(t)) break;"
            "}"
            "return JSON.stringify({feeds:all,attach:at});"
            "}catch(e){return JSON.stringify({feeds:[],attach:'',err:String(e&&(e.message||e))});}})()"
            % (json.dumps(attach), max(1, int(pages)), feed_type, max(10, int(page_size)))
        )
        raw = self.page.eval(expr, timeout=300)
        try:
            payload = json.loads(raw or "{}")
        except ValueError:
            raise QzoneError("getFeeds: 返回内容无法解析")
        if payload.get("err"):
            raise QzoneError(f"getFeeds: {payload['err']}")
        payload["feeds"] = [normalize_adapter_feed(f) for f in (payload.get("feeds") or [])]
        return payload

    def _feeds_page(self, attach: str, feed_type: int, page_size: int) -> Optional[tuple]:
        """Fetch one page through the page adapter and normalise the feed objects."""
        expr = (
            "(async()=>{try{"
            "const G=window.GroupZone.GPHOTO;"
            "const d=await G.logic.getFeeds({gid:G.groupId,type:%d,getcnt:%d,curreuestinfo:%s});"
            "return JSON.stringify({ok:true,feeds:(d&&d.feeds)||[],attach:String((d&&d.curreuestinfo)||'')});"
            "}catch(e){return JSON.stringify({ok:false,err:String(e&&(e.message||e))});}})()"
            % (feed_type, max(10, int(page_size)), json.dumps(attach))
        )
        payload = json.loads(self.page.eval(expr, timeout=240) or "{}")
        if not payload.get("ok"):
            raise QzoneError(f"getFeeds: {payload.get('err')}")
        feeds = [normalize_adapter_feed(f) for f in (payload.get("feeds") or [])]
        return payload, feeds, payload.get("attach") or ""

    def _feeds_payload(self, attach: str) -> List[Dict[str, Any]]:
        return self._feeds_page(attach, 1, 10)[1]


    def feed_detail(self, album_id: str, busi_param: Dict[str, Any],
                    refresh_type: int = 3, count: int = 100) -> Dict[str, Any]:
        """Fetch one feed's full detail (complete like list and comments).

        The feed list truncates `likemans`; QZone's detail endpoint returns the complete
        `batchLikes` (QQ numbers only) and `batchComments`. It is rate limited, so callers
        should use it sparingly.
        """
        expr = (
            "(async()=>{try{"
            "const G=window.GroupZone.GPHOTO;"
            "const d=await G.logic.getFeedDetail({qunId:G.groupId,albumId:%s,busi_param:%s,"
            "refresh_type:%d,count:%d});"
            "const b=(d&&d.batch)||{};"
            "return JSON.stringify({ok:true,"
            "likes:(b.batchLikes)||[],likemans:(b.likemans)||[],"
            "comments:(b.batchComments)||b.commments||[],"
            "totalLikes:b.totalLikes||0,totalcomment:b.totalcomment||0});"
            "}catch(e){return JSON.stringify({ok:false,err:String(e&&(e.message||e))});}})()"
            % (json.dumps(album_id), json.dumps(busi_param or {}), int(refresh_type), int(count))
        )
        payload = json.loads(self.page.eval(expr, timeout=180) or "{}")
        if not payload.get("ok"):
            raise QzoneError(f"getFeedDetail: {payload.get('err')}")

        likes: List[Dict[str, Any]] = []
        for item in (payload.get("likemans") or []):
            user = item.get("user") or {}
            uin = str(item.get("id") or user.get("uin") or "")
            nick = item.get("nick") or user.get("nickname") or user.get("name") or ""
            if uin:
                likes.append({"uin": uin, "nick": html.unescape(nick)})
        for item in (payload.get("likes") or []):
            uin = str(item.get("id") or item.get("uin") or
                      (item.get("user") or {}).get("uin") or "")
            nick = item.get("nick") or (item.get("user") or {}).get("name") or ""
            if uin and not any(l["uin"] == uin for l in likes):
                likes.append({"uin": uin, "nick": html.unescape(nick)})

        comments: List[Dict[str, Any]] = []
        for item in (payload.get("comments") or []):
            poster = item.get("poster") or item.get("user") or {}
            comment = {
                "id": str(item.get("id") or item.get("commentid") or ""),
                "content": html.unescape(item.get("content") or ""),
                "time": int(item.get("postTime") or item.get("date") or 0),
                "reply_total": int(item.get("totalreply") or item.get("replynum") or 0),
                "uin": str(poster.get("id") or poster.get("uin") or ""),
                "nick": html.unescape(poster.get("name") or poster.get("nickname") or ""),
                "replies": [],
            }
            for reply in (item.get("replies") or item.get("replys") or []):
                rposter = reply.get("poster") or reply.get("user") or {}
                comment["replies"].append({
                    "id": str(reply.get("id") or reply.get("replyid") or ""),
                    "content": html.unescape(reply.get("content") or ""),
                    "time": int(reply.get("postTime") or reply.get("date") or 0),
                    "uin": str(rposter.get("id") or rposter.get("uin") or ""),
                    "nick": html.unescape(rposter.get("name") or rposter.get("nickname") or ""),
                })
            comments.append(comment)
        return {"likes": likes,
                "comments": [c for c in comments if c["id"]],
                "like_total": int(payload.get("totalLikes") or 0),
                "comment_total": int(payload.get("totalcomment") or 0)}

    # ---------------------------------------------------------------- members
    def get_members(self) -> List[Dict[str, Any]]:
        res = self._guard(self.call("getMembers", self.group_id), "getMembers")
        body = res.get("json") or {}
        data = body.get("data") or body
        return data.get("item") or data.get("user") or []

    def get_member_info(self, uins: List[str]) -> List[Dict[str, Any]]:
        if not uins:
            return []
        res = self.call("getMemberInfo", self.group_id, uins, timeout=240)
        body = res.get("json") or {}
        data = body.get("data") or body
        return data.get("user") or data.get("item") or []

    def get_server_time(self) -> Optional[int]:
        try:
            res = self.call("getServerTime", self.group_id)
        except (QzoneError, CDPError):
            return None
        body = res.get("json") or {}
        for key in ("t", "time", "svrtime"):
            value = (body.get("data") or {}).get(key) if isinstance(body.get("data"), dict) else None
            value = value if value is not None else body.get(key)
            if value:
                try:
                    value = int(value)
                    return value // 1000 if value > 10 ** 11 else value
                except (TypeError, ValueError):
                    pass
        return None

    # -------------------------------------------------------- photo comments
    def get_photo_comments(self, album_id: str, photo_id: str,
                           start: int = 0, num: int = 50) -> Dict[str, Any]:
        return self.call("getPhotoComments", self.group_id, album_id, photo_id, start, num)
