"""AI indexing (SigLIP2) and semantic search over the backup.

Two jobs live here:

* :class:`AiIndexer` walks photos that still lack an embedding, encodes them with
  SigLIP2 on CPU and stores a 768-d vector plus the top tags for each photo.
* :class:`AiSearch` keeps the vector matrix in memory and answers text queries,
  image-to-image queries and id-based similarity, all of which can be combined with
  the normal metadata filters (album / uploader / date / AI tag).
"""
from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .caption import (CAPTION_LABEL, CAPTION_MODEL_TAG, CaptionEngine, CaptionUnavailable,
                      expand_query, has_cjk)
from .siglip import (EMBED_MODEL_TAG, MODEL_LABEL, VOCAB, SiglipEngine, SiglipUnavailable,
                     VECTOR_DIM, build_matrix, tag_color, vector_to_blob)
from .store import Store, now_iso


class AiState:
    """Progress shared with the WebUI."""

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
        self.rate = 0.0
        self.log: List[str] = []

    def update(self, **kwargs: Any) -> None:
        with self.lock:
            for key, value in kwargs.items():
                setattr(self, key, value)

    def add_log(self, text: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {text}"
        with self.lock:
            self.log.append(line)
            self.log = self.log[-300:]
            self.message = text

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            elapsed = (self.finished_at or time.time()) - self.started_at if self.started_at else 0
            percent = (self.current / self.total * 100.0) if self.total else 0.0
            eta = 0.0
            if self.rate > 0 and self.total > self.current:
                eta = (self.total - self.current) / self.rate
            return {
                "running": self.running, "stage": self.stage, "message": self.message,
                "error": self.error, "current": self.current, "total": self.total,
                "percent": round(percent, 2), "elapsed": round(elapsed, 1),
                "rate": round(self.rate, 2), "eta": round(eta, 1), "log": list(self.log[-60:]),
            }


class AiIndexer:
    """Background SigLIP2 indexing of local photo files."""

    def __init__(self, store: Store, group_id: str, engine: Optional[SiglipEngine] = None,
                 model_dir: Optional[str] = None, batch_size: int = 16, loader_workers: int = 8,
                 log: Optional[Callable[[str], None]] = None):
        self.store = store
        self.group_id = str(group_id)
        self.engine = engine
        self.model_dir = model_dir
        self.batch_size = max(1, int(batch_size))
        self.loader_workers = max(1, int(loader_workers))
        self.state = AiState()
        self._thread: Optional[threading.Thread] = None
        self._log = log

    # ------------------------------------------------------------- lifecycle
    def is_running(self) -> bool:
        return bool(self.state.running)

    def cancel(self) -> None:
        self.state.cancel = True
        self.state.add_log("已请求停止，将在当前批次后结束…")

    def engine_or_raise(self) -> SiglipEngine:
        if self.engine is None:
            self.engine = SiglipEngine.instance(self.model_dir,
                                                log=lambda m: self.state.add_log(m))
        ok, reason = self.engine.available()
        if not ok:
            raise SiglipUnavailable(reason)
        return self.engine

    def start(self, limit: int = 0, retag: bool = False, album_id: Optional[str] = None) -> bool:
        if self.state.running:
            return False
        self._reset()
        self._thread = threading.Thread(target=self._worker, name="qqalbum-ai",
                                        args=(limit, retag, album_id), daemon=True)
        self._thread.start()
        return True

    def _reset(self) -> None:
        self.state.update(running=True, stage="prepare", error=None, current=0, total=0,
                          started_at=time.time(), finished_at=None, cancel=False, rate=0.0)
        self.state.log = []
        self.state.add_log("准备 AI 打标与向量索引…")

    # ---------------------------------------------------------------- worker
    def _worker(self, limit: int, retag: bool, album_id: Optional[str]) -> None:
        state = self.state
        try:
            engine = self.engine_or_raise()
            engine.load()
            stats = self.store.ai_stats(self.group_id)
            state.add_log(f"当前已索引 {stats['embedded']} 张，已打标 {stats['tagged_photos']} 张")

            if retag:
                state.add_log(f"重新打标：已清除 {self.store.clear_ai_tags(self.group_id)} 条旧标签")

            rows = self.store.photos_with_local_file(self.group_id, album_id=album_id, limit=0)
            done_ids = {r["photo_id"] for r in self.store.query(
                "SELECT photo_id FROM photo_embedding WHERE group_id=? AND model=?",
                (self.group_id, EMBED_MODEL_TAG))}
            todo = [r for r in rows if retag or r["photo_id"] not in done_ids]
            if limit:
                todo = todo[:limit]
            state.total = len(todo)
            state.update(stage="index")
            if not todo:
                state.add_log("所有本地图片都已建立向量索引")
                state.update(running=False, stage="done", finished_at=time.time())
                return

            # make sure the vocabulary has tag ids before we start writing
            names = [name for name, _ in VOCAB]
            for name, prompt in VOCAB:
                self.store.ensure_model_tag(name, prompt, tag_color(name), "vocab")
            tag_ids = self.store.model_tag_ids(names)
            unknown = [n for n in names if n not in tag_ids]
            if unknown:
                raise RuntimeError(f"标签词表写入失败：{unknown[:5]}")

            state.add_log(f"待处理 {len(todo)} 张图片（batch={self.batch_size}）")
            started = time.time()
            processed = 0
            failed = 0

            def load(row) -> Optional[Any]:
                try:
                    return engine.load_image(row["file_path"])
                except Exception:
                    return None

            with ThreadPoolExecutor(max_workers=self.loader_workers) as pool:
                for start in range(0, len(todo), self.batch_size):
                    if state.cancel:
                        state.add_log("已按请求停止")
                        break
                    chunk = todo[start:start + self.batch_size]
                    images = list(pool.map(load, chunk))
                    valid = [(row, img) for row, img in zip(chunk, images) if img is not None]
                    if not valid:
                        continue
                    feats = engine.encode_images([img for _row, img in valid],
                                                 batch_size=self.batch_size)
                    blob_rows: List[Tuple[str, bytes]] = []
                    for offset, (row, _img) in enumerate(valid):
                        photo_id = row["photo_id"]
                        vector = feats[offset]
                        blob_rows.append((photo_id, vector_to_blob(vector)))
                        # keep the strongest tags; absolute SigLIP probabilities are
                        # uncalibrated, so a fixed top-k plus a floor on the per-photo
                        # relative confidence is the meaningful cut
                        tags = engine.tag_image_features(vector, top_k=12, min_probability=0.0)
                        entries = [{
                            "tag_id": tag_ids[t["name"]],
                            "score": t["score"],
                            "confidence": t["confidence"],
                            "rank": t["rank"],
                        } for t in tags
                            if t["name"] in tag_ids and (t["rank"] <= 3 or t["confidence"] >= 0.05)]
                        self.store.clear_ai_tags(self.group_id, [photo_id])
                        self.store.upsert_ai_tags(self.group_id, photo_id, entries,
                                                  model=EMBED_MODEL_TAG, commit=False)
                    self.store.upsert_embeddings(self.group_id, blob_rows, EMBED_MODEL_TAG,
                                                 VECTOR_DIM, commit=False)
                    self.store.commit()
                    processed += len(valid)
                    failed += len(chunk) - len(valid)
                    elapsed = max(1e-6, time.time() - started)
                    state.update(current=processed, rate=processed / elapsed)
                    if processed % (self.batch_size * 5) < self.batch_size:
                        state.add_log(f"已索引 {processed}/{len(todo)}"
                                      f"（{processed / elapsed:.1f} 张/秒）")
            state.add_log(f"索引完成：{processed} 张成功，{failed} 张读取失败")
            stats = self.store.ai_stats(self.group_id)
            state.add_log(f"累计向量 {stats['embedded']} 条，标签 {stats['ai_tag_rows']} 条，"
                          f"覆盖 {stats['tags_used']} 个标签")
            state.update(running=False, stage="done", finished_at=time.time())
        except SiglipUnavailable as exc:
            state.update(running=False, stage="error", error=str(exc), finished_at=time.time())
            state.add_log(f"无法启动：{exc}")
        except Exception as exc:
            state.update(running=False, stage="error",
                         error=f"{type(exc).__name__}: {exc}", finished_at=time.time())
            state.add_log(f"错误：{type(exc).__name__}: {exc}")


class CaptionIndexer:
    """Background BLIP captioning of local photo files (batched, resumable)."""

    def __init__(self, store: Store, group_id: str, engine: Optional[CaptionEngine] = None,
                 model_dir: Optional[str] = None, batch_size: int = 12, loader_workers: int = 8):
        self.store = store
        self.group_id = str(group_id)
        self.engine = engine
        self.model_dir = model_dir
        self.batch_size = max(1, int(batch_size))
        self.loader_workers = max(1, int(loader_workers))
        self.state = AiState()
        self._thread: Optional[threading.Thread] = None

    def is_running(self) -> bool:
        return bool(self.state.running)

    def cancel(self) -> None:
        self.state.cancel = True
        self.state.add_log("已请求停止，将在当前批次后结束…")

    def engine_or_raise(self) -> CaptionEngine:
        if self.engine is None:
            self.engine = CaptionEngine.instance(self.model_dir,
                                                 log=lambda m: self.state.add_log(m))
        ok, reason = self.engine.available()
        if not ok:
            raise CaptionUnavailable(reason)
        return self.engine

    def start(self, limit: int = 0, recaption: bool = False,
              album_id: Optional[str] = None) -> bool:
        if self.state.running:
            return False
        self._reset()
        self._thread = threading.Thread(target=self._worker, name="qqalbum-caption",
                                        args=(limit, recaption, album_id), daemon=True)
        self._thread.start()
        return True

    def _reset(self) -> None:
        self.state.update(running=True, stage="prepare", error=None, current=0, total=0,
                          started_at=time.time(), finished_at=None, cancel=False, rate=0.0)
        self.state.log = []
        self.state.add_log("准备生成图片描述（BLIP）…")

    def _worker(self, limit: int, recaption: bool, album_id: Optional[str]) -> None:
        state = self.state
        try:
            engine = self.engine_or_raise()
            engine.load()
            if recaption:
                state.add_log(f"重新描述：已清除 {self.store.clear_captions(self.group_id)} 条旧描述")

            rows = self.store.photos_with_local_file(self.group_id, album_id=album_id, limit=0)
            done = {r["photo_id"] for r in self.store.query(
                "SELECT photo_id FROM photo_caption WHERE group_id=? AND model=?",
                (self.group_id, CAPTION_MODEL_TAG))}
            todo = [r for r in rows if recaption or r["photo_id"] not in done]
            if limit:
                todo = todo[:limit]
            state.total = len(todo)
            state.update(stage="caption")
            if not todo:
                state.add_log("所有本地图片都已有描述")
                state.update(running=False, stage="done", finished_at=time.time())
                return
            state.add_log(f"待生成 {len(todo)} 张描述（batch={self.batch_size}）")

            def load(row):
                try:
                    return engine.load_image(row["file_path"])
                except Exception:
                    return None

            started = time.time()
            processed = 0
            failed = 0
            with ThreadPoolExecutor(max_workers=self.loader_workers) as pool:
                for start in range(0, len(todo), self.batch_size):
                    if state.cancel:
                        state.add_log("已按请求停止")
                        break
                    chunk = todo[start:start + self.batch_size]
                    images = list(pool.map(load, chunk))
                    valid = [(row, img) for row, img in zip(chunk, images) if img is not None]
                    failed += len(chunk) - len(valid)
                    if not valid:
                        continue
                    captions = engine.caption_images([img for _row, img in valid])
                    items = []
                    for (row, _img), caption in zip(valid, captions):
                        # store the English caption only: CJK text tokenises to single
                        # characters in FTS5 and would just add noise
                        items.append((row["photo_id"], caption, caption))
                    self.store.upsert_captions(self.group_id, items, model=CAPTION_MODEL_TAG,
                                               source="blip", commit=False)
                    self.store.commit()
                    processed += len(valid)
                    elapsed = max(1e-6, time.time() - started)
                    state.update(current=processed, rate=processed / elapsed)
                    if processed % (self.batch_size * 10) < self.batch_size:
                        state.add_log(f"已生成 {processed}/{len(todo)}"
                                      f"（{processed / elapsed:.1f} 张/秒）")
            state.add_log(f"描述完成：{processed} 张成功，{failed} 张读取失败")
            state.add_log(f"累计描述 {self.store.caption_count(self.group_id)} 条")
            state.update(running=False, stage="done", finished_at=time.time())
        except CaptionUnavailable as exc:
            state.update(running=False, stage="error", error=str(exc), finished_at=time.time())
            state.add_log(f"无法启动：{exc}")
        except Exception as exc:
            state.update(running=False, stage="error",
                         error=f"{type(exc).__name__}: {exc}", finished_at=time.time())
            state.add_log(f"错误：{type(exc).__name__}: {exc}")


class AiSearch:
    """In-memory vector index for text / image / id similarity search."""

    def __init__(self, store: Store, group_id: str, engine: Optional[SiglipEngine] = None,
                 model_dir: Optional[str] = None):
        self.store = store
        self.group_id = str(group_id)
        self.engine = engine
        self.model_dir = model_dir
        self._lock = threading.Lock()
        self._ids: List[str] = []
        self._matrix = None
        self._meta: Dict[str, Dict[str, Any]] = {}
        self._stamp: Optional[str] = None
        self._captions: Dict[str, str] = {}
        self._last_check = 0.0
        self._check_interval = float(os.environ.get("QQALBUM_SEARCH_REFRESH_SECONDS") or 20.0)

    # ----------------------------------------------------------------- cache
    def _engine(self) -> SiglipEngine:
        if self.engine is None:
            self.engine = SiglipEngine.instance(self.model_dir)
        return self.engine

    def _signature(self) -> str:
        row = self.store.query_one(
            "SELECT COUNT(*) c, MAX(created_at) t FROM photo_embedding WHERE group_id=?",
            (self.group_id,))
        return f"{row['c']}:{row['t']}"

    def refresh(self, force: bool = False) -> None:
        """(Re)load the vector matrix, but re-check the database at most every N seconds.

        While a background index job is running the table changes constantly; without
        the interval each query would rebuild the whole matrix.
        """
        with self._lock:
            now = time.time()
            if (not force and self._matrix is not None
                    and now - self._last_check < self._check_interval):
                return
            self._last_check = now
            signature = self._signature()
            if not force and signature == self._stamp and self._matrix is not None:
                return
            ids, blobs = self.store.all_embeddings(self.group_id)
            self._ids = ids
            self._matrix = build_matrix(blobs)
            metas = {r["photo_id"]: dict(r) for r in self.store.query(
                "SELECT photo_id, album_id, uploader_uin, uploader_nick, upload_epoch, description, "
                "name, comment_count, like_count, file_status "
                "FROM photos WHERE group_id=?", (self.group_id,))}
            self._meta = metas
            self._stamp = signature
            self._captions = {r["photo_id"]: r["caption"] for r in self.store.query(
                "SELECT photo_id, caption FROM photo_caption WHERE group_id=?", (self.group_id,))}

    @property
    def size(self) -> int:
        self.refresh()
        return len(self._ids)

    def usable(self) -> Tuple[bool, str]:
        ok, reason = self._engine().available()
        if not ok:
            return False, reason
        self.refresh()
        if not self._ids:
            return False, "还没有建立向量索引，请先在「AI 打标」里运行一次索引"
        return True, ""

    # ---------------------------------------------------------------- search
    def _candidate_mask(self, filters: Optional[Dict[str, Any]]) -> Optional[Any]:
        """Return a boolean numpy mask over self._ids for the metadata filters."""
        filters = filters or {}
        album = (filters.get("album_id") or "").strip()
        uploader = (filters.get("uploader") or "").strip()
        date_from = filters.get("date_from")
        date_to = filters.get("date_to")
        if not any((album, uploader, date_from, date_to)):
            return None
        import numpy as np  # noqa: PLC0415
        from .store import parse_date_arg  # noqa: PLC0415
        date_from = parse_date_arg(filters.get("date_from"))
        date_to = parse_date_arg(filters.get("date_to"), end_of_day=True)
        mask = np.zeros(len(self._ids), dtype=bool)
        for i, pid in enumerate(self._ids):
            meta = self._meta.get(pid)
            if not meta:
                continue
            if album and meta.get("album_id") != album:
                continue
            if uploader and str(meta.get("uploader_uin") or "") != uploader:
                continue
            epoch = meta.get("upload_epoch") or 0
            if date_from or date_to:
                # a photo with no timestamp cannot satisfy a date range: exclude it
                if not epoch:
                    continue
                if date_from and epoch < date_from:
                    continue
                if date_to and epoch > date_to:
                    continue
            mask[i] = True
        return mask

    def search_by_vector(self, vector, top_k: int = 60,
                         filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        self.refresh()
        if not self._ids:
            return []
        import numpy as np  # noqa: PLC0415
        query = np.asarray(vector, dtype="<f4").reshape(-1)
        norm = float(np.linalg.norm(query)) or 1.0
        query = query / norm
        scores = self._matrix @ query
        mask = self._candidate_mask(filters)
        if mask is not None:
            scores = np.where(mask, scores, -np.inf)
        k = min(max(1, int(top_k)), len(scores))
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]
        out = []
        for index in top.tolist():
            score = float(scores[index])
            if score == float("-inf"):
                continue
            photo_id = self._ids[index]
            meta = dict(self._meta.get(photo_id) or {})
            meta["photo_id"] = photo_id
            meta["score"] = score
            out.append(meta)
        return out

    def search_text(self, text: str, top_k: int = 60,
                    filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        engine = self._engine()
        vector = engine.encode_text(text).detach().cpu().numpy()
        results = self.search_by_vector(vector, top_k=top_k, filters=filters)
        for item in results:
            item["match"] = "text"
            item["query"] = text
        return results

    def search_image(self, image, top_k: int = 60,
                     filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        engine = self._engine()
        vector = engine.encode_image(image).detach().cpu().numpy()
        results = self.search_by_vector(vector, top_k=top_k, filters=filters)
        for item in results:
            item["match"] = "image"
        return results

    # ------------------------------------------------------------ captions
    def search_captions(self, text: str, top_k: int = 60,
                        filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Keyword search over BLIP captions (FTS5), with zh->en query help.

        Returns [] when nothing matches so callers can fall back to vector search.
        """
        self.refresh()
        query = (text or "").strip()
        if not query:
            return []
        # Chinese queries: translate locally first (good quality), then fall back to the
        # built-in keyword mapping, then to the raw text
        english = ""
        if has_cjk(query):
            try:
                english = self._engine().translate_to_english(query) or ""
            except Exception:
                english = ""
        attempts: List[str] = []
        if english:
            attempts.append(english)
        expanded = expand_query(query)
        if expanded and expanded != query:
            attempts.append(expanded)
        attempts.append(query)
        hits: List[Tuple[str, float]] = []
        for attempt in attempts:
            hits = self.store.search_captions(self.group_id, attempt, limit=top_k * 2)
            if hits:
                break

        mask = self._candidate_mask(filters)
        index_of = {pid: i for i, pid in enumerate(self._ids)}
        out: List[Dict[str, Any]] = []
        for photo_id, relevance in hits:
            position = index_of.get(photo_id)
            if mask is not None and position is not None and not bool(mask[position]):
                continue
            meta = dict(self._meta.get(photo_id) or {})
            if not meta:
                continue
            meta["photo_id"] = photo_id
            meta["score"] = float(relevance)
            meta["match"] = "caption"
            meta["caption"] = self._captions.get(photo_id, "")
            out.append(meta)
            if len(out) >= top_k:
                break
        return out

    def search_combined(self, text: str, top_k: int = 60,
                        filters: Optional[Dict[str, Any]] = None,
                        caption_weight: float = 0.5) -> Dict[str, List[Dict[str, Any]]]:
        """Semantic (vector) and keyword (caption) results, merged for the UI.

        Each list keeps its own native score so the two engines stay comparable
        within themselves; the merged list uses a normalised blend.

        ⚠️ 为什么 caption 分数要乘上「语义可信度」：caption 通道是**关键词命中**，
        命中就给满分（1.0），而它的满分与矢量通道的第一名只差权重 ——
        于是一张与查询**毫无视觉关系**、只是描述里撞了词的图（例如查「ktv 包厢 唱歌」
        时一张 "a green bag …" 的产品图）会和语义第一名并列，甚至排到第二。
        所以这里把 caption 的贡献按 `0.25 + 0.75 * 语义分` 折算：
        语义也像 → 几乎不折；语义完全不像 → 只算四分之一权重，掉到后面去。
        同时用 (blend, 语义分, caption 分) 三级排序保证同分时语义强者在前。
        """
        semantic = self.search_text(text, top_k=top_k, filters=filters) if text.strip() else []
        captions = self.search_captions(text, top_k=top_k, filters=filters)

        merged: Dict[str, Dict[str, Any]] = {}
        if semantic:
            top = max((s["score"] for s in semantic), default=1.0) or 1.0
            for item in semantic:
                entry = merged.setdefault(item["photo_id"], dict(item))
                entry["vector_score"] = item["score"] / top
                entry["caption_score"] = 0.0
        if captions:
            top = max((c["score"] for c in captions), default=1.0) or 1.0
            for item in captions:
                entry = merged.setdefault(item["photo_id"], dict(item))
                entry.setdefault("vector_score", 0.0)
                entry["caption_score"] = item["score"] / top
                entry["caption"] = item.get("caption") or self._captions.get(item["photo_id"], "")
        for entry in merged.values():
            vector_score = entry.get("vector_score", 0.0)
            caption_score = entry.get("caption_score", 0.0)
            # 语义不像的图，光靠描述撞词不该排在前面
            trust = 0.25 + 0.75 * max(0.0, min(1.0, vector_score))
            entry["blend"] = ((1 - caption_weight) * vector_score
                              + caption_weight * caption_score * trust)
        ordered = sorted(
            merged.values(),
            key=lambda x: (-x["blend"], -x.get("vector_score", 0.0),
                           -x.get("caption_score", 0.0), x["photo_id"]))[:top_k]
        for entry in ordered:
            entry["score"] = entry["blend"]
        return {"semantic": semantic, "captions": captions, "merged": ordered}

    def search_similar_to(self, photo_id: str, top_k: int = 60,
                          filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        self.refresh()
        try:
            index = self._ids.index(photo_id)
        except ValueError:
            row = self.store.query_one(
                "SELECT vector FROM photo_embedding WHERE group_id=? AND photo_id=?",
                (self.group_id, photo_id))
            if not row:
                return []
            import numpy as np  # noqa: PLC0415
            from .siglip import blob_to_vector  # noqa: PLC0415
            vector = blob_to_vector(bytes(row["vector"]))
        else:
            vector = self._matrix[index]
        results = self.search_by_vector(vector, top_k=top_k + 1, filters=filters)
        return [r for r in results if r["photo_id"] != photo_id][:top_k]

    # ------------------------------------------------------------ tag search
    def photos_by_ai_tag(self, tag_id: int, min_confidence: float = 0.0,
                         limit: int = 2000) -> List[str]:
        rows = self.store.query(
            "SELECT photo_id FROM photo_ai_tags WHERE group_id=? AND tag_id=? AND confidence>=? "
            "ORDER BY confidence DESC, score DESC LIMIT ?",
            (self.group_id, int(tag_id), float(min_confidence), int(limit)))
        return [r["photo_id"] for r in rows]

    def combine_with_ai_tags(self, photo_ids: Sequence[str], ai_tags: Sequence[int],
                             min_confidence: float = 0.0) -> List[str]:
        """Keep only ids that carry every requested AI tag."""
        wanted = [int(t) for t in ai_tags if t]
        if not wanted:
            return list(photo_ids)
        allowed: Optional[set] = None
        for tag_id in wanted:
            hits = set(self.photos_by_ai_tag(tag_id, min_confidence, limit=100000))
            allowed = hits if allowed is None else (allowed & hits)
            if not allowed:
                return []
        return [pid for pid in photo_ids if pid in (allowed or set())]
