"""群相册视频：从机器人引擎（SnowLuma）取视频清单与播放地址。

── 为什么视频得走这条路 ──────────────────────────────────────────────────
相册网页接口（qun_list_photo_v2）**一个视频字段都不返回**（实测：18 个字段，
没有 videourl/videodata）。所以网页那条路只能把视频的**封面图**当普通照片存下来
（btype=1，实测两个"视频相册"里的 51 条全是封面）。真正的播放地址只有机器人
引擎的 get_group_album_media_list 会给：

    POST http://127.0.0.1:3000/get_group_album_media_list
    Authorization: Bearer <token>
    {"group_id":..,"album_id":".."}

响应里 mediaList 每项要么是 {image:{...}} 要么是 {video:{id,url,cover,..}}，
其中 video.url 是可直接下载的 .mp4。

── token 从哪来 ─────────────────────────────────────────────────────────
QQ Agent 把 SnowLuma 的访问令牌存在自己的 data/config.json 里
（snowluma.accessToken / snowluma.httpAccessToken）。我们按优先级取：
   ① 环境变量 QQALBUM_SNOWLUMA_TOKEN
   ② QQ Agent 的 /api/onebot-token?which=http（它自己会脱敏，但本机可读明文）
   ③ 直接读 data/config.json
   ④ SnowLuma 自己的 config/onebot_<uin>.json（httpServers[].accessToken）

── ⚠️ 地址会过期 ────────────────────────────────────────────────────────
video.url 带 dis_k/dis_t 签名，过期后 403。所以：
   · 入库的 video_url 只当参考；
   · **下载时必须现场重取**（见 fetch_album_videos），失败再重取一次。
"""
from __future__ import annotations

import glob
import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

DEFAULT_ENGINE_URL = "http://127.0.0.1:3000"
DEFAULT_AGENT_URL = "http://127.0.0.1:3210"
AGENT_CONFIG = r"D:\QQAgent\QQ Agent\data\config.json"
SNOWLUMA_DIR = r"D:\QQAgent\QQ Agent\resources\app\snowluma"


class VideoUnavailable(RuntimeError):
    """机器人引擎不可用（没启动 / 没登录 / 拿不到令牌）。"""


def _http_json(url: str, payload: Optional[Dict[str, Any]] = None,
               token: str = "", timeout: float = 60.0) -> Any:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def _token_from_agent(agent_url: str) -> str:
    try:
        data = _http_json(f"{agent_url}/api/onebot-token?which=http", timeout=15)
        return str((data or {}).get("token") or "")
    except Exception:
        return ""


def _token_from_agent_config(path: str = AGENT_CONFIG) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception:
        return ""
    snow = cfg.get("snowluma") or {}
    return str(snow.get("httpAccessToken") or snow.get("accessToken") or "")


def _token_from_snowluma_config(directory: str = SNOWLUMA_DIR) -> str:
    for path in sorted(glob.glob(os.path.join(directory, "config", "onebot_*.json"))):
        try:
            with open(path, encoding="utf-8") as fh:
                cfg = json.load(fh)
        except Exception:
            continue
        for server in ((cfg.get("networks") or {}).get("httpServers") or []):
            token = str(server.get("accessToken") or "")
            if token:
                return token
    return ""


def find_engine_token(engine_url: str = DEFAULT_ENGINE_URL,
                      agent_url: str = DEFAULT_AGENT_URL,
                      log: Optional[Callable[[str], None]] = None) -> Tuple[str, str]:
    """返回 (engine_url, token)；找不到时抛 VideoUnavailable。"""
    def say(msg: str) -> None:
        if log:
            log(msg)

    env = os.environ.get("QQALBUM_SNOWLUMA_TOKEN")
    if env:
        say("视频引擎令牌：来自环境变量 QQALBUM_SNOWLUMA_TOKEN")
        return engine_url, env.strip()

    token = _token_from_agent(agent_url)
    if token:
        say("视频引擎令牌：来自 QQ Agent 的 /api/onebot-token")
        return engine_url, token

    token = _token_from_agent_config()
    if token:
        say("视频引擎令牌：来自 QQ Agent 的 data/config.json")
        return engine_url, token

    token = _token_from_snowluma_config()
    if token:
        say("视频引擎令牌：来自 SnowLuma 的 config/onebot_*.json")
        return engine_url, token

    raise VideoUnavailable(
        "找不到视频引擎（SnowLuma）的访问令牌。请确认 QQ Agent 正在运行；"
        "也可以手动指定：设置环境变量 QQALBUM_SNOWLUMA_TOKEN=<令牌>，"
        "或在技能/文档里配置 QQALBUM_SNOWLUMA_URL / QQALBUM_SNOWLUMA_TOKEN。")


def _first_url(photo_urls: Iterable[Dict[str, Any]], prefer: Iterable[int] = ()) -> str:
    """从 photoUrls 里挑一个地址：优先 prefer 里的 spec，否则取第一个。"""
    items = [p for p in (photo_urls or []) if isinstance(p, dict)]
    for want in prefer:
        for item in items:
            if int(item.get("spec") or 0) == int(want):
                url = (item.get("url") or {}).get("url") or ""
                if url:
                    return str(url)
    for item in items:
        url = (item.get("url") or {}).get("url") or ""
        if url:
            return str(url)
    return ""


def pickey_from_cover(cover_url: str) -> str:
    """从封面地址里抠出照片的 pickey。

    ⚠️ 别用 video.cover.lloc：那是**另一种编码**（base64 变体），和 photos.lloc 对不上
    （实测 376 条一条都匹配不上）。而封面地址的路径里带的就是照片 pickey：

        https://qungz.photo.store.qq.com/qun-qungz/<album>/<PICKEY>!!/400?...
                                                     ^^^^^^^ 这个和 photos.lloc 完全一致
    """
    text = str(cover_url or "")
    if "/" not in text:
        return ""
    # 去掉 query，取倒数第二段（最后一段是尺寸 400/800）
    path = text.split("?")[0].rstrip("/")
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        return ""
    segment = parts[-2] if parts[-1].isdigit() else parts[-1]
    if segment.endswith("!!"):
        return segment
    return ""


def parse_media_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把 mediaList 里的一项转成入库用的字典；只有视频才返回。

    ⚠️ image 项也可能是"视频的封面"（视频项自己带 video 字段），所以这里只认 video，
    不看 type —— 实测同一批数据里视频项的 type 也是 1，用 type 判断会漏。
    """
    if not isinstance(item, dict):
        return None
    video = item.get("video")
    if not isinstance(video, dict) or not (video.get("id") or video.get("url")):
        return None
    cover = video.get("cover") if isinstance(video.get("cover"), dict) else {}
    cover_url = _first_url(cover.get("photoUrls"), prefer=(2, 3, 1)) or str(cover.get("url") or "")
    # ⚠️ 时长字段叫 videoTime，而且挂在**外层 media 项**上（毫秒）——
    # video 对象里没有 time/duration，之前按那两个名字取，结果一直是空的。
    duration = 0
    for source in (item, video):
        for key in ("videoTime", "video_time", "duration", "time"):
            value = source.get(key)
            if value:
                try:
                    duration = int(value)
                except (TypeError, ValueError):
                    duration = 0
                if duration:
                    break
        if duration:
            break
    return {
        "video_id": str(video.get("id") or "").strip(),
        # lloc 用封面地址里的 pickey，才能和 photos.lloc 对上（见 pickey_from_cover）
        "lloc": pickey_from_cover(cover_url) or str(cover.get("lloc") or item.get("lloc") or ""),
        "width": int(video.get("width") or item.get("width") or 0) or None,
        "height": int(video.get("height") or item.get("height") or 0) or None,
        "duration_ms": duration or None,
        "cover_url": cover_url,
        "video_url": str(video.get("url") or ""),
        # 发布者/时间也能从 media 项上拿到，存下来避免只依赖封面照片
        "uploader_uin": str(item.get("uploader") or ""),
        "upload_epoch": int(item.get("uploadTime") or 0) or None,
        "raw": item,
    }


class VideoClient:
    """机器人引擎（SnowLuma）的视频清单客户端。"""

    def __init__(self, group_id: str, engine_url: str = DEFAULT_ENGINE_URL,
                 agent_url: str = DEFAULT_AGENT_URL,
                 log: Optional[Callable[[str], None]] = None):
        self.group_id = str(group_id)
        self.engine_url = (os.environ.get("QQALBUM_SNOWLUMA_URL") or engine_url).rstrip("/")
        self.agent_url = (os.environ.get("QQALBUM_AGENT_URL") or agent_url).rstrip("/")
        self.log = log or (lambda _m: None)
        self.token = ""
        self._albums: List[Dict[str, Any]] = []

    # ------------------------------------------------------------- connect
    def connect(self) -> str:
        """确认引擎在跑并拿到令牌；返回一句可读的状态描述。"""
        self.engine_url, self.token = find_engine_token(self.engine_url, self.agent_url, self.log)
        try:
            self.call("get_login_info", {})
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise VideoUnavailable(
                    f"视频引擎拒绝了令牌（HTTP {exc.code}）。令牌可能在 QQ Agent 里换过，"
                    "重启 QQ Agent 或重新扫码登录后再试。") from None
            raise VideoUnavailable(f"视频引擎返回 HTTP {exc.code}") from None
        except Exception as exc:
            raise VideoUnavailable(
                f"连不上视频引擎 {self.engine_url}：{type(exc).__name__}: {exc}。"
                "视频清单来自 QQ Agent 的 SnowLuma 引擎，请确认它正在运行。") from None
        return f"{self.engine_url}（令牌 {len(self.token)} 字符）"

    def call(self, action: str, payload: Dict[str, Any], timeout: float = 120.0) -> Dict[str, Any]:
        if not self.token:
            self.engine_url, self.token = find_engine_token(self.engine_url, self.agent_url, self.log)
        body = _http_json(f"{self.engine_url}/{action.lstrip('/')}",
                          payload, token=self.token, timeout=timeout)
        if not isinstance(body, dict):
            raise VideoUnavailable(f"{action} 返回了意外内容：{type(body).__name__}")
        if str(body.get("status") or "").lower() == "failed":
            raise VideoUnavailable(
                f"{action} 失败：{body.get('wording') or body.get('message') or body.get('retcode')}")
        return body

    # -------------------------------------------------------------- albums
    def albums(self) -> List[Dict[str, Any]]:
        if self._albums:
            return self._albums
        body = self.call("get_group_album_list", {"group_id": int(self.group_id)})
        data = body.get("data")
        if isinstance(data, list):
            items = data
        else:
            items = (data or {}).get("albumList") or (data or {}).get("album_list") or []
        self._albums = [a for a in items if isinstance(a, dict)]
        return self._albums

    # --------------------------------------------------------------- media
    def album_videos(self, album_id: str, album_title: str = "",
                     max_pages: int = 60) -> List[Dict[str, Any]]:
        """取一个相册里的全部视频（自动翻页）。"""
        out: List[Dict[str, Any]] = []
        attach: Any = ""
        for _page in range(max(1, max_pages)):
            payload: Dict[str, Any] = {"group_id": int(self.group_id), "album_id": album_id}
            if attach:
                payload["attach_info"] = attach if isinstance(attach, str) else json.dumps(attach)
            body = self.call("get_group_album_media_list", payload)
            data = body.get("data") or {}
            media = data.get("mediaList") or data.get("media_list") or []
            found = 0
            for item in media:
                parsed = parse_media_item(item)
                if not parsed:
                    continue
                parsed["album_id"] = album_id
                parsed["album_title"] = album_title
                out.append(parsed)
                found += 1
            attach = data.get("nextAttachInfo") or data.get("next_attach_info") or ""
            if not attach:
                break
        return out

    def all_videos(self, max_albums: int = 0,
                   progress: Optional[Callable[[int, int, str], None]] = None
                   ) -> List[Dict[str, Any]]:
        """遍历所有相册取视频清单。

        ⚠️ 同一个视频会出现在多个相册里（动态批次是多相册共用的），实测 78 个相册会
        报出 570 条视频记录、但唯一 video_id 少得多。所以这里**按 video_id 去重**，
        保留第一次遇到的那个相册归属，否则入库会重复、下载也会重复。
        """
        albums = self.albums()
        if max_albums:
            albums = albums[:max_albums]
        seen: Dict[str, Dict[str, Any]] = {}
        for index, album in enumerate(albums, 1):
            album_id = str(album.get("id") or album.get("album_id") or "")
            title = str(album.get("name") or album.get("title") or "")
            if not album_id:
                continue
            if progress:
                progress(index, len(albums), title)
            try:
                for video in self.album_videos(album_id, title):
                    seen.setdefault(video["video_id"], video)
            except Exception as exc:
                self.log(f"《{title}》视频清单获取失败：{type(exc).__name__}: {exc}")
        return list(seen.values())

    def fresh_url(self, album_id: str, video_id: str, album_title: str = "") -> str:
        """现场重取某个视频的播放地址（签名会过期，下载前必须走这一步）。"""
        for video in self.album_videos(album_id, album_title):
            if video.get("video_id") == video_id:
                return str(video.get("video_url") or "")
        return ""
