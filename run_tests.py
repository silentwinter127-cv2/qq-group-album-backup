"""Self-contained test suite (no browser / no network required).

Run with:  python run_tests.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from urllib import request as urlrequest

def _find_project_root() -> str:
    """Locate the directory containing the qqalbum package (works from any cwd)."""
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (here, os.path.dirname(here), os.getcwd()):
        if os.path.isdir(os.path.join(candidate, "qqalbum")):
            return candidate
    return here


sys.path.insert(0, _find_project_root())

from qqalbum.caption import (CAPTION_MODEL_TAG, CaptionEngine, CaptionUnavailable,  # noqa: E402
                             english_only, expand_query, find_caption_dir, has_cjk)
from qqalbum.cdp import CDPError  # noqa: E402
from qqalbum.crawler import BackupManager, SyncState  # noqa: E402
from qqalbum.downloader import (PhotoDownloader, VideoDownloader, photo_url_variants,  # noqa: E402
                                safe_name)
from qqalbum.js_bridge import (batch_id_of, build_attach_info, flatten_feed,  # noqa: E402
                               js_bridge, js_call, parse_attach_info)
from qqalbum.qzone import normalize_adapter_feed  # noqa: E402
from qqalbum.store import Store, epoch_to_text  # noqa: E402
from qqalbum.video import parse_media_item, pickey_from_cover  # noqa: E402
from qqalbum.web import (Api, _csv_cell, annotate_video_covers,  # noqa: E402
                         filter_by_media)  # noqa: E402

G = "123456789"
PHOTO_ID = "V5bCQA2ODQ2OTAyNTQr6chqhCznLg!!"


class AttachInfoTests(unittest.TestCase):
    def test_parse_entities(self):
        info = parse_attach_info("offset=615&amp;total=5181&amp;basetime=1786782523")
        self.assertEqual(info, {"offset": 615, "total": 5181, "basetime": 1786782523})

    def test_parse_plain(self):
        self.assertEqual(parse_attach_info("offset=5&total=9")["offset"], 5)

    def test_parse_empty(self):
        self.assertEqual(parse_attach_info(""), {})

    def test_build_roundtrip(self):
        text = build_attach_info({"offset": 10, "total": 20, "basetime": 7, "junk": 1})
        self.assertEqual(text, "offset=10&total=20&basetime=7")
        self.assertEqual(parse_attach_info(text)["total"], 20)


class NormalizeTests(unittest.TestCase):
    def adapter_feed(self):
        return {
            "comment_total": 1,
            "comments": [{"content": "这张是在哪儿拍的？", "id": "1627821298", "replies": [
                {"content": "上海", "id": "11", "time": 5, "user": {"nick": "b", "uin": "2"}}],
                "reply_total": 1, "time": 1791550517, "type": 2,
                "user": {"nick": "MemberExample", "uin": "100000001"}}],
            "fid": "2147486116",
            "is_liked": 1,
            "like": [{"id": "k1", "nick": "UserExample", "uin": 100000003}],
            "like_total": 2,
            "msg": {"album_id": "ALB1", "album_name": "自制作品", "fdesc": "搬运需要授权",
                    "photo_num": 15, "text": "",
                    "pics": [{"bheight": 640, "bwidth": 360, "cp_x": 180, "cp_y": 320,
                              "pic_key": "KEY1", "url": "https://x/y!!/800?a=1&amp;b=2",
                              "videoflag": 0, "videostatus": 0, "videourl": ""}]},
            "time": 1791551787,
            "user": {"uin": 100000004, "nick": "SomeoneElse"},
            "busi_param": {"21": "2147486116", "7": "1"},
        }

    def test_normalize(self):
        feed = normalize_adapter_feed(self.adapter_feed())
        self.assertEqual(feed["_batch_id"], "2147486116")
        self.assertEqual(feed["user"]["uin"], "100000004")
        self.assertEqual(feed["album"]["album_id"], "ALB1")
        self.assertEqual(feed["album"]["album_name"], "自制作品")
        self.assertEqual(len(feed["pics"]), 1)
        self.assertEqual(feed["pics"][0]["url"], "https://x/y!!/800?a=1&b=2")
        self.assertEqual(feed["comment_total"], 1)
        self.assertEqual(feed["likes"][0]["uin"], "100000003")
        self.assertEqual(feed["comments"][0]["replies"][0]["content"], "上海")
        self.assertEqual(feed["org_like_key"], "k1")

    def test_batch_id(self):
        self.assertEqual(batch_id_of(normalize_adapter_feed(self.adapter_feed())), "2147486116")
        self.assertEqual(batch_id_of({"fid": "999"}), "999")

    def test_user_as_list(self):
        feed = normalize_adapter_feed({"fid": "1", "user": [{"uin": "5", "nick": "n"}]})
        self.assertEqual(feed["user"]["uin"], "5")


class FlattenFeedTests(unittest.TestCase):
    def test_flatten_singlefeed(self):
        single = [None] * 19
        single[0] = {"time": 1700000000, "subid": "555"}
        single[1] = {"user": {"uin": "42", "nickname": "nick"}}
        single[5] = {"albumid": "A1", "albumname": "相册", "desc": "d", "uploadnum": 3,
                     "picdata": [{"lloc": "P1", "photourl": {"11": {"url": "u11", "width": 11,
                                                                    "height": 11},
                                                             "1": {"url": "u", "width": 10,
                                                                   "height": 20},
                                                             "3": {"url": "u3", "width": 30,
                                                                   "height": 40}}}]}
        single[10] = {"num": 2, "commments": []}
        single[11] = {"num": 3, "isliked": 1, "likemans": [{"user": {"uin": "7", "nickname": "z"}}]}
        single[18] = {"busi_param": {"21": "555"}}
        feed = flatten_feed(single)
        self.assertEqual(feed["fid"], "555")
        self.assertEqual(feed["user"]["uin"], "42")
        self.assertEqual(feed["album"]["album_name"], "相册")
        self.assertEqual(feed["comment_total"], 2)
        self.assertEqual(feed["like_total"], 3)
        self.assertEqual(feed["is_liked"], 1)
        self.assertEqual(feed["likes"][0]["uin"], "7")
        self.assertEqual(feed["pics"][0]["url"], "u")  # QZone prefers photourl["1"]
        self.assertEqual(feed["pics"][0]["pic_key"], "P1")


class UrlVariantTests(unittest.TestCase):
    def test_variants_order(self):
        row = {"raw_url": "https://cdn/A/0", "big_url": "https://cdn/A/800",
               "thumb_url": "https://cdn/A/200", "photo_id": "x"}
        variants = photo_url_variants(row)
        self.assertEqual(variants[0], "https://cdn/A/0")
        self.assertIn("https://cdn/A/800", variants)
        self.assertIn("https://cdn/A/200", variants)

    def test_variants_dedup(self):
        row = {"raw_url": "https://cdn/A/0", "big_url": "https://cdn/A/800",
               "thumb_url": "https://cdn/A/200", "photo_id": "x"}
        self.assertEqual(len(photo_url_variants(row)), len(set(photo_url_variants(row))))

    def test_variants_keep_query(self):
        row = {"raw_url": "https://cdn/A/0?w5=1&h5=2", "big_url": None, "thumb_url": None}
        self.assertEqual(photo_url_variants(row)[0], "https://cdn/A/0?w5=1&h5=2")

    def test_safe_name(self):
        self.assertEqual(safe_name("a/b*c"), "a_b_c")
        self.assertEqual(safe_name(""), "file")
        self.assertTrue(len(safe_name("x" * 200, limit=10)) <= 10)


class EpochTests(unittest.TestCase):
    def test_none(self):
        self.assertIsNone(epoch_to_text(None))
        self.assertIsNone(epoch_to_text(0))

    def test_overflow_safe(self):
        self.assertIsNotNone(epoch_to_text(1791551787))
        self.assertIsNone(epoch_to_text(10 ** 20))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "t.sqlite3"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_album_photo_roundtrip(self):
        self.store.upsert_album(G, {"id": "A1", "title": "标题", "desc": "描述", "photocnt": 2,
                                    "createtime": "2025-05-05 09:41:13", "createuin": "1",
                                    "createnickname": "n", "coverurl": "c"})
        album = self.store.query_one("SELECT * FROM albums WHERE group_id=? AND album_id=?", (G, "A1"))
        self.assertEqual(album["title"], "标题")
        self.assertEqual(album["photo_count"], 2)
        self.assertIsNotNone(album["create_epoch"])

        photos = [{**{"id": PHOTO_ID, "_album_id": "A1", "owner": "5", "ownername": "who",
                      "uploadtime": 1791551787, "batchid": "77", "desc": "d",
                      "burl": "http://cdn/x!!/200?w=1", "commentcnt": 3, "hasraw": 1}}]
        self.assertEqual(self.store.upsert_photos(G, photos, {"5": "who"}), 1)
        row = self.store.query_one("SELECT * FROM photos WHERE photo_id=?", (PHOTO_ID,))
        self.assertEqual(row["raw_url"], "http://cdn/x!!/0")
        self.assertEqual(row["big_url"], "http://cdn/x!!/200?w=1")
        self.assertEqual(row["comment_count"], 3)
        self.assertEqual(row["uploader_nick"], "who")

    def test_upsert_is_idempotent(self):
        photo = {"id": PHOTO_ID, "_album_id": "A1", "owner": "5", "uploadtime": 1,
                 "batchid": "b", "burl": "http://cdn/x!!/200"}
        self.store.upsert_photos(G, [photo])
        self.store.upsert_photos(G, [photo])
        self.assertEqual(self.store.query_one(
            "SELECT COUNT(*) c FROM photos WHERE group_id=?", (G,))["c"], 1)

    def test_feed_comments_likes(self):
        feed = normalize_adapter_feed({
            "fid": "300", "time": 100, "user": {"uin": "9", "nick": "up"},
            "msg": {"album_id": "A1", "album_name": "n", "photo_num": 2,
                    "pics": [{"pic_key": "K", "url": "u", "bwidth": 1, "bheight": 2}]},
            "comments": [{"id": "c1", "content": "hi", "time": 5,
                          "user": {"uin": "8", "nick": "cm"},
                          "replies": [{"id": "r1", "content": "re", "time": 6,
                                       "user": {"uin": "7", "nick": "rp"}}]}],
            "like": [{"uin": "6", "nick": "lk", "id": "lk1"}],
            "comment_total": 1, "like_total": 1, "is_liked": 0,
            "busi_param": {"21": "300"},
        })
        self.store.upsert_feed(G, feed)
        self.store.upsert_feed_photos(G, "300", feed["pics"])
        comments = [dict(c, _batch_id="300") for c in feed["comments"]]
        for reply in comments[0]["replies"]:
            comments.append(dict(reply, _batch_id="300", is_reply=1, _parent_id="c1"))
        self.assertEqual(self.store.upsert_comments(G, comments), 2)
        self.assertEqual(self.store.upsert_likes(G, "300", feed["likes"]), 1)
        self.assertEqual(self.store.query_one(
            "SELECT COUNT(*) c FROM comments WHERE group_id=? AND is_reply=1", (G,))["c"], 1)
        self.assertEqual(self.store.query_one(
            "SELECT COUNT(*) c FROM feed_photos WHERE group_id=?", (G,))["c"], 1)

    def test_link_photos_to_feeds(self):
        self.store.upsert_photos(G, [{"id": "P1", "_album_id": "A", "batchid": "B",
                                      "burl": "http://c/1/200"}])
        self.store.upsert_feed(G, normalize_adapter_feed({
            "fid": "B", "time": 1, "user": {"uin": "1", "nick": "n"},
            "msg": {"album_id": "A", "pics": []}, "comments": [], "like": [],
            "comment_total": 7, "like_total": 8, "busi_param": {"21": "B"}}))
        self.store.link_photos_to_feeds(G)
        row = self.store.query_one("SELECT comment_count, like_count FROM photos WHERE photo_id='P1'")
        self.assertEqual((row["comment_count"], row["like_count"]), (7, 8))

    def test_stats_and_members(self):
        self.store.upsert_members(G, [{"uin": "1", "nick": "a"}, {"uin": "2", "nick": "b"}])
        self.assertEqual(self.store.member_names(G), {"1": "a", "2": "b"})
        self.assertEqual(self.store.stats(G)["members"], 2)

    def test_fts(self):
        self.store.upsert_photos(G, [{"id": "P1", "_album_id": "A", "owner": "1",
                                      "ownername": "Alice", "desc": "空壳测试",
                                      "burl": "http://c/1/200"}])
        self.store.upsert_album(G, {"id": "A", "title": "相册A"})
        self.store.rebuild_fts(G)
        self.assertEqual(self.store.query_one(
            "SELECT COUNT(*) c FROM photo_fts WHERE group_id=?", (G,))["c"], 1)

    def test_missing_files_query(self):
        self.store.upsert_photos(G, [{"id": "P1", "_album_id": "A", "burl": "http://c/1/200"}])
        self.assertEqual(len(self.store.photos_missing_files(G)), 1)
        self.store.set_photo_file(G, "P1", "done", path="x", size=10)
        self.assertEqual(len(self.store.photos_missing_files(G)), 0)

    def test_tags(self):
        tag_id = self.store.ensure_tag("风景", "#123456")
        self.assertEqual(self.store.ensure_tag("风景"), tag_id)
        self.store.execute("INSERT OR IGNORE INTO photo_tags (group_id, photo_id, tag_id) VALUES (?,?,?)",
                           (G, "P1", tag_id))
        self.store.commit()
        self.assertEqual(self.store.query_one(
            "SELECT COUNT(*) c FROM photo_tags WHERE tag_id=?", (tag_id,))["c"], 1)


class BridgeTests(unittest.TestCase):
    def test_bridge_is_js(self):
        js = js_bridge()
        self.assertIn("window.QQAlbum", js)
        self.assertIn("getAlbums", js)
        self.assertIn("getPhotos", js)
        self.assertIn("fresh", js)

    def test_js_call(self):
        expr = js_call("getPhotos", "g", "a", 0, 10)
        self.assertEqual(expr, 'window.QQAlbum.getPhotos("g", "a", 0, 10)')


class WebApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "t.sqlite3"))
        self.store.upsert_album(G, {"id": "A1", "title": "相册一", "desc": "d", "photocnt": 3,
                                    "coverurl": "http://c/cover"})
        self.store.upsert_photos(G, [
            {"id": "P1", "_album_id": "A1", "owner": "1", "ownername": "小明",
             "desc": "海边日落", "uploadtime": 100, "batchid": "B1", "commentcnt": 2,
             "burl": "http://c/P1/200"},
            {"id": "P2", "_album_id": "A1", "owner": "2", "ownername": "小红",
             "desc": "山顶", "uploadtime": 200, "batchid": "B2", "burl": "http://c/P2/200"},
        ])
        self.store.upsert_feed(G, normalize_adapter_feed({
            "fid": "B1", "time": 100, "user": {"uin": "1", "nick": "小明"},
            "msg": {"album_id": "A1", "album_name": "相册一", "pics": []},
            "comments": [{"id": "c1", "content": "好看", "time": 5,
                          "user": {"uin": "3", "nick": "路人"}}],
            "like": [{"uin": "4", "nick": "点赞者", "id": "k"}],
            "comment_total": 1, "like_total": 1, "busi_param": {"21": "B1"}}))
        self.store.upsert_comments(G, [{"id": "c1", "content": "好看", "time": 5, "uin": "3",
                                        "nick": "路人", "_batch_id": "B1"}])
        self.store.upsert_likes(G, "B1", [{"uin": "4", "nick": "点赞者"}])
        self.store.rebuild_fts(G)
        manager = BackupManager(G, self.store, lambda: None, root_dir=self.tmp.name)
        self.api = Api(self.store, manager, G, self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_overview(self):
        data = self.api.overview()
        self.assertEqual(data["stats"]["photos"], 2)
        self.assertEqual(len(data["albums"]), 1)
        self.assertEqual(data["albums"][0]["stored"], 2)

    def test_photo_filters(self):
        self.assertEqual(self.api.photos({})["total"], 2)
        self.assertEqual(self.api.photos({"album_id": ["A1"]})["total"], 2)
        self.assertEqual(self.api.photos({"uploader": ["2"]})["total"], 1)
        self.assertEqual(self.api.photos({"has_comment": ["1"]})["total"], 1)
        self.assertEqual(self.api.photos({"q": ["海边"]})["total"], 1)
        self.assertEqual(self.api.photos({"q": ["好看"]})["total"], 1)  # matches comment text
        self.assertEqual(self.api.photos({"order": ["upload_desc"]})["items"][0]["photo_id"], "P2")
        self.assertEqual(self.api.photos({"per_page": ["1"], "page": ["2"]})["page"], 2)

    def test_download_status_filters(self):
        """downloaded=0 must mean 'needs work', not 'QQ has no original'."""
        # P1: real original stored; P2: only a CDN stub exists
        self.store.set_photo_file(G, "P1", "done", path="/tmp/p1.jpg", size=1000)
        self.store.set_photo_file(G, "P2", "thumb-only", path="/tmp/p2.jpg", size=300)
        self.assertEqual(self.api.photos({"downloaded": ["1"]})["total"], 2)
        self.assertEqual(self.api.photos({"downloaded": ["full"]})["total"], 1)
        self.assertEqual(self.api.photos({"thumb_only": ["1"]})["total"], 1)
        self.assertEqual(self.api.photos({"downloaded": ["0"]})["total"], 0)
        # a genuinely pending photo shows up as needing work
        self.store.upsert_photos(G, [{"id": "P3", "_album_id": "A1", "burl": "http://c/P3/200"}])
        self.assertEqual(self.api.photos({"downloaded": ["0"]})["total"], 1)
        self.assertEqual(self.api.photos({"file_status": ["error"]})["total"], 0)

    def test_photo_detail(self):
        detail = self.api.photo_detail("P1")
        self.assertEqual(detail["photo"]["album_title"], "相册一")
        self.assertEqual(len(detail["comments"]), 1)
        self.assertEqual(len(detail["likes"]), 1)
        self.assertEqual(detail["photo"]["comment_count"], 2)
        self.assertEqual(detail["comments"][0]["content"], "好看")

    def test_photo_detail_missing(self):
        self.assertEqual(self.api.photo_detail("nope")["error"], "not found")

    def test_tags_flow(self):
        tag = self.api.add_tag("测试")
        self.assertEqual(self.api.tag_photos(tag["tag_id"], ["P1"]), 1)
        self.assertEqual(self.api.photos({"tag_id": [str(tag["tag_id"])]})["total"], 1)
        self.api.tag_photos(tag["tag_id"], ["P1"], remove=True)
        self.assertEqual(self.api.photos({"tag_id": [str(tag["tag_id"])]})["total"], 0)

    def test_auto_tag(self):
        self.api.auto_tag_by_uploader()
        names = {t["name"] for t in self.api.tags()}
        self.assertIn("上传者:小明", names)
        self.assertIn("上传者:小红", names)

    def test_uploaders_and_timeline(self):
        self.assertEqual(len(self.api.uploaders()), 2)
        self.assertEqual(len(self.api.top_uploaders()), 2)
        self.assertTrue(self.api.timeline())

    def test_sync_controls(self):
        self.assertFalse(self.api.sync_status()["running"])
        release = threading.Event()
        fake = type("FakeClient", (), {"connect": lambda self: release.wait(5) and {},
                                       "session_info": {}})()
        self.api.manager.client_factory = lambda: fake
        self.assertTrue(self.api.start_sync({})["started"])
        self.assertFalse(self.api.start_sync({})["started"])  # already running
        self.api.cancel()
        release.set()
        for _ in range(50):
            if not self.api.sync_status()["running"]:
                break
            time.sleep(0.05)

    def test_export(self):
        rows = self.api.export_rows()
        self.assertEqual(len(rows), 2)
        self.assertIn("album_title", rows[0])

    def test_csv_cell(self):
        self.assertEqual(_csv_cell("a,b"), '"a,b"')
        self.assertEqual(_csv_cell('say "hi"'), '"say ""hi"""')
        self.assertEqual(_csv_cell(None), "")


class DownloaderTests(unittest.TestCase):
    """Stub/partial handling without touching the network."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "d.sqlite3"))
        self.row = {"photo_id": "P1", "album_id": "A1", "uploader_uin": "7",
                    "group_id": G, "raw_url": "https://cdn/A1/P1/0",
                    "big_url": "https://cdn/A1/P1/800", "thumb_url": "https://cdn/A1/P1/200",
                    "is_animated": 1}
        self.store.upsert_photos(G, [dict(self.row, id="P1", _album_id="A1", btype=2, owner="7",
                                          burl="http://c/1/200")])
        self.stored = self.store.query_one("SELECT * FROM photos WHERE photo_id='P1'")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_target_path_uses_gif_for_animated(self):
        d = PhotoDownloader(self.store, self.tmp.name)
        path = d.target_path(self.stored)
        self.assertTrue(path.endswith(".gif"), path)
        self.assertIn(os.path.join("photos", G, "A1", "7"), path)

    def test_stub_rank(self):
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (16, 16), "red").save(buf, "JPEG")
        self.assertEqual(PhotoDownloader._stub_rank(buf.getvalue()), 16)
        big = io.BytesIO()
        Image.new("RGB", (400, 300), "blue").save(big, "JPEG")
        self.assertIsNone(PhotoDownloader._stub_rank(big.getvalue()))
        self.assertIsNone(PhotoDownloader._stub_rank(b"not an image"))

    def test_download_stores_stub_and_leaves_no_part_files(self):
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (16, 16), "red").save(buf, "JPEG")
        payload = buf.getvalue()

        class FakeResponse(io.BytesIO):
            headers = {"Content-Type": "image/jpeg"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        d = PhotoDownloader(self.store, self.tmp.name, retries=1)
        original = urlrequest.urlopen
        urlrequest.urlopen = lambda *a, **k: FakeResponse(payload)
        try:
            result = d.download_one(self.stored, G)
        finally:
            urlrequest.urlopen = original

        self.assertTrue(result["ok"])
        self.assertEqual(result.get("status"), "thumb-only")
        self.assertTrue(os.path.isfile(result["path"]))
        # no .part leftovers anywhere
        leftovers = []
        for root, _dirs, names in os.walk(os.path.join(self.tmp.name, "photos")):
            leftovers += [n for n in names if ".part" in n]
        self.assertEqual(leftovers, [])
        row = self.store.query_one("SELECT file_status FROM photos WHERE photo_id='P1'")
        self.assertEqual(row["file_status"], "thumb-only")

    def test_sweep_orphan_parts(self):
        d = PhotoDownloader(self.store, self.tmp.name)
        d = PhotoDownloader(self.store, self.tmp.name)
        folder = os.path.join(self.tmp.name, "photos", G, "A1", "7")
        os.makedirs(folder, exist_ok=True)
        junk = os.path.join(folder, "x.jpg.part.123")
        keep = os.path.join(folder, "x.jpg")
        for path in (junk, keep):
            with open(path, "wb") as fh:
                fh.write(b"data")
        removed = d.sweep_orphan_parts()
        self.assertEqual(removed, 1)
        self.assertFalse(os.path.exists(junk))
        self.assertTrue(os.path.exists(keep))


class CrawlPipelineTests(unittest.TestCase):
    """Exercise BackupManager against a fake QZone client (regression guard)."""

    class FakeClient:
        def __init__(self, albums, photos_by_album, server_time=1000):
            self._albums = albums
            self._photos = photos_by_album
            self._server_time = server_time
            self.session_info = {"uin": "1", "logged_in": True}
            self.page = self
            self.last_feed_attach = ""
            self.last_feed_attach_before = ""
            self.new_feeds_since_chunk = 0
            self.restarts = 0

        # QzoneClient surface used by BackupManager
        def eval(self, *_a, **_k):
            return 2

        def connect(self):
            return self.session_info

        def get_server_time(self):
            return self._server_time

        def iter_albums(self, page_size=100):
            for album in self._albums:
                yield album

        def iter_photos(self, album_id, expected=None, page_size=500, progress=None,
                        should_stop=None):
            for photo in self._photos.get(album_id, []):
                yield dict(photo)

        def get_members(self):
            return []

        def get_member_info(self, uins):
            return [{"uin": u, "nick": f"nick{u}"} for u in uins]

        def iter_feeds(self, **kwargs):
            return iter(())

        def hard_restart(self):
            self.restarts += 1
            self.session_info = {"uin": "1", "logged_in": True}
            return self.session_info

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "p.sqlite3"))
        self.albums = [{"id": "A1", "title": "相册一", "photocnt": 3},
                       {"id": "A2", "title": "相册二", "photocnt": 1}]
        self.photos = {
            "A1": [{"id": "P1", "_album_id": "A1", "owner": "9", "ownername": "小九",
                    "uploadtime": 900, "batchid": "b1", "burl": "http://c/1/200"},
                   {"id": "P2", "_album_id": "A1", "owner": "9", "ownername": "小九",
                    "uploadtime": 800, "batchid": "b1", "burl": "http://c/2/200"},
                   {"id": "P3", "_album_id": "A1", "owner": "8", "ownername": "小八",
                    "uploadtime": 700, "batchid": "b2", "burl": "http://c/3/200"}],
            "A2": [{"id": "P4", "_album_id": "A2", "owner": "8", "ownername": "小八",
                    "uploadtime": 600, "batchid": "b3", "burl": "http://c/4/200"}],
        }

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass
        for _ in range(30):
            try:
                self.tmp.cleanup()
                break
            except PermissionError:
                time.sleep(0.1)

    def _manager(self, client):
        return BackupManager(G, self.store, lambda: client, root_dir=self.tmp.name,
                             page_size=2, max_feeds=5)

    def _run(self, client, **opts):
        manager = self._manager(client)
        manager.start_sync(**opts)
        deadline = time.time() + 20
        while manager.state.running and time.time() < deadline:
            time.sleep(0.05)
        self.addCleanup(self.store.close)
        return manager

    def test_full_photo_crawl(self):
        client = self.FakeClient(self.albums, self.photos)
        manager = self._run(client, albums=True, photos=True, feeds=False, members=True)
        self.assertEqual(manager.state.stage, "done", manager.state.error)
        self.assertEqual(manager.state.error, None)
        self.assertEqual(self.store.stats(G)["photos"], 4)
        self.assertEqual(self.store.stats(G)["albums"], 2)
        # cursor per album is the newest upload time seen
        self.assertEqual(int(self.store.get_meta(f"album_cursor:{G}:A1")), 900)
        self.assertEqual(int(self.store.get_meta(f"album_cursor:{G}:A2")), 600)
        # uploader nicknames copied onto the photo rows
        row = self.store.query_one("SELECT uploader_nick FROM photos WHERE photo_id='P1'")
        self.assertEqual(row["uploader_nick"], "小九")

    def test_incremental_skips_old_photos(self):
        client = self.FakeClient(self.albums, self.photos)
        self._run(client, albums=True, photos=True, feeds=False, members=False)
        # a new photo arrives, then an incremental run must fetch it and stop early
        newer = dict(self.photos["A1"][0], id="P0", uploadtime=1000)
        client._photos["A1"] = [newer] + self.photos["A1"]
        client._albums = [{"id": "A1", "title": "相册一", "photocnt": 4}]
        manager = self._run(client, albums=False, photos=True, feeds=False, members=False,
                            incremental=True)
        self.assertEqual(manager.state.stage, "done")
        self.assertEqual(self.store.query_one("SELECT COUNT(*) c FROM photos")["c"], 5)
        self.assertEqual(int(self.store.get_meta(f"album_cursor:{G}:A1")), 1000)
        log = "\n".join(manager.state.log)
        self.assertIn("到达已有数据边界", log)

    def test_album_crawl_recovers_from_wedge(self):
        """A timeout mid-album must restart the browser and finish the album."""
        client = self.FakeClient(self.albums, self.photos)
        # seed the album list the way a previous run would have left it
        manager0 = self._run(client, albums=True, photos=False, feeds=False, members=False)
        self.assertEqual(manager0.state.stage, "done")

        calls = {"n": 0}
        original = client.iter_photos
        state = {"wedged": False}

        def flaky(album_id, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                # the renderer stops answering JS too, exactly like the real wedge
                state["wedged"] = True
                raise CDPError("websocket read failed: timed out")
            return original(album_id, **kwargs)

        def fake_eval(*_a, **_k):
            if state["wedged"]:
                raise CDPError("timeout waiting for Runtime.evaluate")
            return 2

        def restart():
            state["wedged"] = False
            client.restarts += 1
            return client.session_info

        client.iter_photos = flaky
        client.eval = fake_eval
        client.hard_restart = restart
        manager = self._run(client, albums=False, photos=True, feeds=False, members=False)
        self.assertEqual(manager.state.stage, "done", manager.state.error)
        self.assertGreaterEqual(client.restarts, 1)
        self.assertEqual(self.store.stats(G)["photos"], 4)
        self.assertIn("重启浏览器", "\n".join(manager.state.log))


class AiStorageTests(unittest.TestCase):
    """AI tag / embedding storage and search plumbing (no model download needed)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "ai.sqlite3"))
        self.photo_ids = ["P1", "P2", "P3"]
        self.store.upsert_photos(G, [
            {"id": pid, "_album_id": "A1", "owner": "7", "ownername": "小红",
             "uploadtime": 1000 - i * 10, "burl": f"http://c/{pid}/200"}
            for i, pid in enumerate(self.photo_ids)])

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_model_tag_ids(self):
        for name, prompt in [("食物", "a photo of food"), ("猫", "a photo of a cat")]:
            self.store.ensure_model_tag(name, prompt, "#fff")
        ids = self.store.model_tag_ids(["食物", "猫", "不存在"])
        self.assertEqual(set(ids), {"食物", "猫"})
        self.store.ensure_model_tag("食物", "a photo of food")
        self.assertEqual(len(self.store.model_tag_ids(["食物"])), 1)

    def test_ai_tag_roundtrip_and_confidence_order(self):
        tag_food = self.store.ensure_model_tag("食物", "a photo of food")
        tag_cat = self.store.ensure_model_tag("猫", "a photo of a cat")
        self.store.upsert_ai_tags(G, "P1", [
            {"tag_id": tag_cat, "score": 0.02, "confidence": 1.0, "rank": 1},
            {"tag_id": tag_food, "score": 0.005, "confidence": 0.25, "rank": 2},
        ], model="m")
        tags = self.store.ai_tags_of_photo(G, "P1")
        self.assertEqual([t["name"] for t in tags], ["猫", "食物"])
        self.assertEqual(tags[0]["confidence"], 1.0)
        # limit + min_confidence
        self.assertEqual(len(self.store.ai_tags_of_photo(G, "P1", limit=1)), 1)
        self.assertEqual(len(self.store.ai_tags_of_photo(G, "P1", min_confidence=0.5)), 1)
        # upsert is idempotent
        self.store.upsert_ai_tags(G, "P1", [{"tag_id": tag_cat, "score": 0.9,
                                             "confidence": 1.0, "rank": 1}])
        self.assertEqual(len(self.store.ai_tags_of_photo(G, "P1")), 2)
        self.assertAlmostEqual(self.store.ai_tags_of_photo(G, "P1")[0]["score"], 0.9, places=5)

    def test_clear_ai_tags(self):
        tag = self.store.ensure_model_tag("食物", "food")
        for pid in self.photo_ids:
            self.store.upsert_ai_tags(G, pid, [{"tag_id": tag, "score": 1, "confidence": 1, "rank": 1}])
        self.assertEqual(self.store.ai_stats(G)["ai_tag_rows"], 3)
        self.store.clear_ai_tags(G, ["P1", "P2"])
        self.assertEqual(self.store.ai_stats(G)["ai_tag_rows"], 1)
        self.store.clear_ai_tags(G)
        self.assertEqual(self.store.ai_stats(G)["ai_tag_rows"], 0)

    def test_embedding_blob_roundtrip(self):
        import numpy as np  # noqa: PLC0415
        from qqalbum.siglip import blob_to_vector, build_matrix, vector_to_blob, VECTOR_DIM  # noqa: PLC0415
        vectors = []
        for i, pid in enumerate(self.photo_ids):
            vec = np.zeros(VECTOR_DIM, dtype="<f4")
            vec[i] = 1.0
            vectors.append(vec)
            self.store.upsert_embeddings(G, [(pid, vector_to_blob(vec))], "m", VECTOR_DIM)
        self.assertEqual(self.store.embedding_count(G), 3)
        self.assertEqual(self.store.embedding_count(G, "other"), 0)
        ids, blobs = self.store.all_embeddings(G)
        self.assertEqual(sorted(ids), sorted(self.photo_ids))
        matrix = build_matrix(blobs)
        self.assertEqual(matrix.shape, (3, VECTOR_DIM))
        self.assertAlmostEqual(float(matrix[0] @ matrix[0]), 1.0, places=5)
        self.assertAlmostEqual(float(matrix[0] @ matrix[1]), 0.0, places=5)
        self.assertEqual(blob_to_vector(vector_to_blob(vectors[0])).shape, (VECTOR_DIM,))

    def test_photos_without_embedding(self):
        from qqalbum.siglip import vector_to_blob  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
        # indexing only considers photos that actually have a local file
        for pid in self.photo_ids:
            self.store.set_photo_file(G, pid, "done", path=f"/tmp/{pid}.jpg", size=1234)
        pending = self.store.photos_without_embedding(G, "m")
        self.assertEqual(len(pending), 3)
        vec = np.ones(768, dtype="<f4")
        self.store.upsert_embeddings(G, [("P1", vector_to_blob(vec))], "m", 768)
        pending = self.store.photos_without_embedding(G, "m")
        self.assertEqual(sorted(r["photo_id"] for r in pending), ["P2", "P3"])
        # a different model id does not count as done
        self.assertEqual(len(self.store.photos_without_embedding(G, "other")), 3)
        # a photo without a local file is never indexed
        self.store.upsert_photos(G, [{"id": "P9", "_album_id": "A1", "burl": "http://c/9/200"}])
        self.assertNotIn("P9", [r["photo_id"] for r in self.store.photos_without_embedding(G, "m")])

    def test_search_ranks_by_cosine_and_respects_filters(self):
        import numpy as np  # noqa: PLC0415
        from qqalbum.ai import AiSearch  # noqa: PLC0415
        from qqalbum.siglip import vector_to_blob  # noqa: PLC0415
        # P1 points at axis0, P2 nearly axis0, P3 at axis1
        for pid, vec in (("P1", [1.0, 0.0]), ("P2", [0.98, 0.1]), ("P3", [0.0, 1.0])):
            full = np.zeros(768, dtype="<f4")
            full[0], full[1] = vec
            self.store.upsert_embeddings(G, [(pid, vector_to_blob(full))], "m", 768)
        searcher = AiSearch(self.store, G)
        searcher.refresh(force=True)
        query = np.zeros(768, dtype="<f4")
        query[0] = 1.0
        hits = searcher.search_by_vector(query, top_k=3)
        self.assertEqual([h["photo_id"] for h in hits], ["P1", "P2", "P3"])
        self.assertGreater(hits[0]["score"], hits[1]["score"])
        # uploader filter: only "7" exists, so a bogus uploader yields nothing
        hits = searcher.search_by_vector(query, top_k=3, filters={"uploader": "999"})
        self.assertEqual(hits, [])
        # date filter: the synthetic photos sit at epoch ~1000 (1970-01-01 UTC+8),
        # so a range starting in 2020 must exclude all of them
        hits = searcher.search_by_vector(query, top_k=3, filters={"date_from": "2020-01-01"})
        self.assertEqual(hits, [])
        # a wide range keeps them, and an end date far in the past excludes them
        hits = searcher.search_by_vector(query, top_k=3,
                                         filters={"date_from": "2000-01-01",
                                                  "date_to": "2000-12-31"})
        self.assertEqual(hits, [])
        hits = searcher.search_by_vector(query, top_k=3, filters={"date_to": "2100-01-01"})
        self.assertEqual(len(hits), 3)

    def test_search_similar_excludes_self(self):
        import numpy as np  # noqa: PLC0415
        from qqalbum.ai import AiSearch  # noqa: PLC0415
        from qqalbum.siglip import vector_to_blob  # noqa: PLC0415
        for pid in self.photo_ids:
            vec = np.zeros(768, dtype="<f4")
            vec[0] = 1.0
            self.store.upsert_embeddings(G, [(pid, vector_to_blob(vec))], "m", 768)
        searcher = AiSearch(self.store, G)
        hits = searcher.search_similar_to("P1", top_k=5)
        self.assertEqual(len(hits), 2)
        self.assertNotIn("P1", [h["photo_id"] for h in hits])
        self.assertEqual(searcher.search_similar_to("unknown", top_k=5), [])

    def test_photos_by_ai_tag_and_combine(self):
        import numpy as np  # noqa: PLC0415
        from qqalbum.ai import AiSearch  # noqa: PLC0415
        from qqalbum.siglip import vector_to_blob  # noqa: PLC0415
        tag_a = self.store.ensure_model_tag("A", "a")
        tag_b = self.store.ensure_model_tag("B", "b")
        self.store.upsert_ai_tags(G, "P1", [{"tag_id": tag_a, "score": 1, "confidence": 1, "rank": 1},
                                            {"tag_id": tag_b, "score": 0.5, "confidence": 0.5, "rank": 2}])
        self.store.upsert_ai_tags(G, "P2", [{"tag_id": tag_a, "score": 1, "confidence": 1, "rank": 1}])
        searcher = AiSearch(self.store, G)
        self.assertEqual(sorted(searcher.photos_by_ai_tag(tag_a)), ["P1", "P2"])
        self.assertEqual(searcher.photos_by_ai_tag(tag_b), ["P1"])
        self.assertEqual(searcher.combine_with_ai_tags(["P1", "P2"], [tag_a, tag_b]), ["P1"])
        self.assertEqual(searcher.combine_with_ai_tags(["P1", "P2"], []), ["P1", "P2"])

    def test_ai_stats(self):
        tag = self.store.ensure_model_tag("食物", "food")
        self.store.upsert_ai_tags(G, "P1", [{"tag_id": tag, "score": 1, "confidence": 1, "rank": 1}])
        stats = self.store.ai_stats(G)
        self.assertEqual(stats["tagged_photos"], 1)
        self.assertEqual(stats["ai_tag_rows"], 1)
        self.assertEqual(stats["tags_used"], 1)
        self.assertGreaterEqual(stats["vocab_size"], 1)


class SiglipHelperTests(unittest.TestCase):
    def test_vocab_shape(self):
        from qqalbum.siglip import VOCAB, tag_color  # noqa: PLC0415
        self.assertGreater(len(VOCAB), 80)
        names = [n for n, _ in VOCAB]
        self.assertEqual(len(names), len(set(names)), "vocabulary labels must be unique")
        for name, prompt in VOCAB:
            self.assertTrue(name and prompt)
            self.assertTrue(prompt[0].islower(), prompt)
        self.assertTrue(tag_color("食物").startswith("#"))
        self.assertTrue(tag_color("不存在的标签").startswith("#"))

    def test_date_arg_parsing(self):
        from qqalbum.store import parse_date_arg  # noqa: PLC0415
        self.assertEqual(parse_date_arg("2025-01-31"), 1738252800)
        self.assertEqual(parse_date_arg("2025-01-31", end_of_day=True), 1738339199)
        self.assertEqual(parse_date_arg("1746409202"), 1746409202)
        self.assertIsNone(parse_date_arg(""))
        self.assertIsNone(parse_date_arg("nonsense"))
        # pre-1970 must not raise (mktime overflows on Windows)
        self.assertIsInstance(parse_date_arg("1950-01-01"), int)
        self.assertIsInstance(parse_date_arg("1900-06-15", end_of_day=True), int)

    def test_stub_rank_helpers(self):
        import io  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415
        from qqalbum.downloader import PhotoDownloader  # noqa: PLC0415
        buf = io.BytesIO()
        Image.new("RGB", (16, 16)).save(buf, "JPEG")
        self.assertEqual(PhotoDownloader._stub_rank(buf.getvalue()), 16)
        big = io.BytesIO()
        Image.new("RGB", (300, 200)).save(big, "JPEG")
        self.assertIsNone(PhotoDownloader._stub_rank(big.getvalue()))


class CaptionStoreTests(unittest.TestCase):
    """BLIP caption storage, FTS search and query handling."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "c.sqlite3"))
        self.store.upsert_photos(G, [
            {"id": "P1", "_album_id": "A1", "owner": "7", "uploadtime": 100, "burl": "http://c/1/200"},
            {"id": "P2", "_album_id": "A1", "owner": "7", "uploadtime": 200, "burl": "http://c/2/200"},
        ])

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_upsert_and_read(self):
        self.assertEqual(self.store.upsert_captions(G, [
            ("P1", "a girl with purple hair", "a girl with purple hair"),
            ("P2", "a bus next to a city", "a bus next to a city"),
        ], model=CAPTION_MODEL_TAG), 2)
        self.assertEqual(self.store.caption_count(G), 2)
        row = self.store.caption_of_photo(G, "P1")
        self.assertEqual(row["caption"], "a girl with purple hair")
        self.assertEqual(row["model"], CAPTION_MODEL_TAG)

    def test_upsert_is_idempotent_and_updates(self):
        self.store.upsert_captions(G, [("P1", "one", "one")], model="m")
        self.store.upsert_captions(G, [("P1", "two", "two")], model="m")
        self.assertEqual(self.store.caption_count(G), 1)
        self.assertEqual(self.store.caption_of_photo(G, "P1")["caption"], "two")
        # the full-text index must not keep the stale text
        self.assertEqual(self.store.search_captions(G, "one"), [])
        self.assertEqual(len(self.store.search_captions(G, "two")), 1)

    def test_search_english_and_multiword(self):
        self.store.upsert_captions(G, [
            ("P1", "a girl with purple hair and a pink background",
             "a girl with purple hair and a pink background"),
            ("P2", "a street with a bicycle and a building",
             "a street with a bicycle and a building"),
        ], model="m")
        self.assertEqual(self.store.search_captions(G, "girl")[0][0], "P1")
        self.assertEqual(self.store.search_captions(G, "bicycle")[0][0], "P2")
        self.assertEqual(self.store.search_captions(G, "purple girl")[0][0], "P1")
        self.assertEqual(self.store.search_captions(G, "!!!"), [])
        self.assertEqual(self.store.search_captions(G, ""), [])

    def test_photos_without_caption(self):
        for pid in ("P1", "P2"):
            self.store.set_photo_file(G, pid, "done", path=f"/tmp/{pid}.jpg", size=100)
        self.assertEqual(len(self.store.photos_without_caption(G, "m")), 2)
        self.store.upsert_captions(G, [("P1", "x", "x")], model="m")
        self.assertEqual([r["photo_id"] for r in self.store.photos_without_caption(G, "m")], ["P2"])
        # another model id does not count as done
        self.assertEqual(len(self.store.photos_without_caption(G, "other")), 2)

    def test_clear_captions(self):
        self.store.upsert_captions(G, [("P1", "one", "one"), ("P2", "two", "two")], model="m")
        self.assertEqual(self.store.clear_captions(G), 2)
        self.assertEqual(self.store.caption_count(G), 0)
        self.assertEqual(self.store.search_captions(G, "one"), [])

    def test_ai_stats_includes_captions(self):
        self.store.upsert_captions(G, [("P1", "one", "one")], model="m")
        self.assertEqual(self.store.ai_stats(G)["captioned"], 1)


class CaptionQueryTests(unittest.TestCase):
    def test_has_cjk(self):
        self.assertTrue(has_cjk("女孩"))
        self.assertTrue(has_cjk("a 女孩 b"))
        self.assertFalse(has_cjk("a girl"))
        self.assertFalse(has_cjk(""))

    def test_expand_query(self):
        expanded = expand_query("一个女孩拿着包")
        self.assertIn("girl", expanded)
        self.assertIn("bag", expanded)
        self.assertEqual(expand_query("a girl"), "a girl")
        self.assertEqual(expand_query("完全未知的词"), "完全未知的词")

    def test_english_only(self):
        self.assertEqual(english_only("a 女孩 b"), "a b")
        self.assertEqual(english_only("a girl"), "a girl")
        self.assertEqual(english_only("女孩"), "")

    def test_find_caption_dir_reports_missing(self):
        with self.assertRaises(CaptionUnavailable):
            find_caption_dir(os.path.join(tempfile.gettempdir(), "definitely-not-a-model"))

    def test_explicit_dir_must_exist(self):
        """A typo in --caption-dir must fail loudly instead of using another model."""
        with self.assertRaises(CaptionUnavailable):
            find_caption_dir(r"D:\nope-not-here")

    def test_engine_available_shape(self):
        engine = CaptionEngine(r"D:\definitely-missing-model")
        ok, reason = engine.available()
        self.assertFalse(ok)
        self.assertTrue(reason)


class VideoParseTests(unittest.TestCase):
    """视频清单解析：封面 pickey 提取 与 mediaList 项识别。"""

    def test_pickey_from_cover(self):
        url = ("https://qungz.photo.store.qq.com/qun-qungz/V63ehHrD4K/V5bCQA2ODQ2OTAyNTQH4RVquOH5Kw!!"
               "/400?w5=1564&h5=1080&t=1&rf=groupPhoto")
        self.assertEqual(pickey_from_cover(url), "V5bCQA2ODQ2OTAyNTQH4RVquOH5Kw!!")

    def test_pickey_from_cover_variants(self):
        self.assertEqual(pickey_from_cover("http://h/a/ABC!!/800"), "ABC!!")
        self.assertEqual(pickey_from_cover("http://h/a/ABC!!"), "ABC!!")
        self.assertEqual(pickey_from_cover(""), "")
        self.assertEqual(pickey_from_cover("not a url"), "")

    def test_parse_media_item_video_only(self):
        self.assertIsNone(parse_media_item({"type": 1, "image": {"lloc": "X"}}))
        parsed = parse_media_item({
            "type": 1,
            "video": {
                "id": "1071_abc",
                "url": "https://groupvideo.photo.qq.com/1071_abc.f5020.mp4?dis_k=k&dis_t=1",
                "width": 1920, "height": 1080, "time": 12000,
                "cover": {
                    # cover.lloc 是另一种编码，和 photos.lloc 对不上 —— 必须忽略它
                    "lloc": "SOMETHING-ELSE",
                    "photoUrls": [
                        {"spec": 1, "url": {"url": "https://c/p/COVERPIC!!/200"}},
                        {"spec": 3, "url": {"url": "https://c/p/COVERPIC!!/800"}},
                    ],
                },
            },
        })
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["video_id"], "1071_abc")
        self.assertEqual(parsed["lloc"], "COVERPIC!!")
        self.assertEqual(parsed["width"], 1920)
        self.assertEqual(parsed["duration_ms"], 12000)
        self.assertIn("/800", parsed["cover_url"])

    def test_parse_media_item_without_id_is_ignored(self):
        self.assertIsNone(parse_media_item({"video": {"url": ""}}))


class VideoStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "v.sqlite3"))

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_upsert_and_stats(self):
        self.assertEqual(self.store.upsert_videos(G, [
            {"video_id": "v1", "album_id": "A1", "lloc": "L1", "video_url": "http://x/1.mp4",
             "width": 640, "height": 480},
            {"video_id": "v2", "album_id": "A1", "lloc": "L2"},
        ]), 2)
        self.assertEqual(self.store.video_count(G), 2)
        stats = self.store.video_stats(G)
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["pending"], 2)

    def test_upsert_keeps_download_state(self):
        """重新抓元数据不能把「已下载」抹掉，否则增量会永远重下。"""
        self.store.upsert_videos(G, [{"video_id": "v1", "album_id": "A1", "lloc": "L1"}])
        self.store.set_video_file(G, "v1", "done", path="/tmp/v1.mp4", size=999)
        self.store.upsert_videos(G, [{"video_id": "v1", "album_id": "A1", "lloc": "L1",
                                      "width": 100}])
        row = self.store.query_one("SELECT * FROM videos WHERE group_id=? AND video_id=?", (G, "v1"))
        self.assertEqual(row["file_status"], "done")
        self.assertEqual(row["file_size"], 999)
        self.assertEqual(row["width"], 100)
        self.assertEqual(len(self.store.videos_missing_files(G)), 0)

    def test_missing_files_excludes_done_and_skipped(self):
        self.store.upsert_videos(G, [{"video_id": f"v{i}"} for i in range(4)])
        self.store.set_video_file(G, "v0", "done")
        self.store.set_video_file(G, "v1", "skipped")
        self.store.set_video_file(G, "v2", "failed")
        ids = {r["video_id"] for r in self.store.videos_missing_files(G)}
        self.assertEqual(ids, {"v2", "v3"})

    def test_videos_list_filters(self):
        self.store.upsert_videos(G, [
            {"video_id": "v1", "album_id": "A1", "album_title": "甲"},
            {"video_id": "v2", "album_id": "A2", "album_title": "乙"},
        ])
        self.store.set_video_file(G, "v1", "done")
        self.assertEqual(self.store.videos(G)["total"], 2)
        self.assertEqual(self.store.videos(G, album_id="A1")["total"], 1)
        self.assertEqual(self.store.videos(G, downloaded="1")["total"], 1)
        self.assertEqual(self.store.videos(G, downloaded="0")["total"], 1)
        self.assertEqual(self.store.videos(G, per_page=1)["total"], 2)

    def test_link_videos_to_photos(self):
        self.store.upsert_photos(G, [
            {"id": "P1", "lloc": "P1!!", "_album_id": "A1", "owner": "7",
             "uploadtime": 100, "burl": "http://c/1/200"},
        ])
        self.store.upsert_videos(G, [{"video_id": "v1", "album_id": "A1", "lloc": "P1!!"}])
        self.assertEqual(self.store.link_videos_to_photos(G), 1)
        row = self.store.query_one("SELECT photo_id FROM videos WHERE group_id=? AND video_id=?",
                                   (G, "v1"))
        self.assertEqual(row["photo_id"], "P1")

    def test_set_video_url(self):
        self.store.upsert_videos(G, [{"video_id": "v1", "video_url": "old"}])
        self.store.set_video_url(G, "v1", "new-url")
        row = self.store.query_one("SELECT video_url FROM videos WHERE video_id=?", ("v1",))
        self.assertEqual(row["video_url"], "new-url")


class VideoDownloaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "d.sqlite3"))
        self.dl = VideoDownloader(self.store, self.tmp.name, workers=1,
                                  log=lambda _m: None, min_bytes=16)

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_target_path_uses_mp4(self):
        path = self.dl.target_path({"group_id": G, "album_id": "A1", "video_id": "v1"})
        self.assertTrue(path.endswith(".mp4"))
        self.assertIn(os.path.join("videos", G, "A1"), path)

    def test_mp4_sniffing(self):
        self.assertTrue(self.dl._is_mp4(b"\x00\x00\x00 ftypisom...."))
        self.assertFalse(self.dl._is_mp4(b"<html><body>nope"))
        self.assertFalse(self.dl._is_mp4(b""))

    def test_no_url_marks_failed(self):
        self.store.upsert_videos(G, [{"video_id": "v1"}])
        row = self.store.videos_missing_files(G)[0]
        res = self.dl.download_one(row, G)
        self.assertFalse(res["ok"])
        self.assertEqual(self.store.query_one(
            "SELECT file_status FROM videos WHERE video_id=?", ("v1",))["file_status"], "failed")

    def test_existing_file_is_skipped(self):
        self.store.upsert_videos(G, [{"video_id": "v1", "album_id": "A1",
                                      "video_url": "http://unused/x.mp4"}])
        row = self.store.videos_missing_files(G)[0]
        path = self.dl.target_path(row)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"\x00\x00\x00 ftypisom" + b"x" * 100)
        res = self.dl.download_one(row, G)
        self.assertTrue(res.get("skipped"))


class VideoCoverSearchTests(unittest.TestCase):
    """视频 ↔ 封面照片绑定，以及"按封面搜视频"依赖的映射。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "vc.sqlite3"))
        self.store.upsert_photos(G, [
            {"id": "Pvideo", "lloc": "Pvideo!!", "_album_id": "A1", "owner": "7",
             "uploadtime": 100, "burl": "http://c/pv/200"},
            {"id": "Pphoto", "lloc": "Pphoto!!", "_album_id": "A1", "owner": "7",
             "uploadtime": 200, "burl": "http://c/pp/200"},
        ])

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_video_ids_for_photos(self):
        self.store.upsert_videos(G, [{"video_id": "v1", "album_id": "A1", "lloc": "Pvideo!!"}])
        self.store.link_videos_to_photos(G)
        mapping = self.store.video_ids_for_photos(G, ["Pvideo", "Pphoto", "missing"])
        self.assertEqual(mapping, {"Pvideo": "v1"})
        self.assertEqual(self.store.video_ids_for_photos(G, []), {})

    def test_photos_of_videos(self):
        self.store.upsert_videos(G, [{"video_id": "v1", "album_id": "A1", "lloc": "Pvideo!!"}])
        self.store.link_videos_to_photos(G)
        self.assertEqual(self.store.photos_of_videos(G), ["Pvideo"])
        self.assertEqual(self.store.photos_of_videos(G, album_id="OTHER"), [])

    def test_videos_by_ids(self):
        self.store.upsert_videos(G, [{"video_id": "v1", "album_id": "A1", "lloc": "L",
                                      "video_url": "u", "duration_ms": 5000}])
        got = self.store.videos_by_ids(G, ["v1", "nope"])
        self.assertIn("v1", got)
        self.assertEqual(got["v1"]["duration_ms"], 5000)
        self.assertNotIn("raw_json", got["v1"])
        self.assertEqual(self.store.videos_by_ids(G, []), {})

    def test_videos_list_carries_cover_metadata(self):
        """列表也要能显示发布者/互动数 —— 从封面照片补。"""
        self.store.upsert_photos(G, [
            {"id": "P1", "lloc": "P1!!", "_album_id": "A1", "owner": "42",
             "uploadtime": 1700000000, "burl": "http://c/1/200"},
        ])
        self.store.upsert_videos(G, [{"video_id": "v1", "album_id": "A1", "lloc": "P1!!"}])
        self.store.link_videos_to_photos(G)
        rows = self.store.videos(G)["items"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["comment_count"], 0)
        self.assertEqual(rows[0]["like_count"], 0)


class VideoMediaFlagTests(unittest.TestCase):
    """filter_by_media / annotate_video_covers 的分支（纯函数，直接调用）。"""

    class _Store:
        def __init__(self, mapping):
            self.mapping = mapping

        def video_ids_for_photos(self, group_id, photo_ids):
            return {p: self.mapping[p] for p in photo_ids if p in self.mapping}

        def videos_by_ids(self, group_id, video_ids):
            return {v: {"video_id": v, "file_status": "done", "file_path": "/x.mp4",
                        "duration_ms": 1000} for v in video_ids}

    def _api(self, mapping):
        return types.SimpleNamespace(group_id=G, store=self._Store(mapping))

    def test_videos_only(self):
        api = self._api({"pv": "v1"})
        items = [{"photo_id": "pv"}, {"photo_id": "pp"}]
        kept = filter_by_media(api, items, "videos")
        self.assertEqual([i["photo_id"] for i in kept], ["pv"])
        self.assertTrue(kept[0]["is_video_cover"])
        self.assertEqual(kept[0]["video"]["video_id"], "v1")
        self.assertTrue(kept[0]["video"]["has_file"])

    def test_photos_only(self):
        api = self._api({"pv": "v1"})
        items = [{"photo_id": "pv"}, {"photo_id": "pp"}]
        kept = filter_by_media(api, items, "photos")
        self.assertEqual([i["photo_id"] for i in kept], ["pp"])
        self.assertFalse(kept[0]["is_video_cover"])

    def test_no_filter_keeps_everything(self):
        api = self._api({"pv": "v1"})
        items = [{"photo_id": "pv"}, {"photo_id": "pp"}]
        self.assertEqual(len(filter_by_media(api, items, "")), 2)

    def test_annotate_marks_only_covers(self):
        api = self._api({"pv": "v1"})
        items = [{"photo_id": "pv"}, {"photo_id": "pp"}]
        annotated = annotate_video_covers(api, items)
        self.assertTrue(annotated[0]["is_video_cover"])
        self.assertFalse(annotated[1]["is_video_cover"])
        self.assertNotIn("video", annotated[1])

    def test_no_videos_still_flags_everything_false(self):
        api = self._api({})
        items = [{"photo_id": "pp"}]
        annotate_video_covers(api, items)
        self.assertFalse(items[0]["is_video_cover"])


class VideoUpsertKeepsDataTests(unittest.TestCase):
    """重新采集视频元数据不能把已有字段抹掉。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "vu.sqlite3"))

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_upload_epoch_not_blanked(self):
        self.store.upsert_videos(G, [{"video_id": "v1", "upload_epoch": 1700000000,
                                      "uploader_uin": "42"}])
        # 第二次采集没带 uploadTime → 不能把时间/上传者抹成 NULL
        self.store.upsert_videos(G, [{"video_id": "v1", "duration_ms": 9000}])
        row = self.store.query_one("SELECT * FROM videos WHERE video_id=?", ("v1",))
        self.assertEqual(row["upload_epoch"], 1700000000)
        self.assertEqual(row["uploader_uin"], "42")
        self.assertEqual(row["duration_ms"], 9000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
