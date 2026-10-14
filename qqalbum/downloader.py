"""Original-image downloader with resume, retries and integrity checks."""
from __future__ import annotations

import os
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence
from urllib import error as urlerror
from urllib import request as urlrequest

from .store import Store, now_iso

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 Edg/126.0")
REFERER = "https://h5.qzone.qq.com/"
_SAFE = re.compile(r"[^0-9A-Za-z._@()\-]+")


def safe_name(text: str, fallback: str = "file", limit: int = 90) -> str:
    text = _SAFE.sub("_", (text or "").strip())
    text = text.strip("._") or fallback
    return text[:limit]


def photo_url_variants(row: Any) -> List[str]:
    """Candidate download URLs, best quality first.

    Some QQ photos have no stored full-size original: the CDN then answers with a
    few-hundred-byte webp placeholder. Falling back to the 800/200 renderings keeps
    a usable image instead of losing the photo entirely.
    """
    urls: List[str] = []
    for key in ("raw_url", "big_url", "thumb_url"):
        url = row[key] if key in row.keys() else None
        if not url:
            continue
        url = str(url)
        urls.append(url)
        base = url.split("?")[0]
        for size in ("/0", "/800", "/200"):
            if base.endswith(size):
                for fallback in ("/800", "/200"):
                    candidate = base[: -len(size)] + fallback
                    if candidate != url:
                        urls.append(candidate)
                break
    seen: set = set()
    out: List[str] = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


class PhotoDownloader:
    def __init__(self, store: Store, root_dir: str, workers: int = 4,
                 log: Optional[Callable[[str], None]] = None, timeout: int = 60,
                 retries: int = 3, min_bytes: int = 1024):
        self.store = store
        self.root_dir = root_dir
        self.workers = max(1, int(workers))
        self.log = log or (lambda msg: None)
        self.timeout = timeout
        self.retries = retries
        self.min_bytes = min_bytes
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ paths
    def target_path(self, row: Any) -> str:
        album = safe_name(str(row["album_id"] or "album"), "album", 60)
        uploader = safe_name(str(row["uploader_uin"] or "unknown"), "unknown", 32)
        photo_id = safe_name(str(row["photo_id"] or "photo"), "photo", 80)
        ext = ".gif" if self._is_animated(row) else ".jpg"
        return os.path.join(self.root_dir, "photos", safe_name(self.group_of(row), "group", 32),
                            album, uploader, f"{photo_id}{ext}")

    @staticmethod
    def _is_animated(row: Any) -> bool:
        try:
            return int(row["is_animated"] or 0) == 1
        except (KeyError, IndexError, TypeError, ValueError):
            return False

    @staticmethod
    def group_of(row: Any) -> str:
        try:
            return str(row["group_id"] or "")
        except Exception:
            return ""

    # --------------------------------------------------------------- download
    @staticmethod
    def _stub_rank(data: bytes, stub_max: int = 64) -> Optional[int]:
        """Return the pixel size when `data` is a tiny placeholder, else None.

        QQ sometimes keeps only a 16x16/32x32 stub for a photo. Judging by pixel size
        (not byte size) avoids misclassifying legitimate small images.
        """
        if len(data) > 200_000 or len(data) < 24:
            return None
        try:
            import io as _io
            from PIL import Image
            with Image.open(_io.BytesIO(data)) as img:
                width, height = img.size
            if width <= stub_max and height <= stub_max:
                return max(width, height)
        except Exception:
            return None
        return None

    def download_one(self, row: Any, group_id: str) -> Dict[str, Any]:
        variants = photo_url_variants(row)
        url = variants[0] if variants else ""
        photo_id = str(row["photo_id"])
        album_id = str(row["album_id"] or "")
        result = {"photo_id": photo_id, "ok": False, "path": None, "size": 0, "error": "", "url": url}
        if not url:
            result["error"] = "no url"
            self.store.set_photo_file(group_id, photo_id, "error", error="缺少图片地址")
            return result

        path = self.target_path(row)
        if os.path.exists(path) and os.path.getsize(path) > self.min_bytes:
            size = os.path.getsize(path)
            self.store.set_photo_file(group_id, photo_id, "done", path=path, size=size)
            result.update(ok=True, path=path, size=size, skipped=True)
            return result

        last_error = ""
        part = path + ".part"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # remember the largest small payload seen: some QQ photos are 16x16/32x32 stubs
        tiny: Optional[bytes] = None
        tiny_rank = -1
        for candidate in variants:
            for attempt in range(1, self.retries + 1):
                try:
                    req = urlrequest.Request(candidate, headers={
                        "User-Agent": USER_AGENT,
                        "Referer": REFERER,
                        "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
                    })
                    with urlrequest.urlopen(req, timeout=self.timeout) as resp:
                        ctype = (resp.headers.get("Content-Type") or "").lower()
                        if "image" not in ctype and "octet-stream" not in ctype:
                            raise ValueError(f"unexpected content-type {ctype!r}")
                        data = resp.read()
                    rank = self._stub_rank(data)
                    if rank is not None:
                        # a stub/placeholder: this size has no usable image on the CDN
                        if rank > tiny_rank:
                            tiny, tiny_rank = data, rank
                        last_error = f"CDN 无原始图（仅 {rank}×{rank} 占位图，{len(data)} 字节）"
                        break
                    tmp = part + f".{threading.get_ident()}"
                    with open(tmp, "wb") as fh:
                        fh.write(data)
                    os.replace(tmp, path)
                    self.store.set_photo_file(group_id, photo_id, "done", path=path, size=len(data))
                    self.store.log_download(group_id, photo_id, album_id, candidate, "ok", len(data))
                    result.update(ok=True, path=path, size=len(data), url=candidate)
                    return result
                except (urlerror.URLError, urlerror.HTTPError, ValueError, OSError, TimeoutError) as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    if attempt < self.retries:
                        time.sleep(min(2 ** attempt * 0.4, 4.0))
        self._cleanup_parts(part)
        if tiny is not None:
            # keep the placeholder so the grid still shows something and later runs skip it
            try:
                with open(path, "wb") as fh:
                    fh.write(tiny)
                self.store.set_photo_file(group_id, photo_id, "thumb-only", path=path, size=len(tiny),
                                          error=last_error[:400])
                self.store.log_download(group_id, photo_id, album_id, url, "thumb-only", len(tiny),
                                        last_error[:400])
                result.update(ok=True, path=path, size=len(tiny), status="thumb-only",
                              error=last_error)
                return result
            except OSError:
                pass
        self.store.set_photo_file(group_id, photo_id, "error", error=last_error[:400])
        self.store.log_download(group_id, photo_id, album_id, url, "error", 0, last_error[:400])
        result["error"] = last_error
        return result

    @staticmethod
    def _cleanup_parts(part_prefix: str) -> None:
        """Remove `<file>.part` / `<file>.part.<tid>` temp files left by earlier attempts."""
        directory = os.path.dirname(part_prefix) or "."
        base = os.path.basename(part_prefix)
        try:
            names = os.listdir(directory)
        except OSError:
            return
        for name in names:
            if name == base or name.startswith(base + "."):
                try:
                    os.remove(os.path.join(directory, name))
                except OSError:
                    pass

    def sweep_orphan_parts(self, log: Optional[Callable[[str], None]] = None) -> int:
        """Delete stale `*.part*` temp files under the photo directory."""
        root = os.path.join(self.root_dir, "photos")
        removed = 0
        for dirpath, _dirs, names in os.walk(root):
            for name in names:
                if ".part" in name:
                    try:
                        os.remove(os.path.join(dirpath, name))
                        removed += 1
                    except OSError:
                        pass
        if removed and log:
            log(f"清理了 {removed} 个残留的临时下载文件")
        return removed

    # -------------------------------------------------------------------- run
    def run(self, rows: Sequence[Any], group_id: str,
            should_stop: Optional[Callable[[], bool]] = None,
            progress: Optional[Callable[[int, int], None]] = None) -> int:
        self.sweep_orphan_parts(self.log)
        total = len(rows)
        done = 0
        if not total:
            return 0
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(self.download_one, row, group_id): row for row in rows}
            for future in as_completed(futures):
                if should_stop and should_stop():
                    for pending in futures:
                        pending.cancel()
                    break
                try:
                    res = future.result()
                except Exception as exc:  # pragma: no cover - defensive
                    self.log(f"下载异常：{type(exc).__name__}: {exc}")
                    continue
                if res.get("ok"):
                    done += 1
                if progress:
                    progress(done, total)
                if done and done % 25 == 0:
                    self.log(f"已下载 {done}/{total}")
        return done


# ---------------------------------------------------------------------- video
class VideoDownloader:
    """群相册视频下载器。

    和图片下载器的三点不同：
      ① 视频地址**带签名会过期**（dis_k/dis_t），所以每一步都可能需要现场重取
         （`refresh` 回调 → VideoClient.fresh_url）；403/401 时自动换一条再试。
      ② 文件大，必须支持**断点续传**：服务器认 Range（实测 206），所以先下到 .part、
         中断后带 Range 续传，全部收完再改名。
      ③ 校验方式是 MP4 容器头（`ftyp`），不是图片解码。
    """

    def __init__(self, store: Store, root_dir: str, workers: int = 2,
                 log: Optional[Callable[[str], None]] = None, timeout: int = 180,
                 retries: int = 3, min_bytes: int = 1024,
                 refresh: Optional[Callable[[Any], str]] = None):
        self.store = store
        self.root_dir = root_dir
        self.workers = max(1, int(workers))
        self.log = log or (lambda msg: None)
        self.timeout = timeout
        self.retries = retries
        self.min_bytes = min_bytes
        # refresh(row) -> 新的播放地址（拿不到就返回空串）
        self.refresh = refresh

    # ------------------------------------------------------------------ paths
    def target_path(self, row: Any) -> str:
        group = safe_name(str(row["group_id"] or "group"), "group", 32)
        album = safe_name(str(row["album_id"] or "album"), "album", 60)
        video_id = safe_name(str(row["video_id"] or "video"), "video", 90)
        return os.path.join(self.root_dir, "videos", group, album, f"{video_id}.mp4")

    @staticmethod
    def _looks_like_html(data: bytes) -> bool:
        head = data[:300].lstrip().lower()
        return head.startswith(b"<!doctype html") or head.startswith(b"<html")

    @staticmethod
    def _is_mp4(data: bytes) -> bool:
        """MP4/MOV 容器里一定有 'ftyp' box（通常在第 4~12 字节）。"""
        if len(data) < 12:
            return False
        return b"ftyp" in data[:64]

    # --------------------------------------------------------------- download
    def download_one(self, row: Any, group_id: str) -> Dict[str, Any]:
        video_id = str(row["video_id"])
        album_id = str(row["album_id"] or "")
        url = str(row["video_url"] or "")
        result = {"video_id": video_id, "ok": False, "path": None, "size": 0, "error": "", "url": url}
        if not url and self.refresh:
            url = self.refresh(row) or ""
        if not url:
            self.store.set_video_file(group_id, video_id, "failed", error="缺少播放地址")
            result["error"] = "no url"
            return result

        path = self.target_path(row)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > self.min_bytes:
            size = os.path.getsize(path)
            self.store.set_video_file(group_id, video_id, "done", path=path, size=size)
            result.update(ok=True, path=path, size=size, skipped=True)
            return result

        part = path + ".part"
        last_error = ""
        refreshed = False
        attempt = 0
        while attempt < self.retries:
            attempt += 1
            have = os.path.getsize(part) if os.path.exists(part) else 0
            headers = {
                "User-Agent": USER_AGENT,
                "Referer": REFERER,
                "Accept": "video/*,*/*;q=0.8",
            }
            if have:
                headers["Range"] = f"bytes={have}-"
            try:
                req = urlrequest.Request(url, headers=headers)
                with urlrequest.urlopen(req, timeout=self.timeout) as resp:
                    status = getattr(resp, "status", 200)
                    ctype = (resp.headers.get("Content-Type") or "").lower()
                    if status == 200 and have:
                        # 服务器忽略了 Range：从头重下，别把两段拼在一起
                        self.log(f"{video_id[:18]}… 服务器不支持续传，从头下载")
                        have = 0
                    if "html" in ctype:
                        raise ValueError(f"拿到的是网页而不是视频（{ctype}）")
                    with open(part, "ab" if have else "wb") as fh:
                        while True:
                            chunk = resp.read(256 * 1024)
                            if not chunk:
                                break
                            fh.write(chunk)
                size = os.path.getsize(part)
                if size <= self.min_bytes:
                    raise ValueError(f"文件太小（{size} 字节）")
                with open(part, "rb") as fh:
                    head = fh.read(64)
                if not self._is_mp4(head):
                    raise ValueError(f"不是 MP4 容器（{head[:12]!r}）")
                os.replace(part, path)
                self.store.set_video_file(group_id, video_id, "done", path=path, size=size)
                self.store.set_video_url(group_id, video_id, url)
                self.store.log_download(group_id, video_id, album_id, url, "ok", size)
                result.update(ok=True, path=path, size=size, url=url)
                return result
            except (urlerror.URLError, urlerror.HTTPError, ValueError, OSError, TimeoutError) as exc:
                code = getattr(exc, "code", None)
                last_error = f"{type(exc).__name__}: {exc}"
                expired = code in (401, 403) or "403" in str(exc) or "401" in str(exc)
                # 地址过期 → 现场换一条（只换一次，避免把限流当成过期反复打接口）
                if expired and self.refresh and not refreshed:
                    refreshed = True
                    self.log(f"{video_id[:18]}… 地址已过期，重新获取…")
                    new_url = self.refresh(row) or ""
                    if new_url and new_url != url:
                        url = new_url
                        attempt -= 1  # 这次不算失败重试
                        if os.path.exists(part):
                            try:
                                os.remove(part)   # 新地址的 Range 偏移未必对得上，重下
                            except OSError:
                                pass
                        continue
                    last_error = f"{last_error}（重新获取地址也没成功）"
                if attempt < self.retries:
                    time.sleep(min(2 ** attempt * 0.5, 5.0))
        try:
            if os.path.exists(part):
                os.remove(part)
        except OSError:
            pass
        self.store.set_video_file(group_id, video_id, "failed", error=last_error[:400])
        self.store.log_download(group_id, video_id, album_id, url, "error", 0, last_error[:400])
        result["error"] = last_error
        return result

    def sweep_orphan_parts(self, log: Optional[Callable[[str], None]] = None) -> int:
        root = os.path.join(self.root_dir, "videos")
        removed = 0
        for dirpath, _dirs, names in os.walk(root):
            for name in names:
                if ".part" in name:
                    try:
                        os.remove(os.path.join(dirpath, name))
                        removed += 1
                    except OSError:
                        pass
        if removed and log:
            log(f"清理了 {removed} 个残留的视频临时文件")
        return removed

    def run(self, rows: Sequence[Any], group_id: str,
            should_stop: Optional[Callable[[], bool]] = None,
            progress: Optional[Callable[[int, int], None]] = None) -> int:
        total = len(rows)
        if not total:
            return 0
        done = 0
        failed = 0
        # 视频并发压低：单文件大，而且引擎接口有限流
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(self.download_one, row, group_id): row for row in rows}
            for future in as_completed(futures):
                if should_stop and should_stop():
                    for pending in futures:
                        pending.cancel()
                    break
                try:
                    res = future.result()
                except Exception as exc:  # pragma: no cover - defensive
                    self.log(f"视频下载异常：{type(exc).__name__}: {exc}")
                    continue
                if res.get("ok"):
                    done += 1
                else:
                    failed += 1
                if progress:
                    progress(done + failed, total)
                if (done + failed) % 5 == 0:
                    self.log(f"视频：成功 {done} 失败 {failed} / 共 {total}")
        return done

