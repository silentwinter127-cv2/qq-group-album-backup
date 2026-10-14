"""Command line entry point for the QQ group album backup tool."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from typing import Any, Dict, Optional

from .caption import CaptionEngine, CaptionUnavailable
from .crawler import BackupManager
from .qzone import DEFAULT_PROFILE, QzoneClient
from .siglip import SiglipEngine, SiglipUnavailable
from .store import DEFAULT_DB, Store
from .web import Api, find_free_port, serve


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qqalbum",
        description="把 QQ 群相册的元数据（上传者/上传时间/评论/点赞）备份到本地 SQLite，"
                    "并下载原图，附带本地 WebUI 浏览与分类。")
    parser.add_argument("group", nargs="?", default=None, help="QQ 群号，例如 <群号>")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite 数据库路径（默认 {DEFAULT_DB}）")
    parser.add_argument("--root", default=None, help="图片与缓存根目录（默认与数据库同目录）")
    parser.add_argument("--profile", default=DEFAULT_PROFILE, help="浏览器用户数据目录（保存登录态）")
    parser.add_argument("--port", type=int, default=8787, help="WebUI 端口（默认 8787，自动避让占用）")
    parser.add_argument("--host", default="127.0.0.1", help="WebUI 监听地址")
    parser.add_argument("--browser", default=None, help="Edge/Chrome 可执行文件路径")
    parser.add_argument("--headless", action="store_true", help="无头模式（登录后可用）")
    parser.add_argument("--workers", type=int, default=4, help="下载并发数（默认 4）")
    parser.add_argument("--page-size", type=int, default=300, help="照片分页大小")
    parser.add_argument("--no-open", action="store_true", help="不要自动打开浏览器")
    parser.add_argument("--no-web", action="store_true", help="只跑命令行，不启动 WebUI")
    parser.add_argument("--sync", action="store_true", help="启动后立即抓取一次")
    parser.add_argument("--download", action="store_true", help="启动后立即下载原图")
    parser.add_argument("--full", action="store_true", help="抓取时不用增量（重新全量）")
    parser.add_argument("--status", action="store_true", help="打印数据库统计后退出")
    parser.add_argument("--groups", action="store_true", help="列出本地数据库里已备份的群")
    parser.add_argument("--siglip-dir", default=None,
                        help="本地 SigLIP2 模型目录（默认自动探测 D:\\siglip2-base-patch16-224）")
    parser.add_argument("--ai-index", action="store_true", help="启动后立即建立 AI 向量索引与标签")
    parser.add_argument("--ai-limit", type=int, default=0, help="本次 AI 索引最多处理多少张（0=全部）")
    parser.add_argument("--ai-retag", action="store_true", help="重新打标（覆盖已有 AI 标签）")
    parser.add_argument("--caption-dir", default=None,
                        help="本地 BLIP 描述模型目录（默认自动探测 D:\\blip-image-captioning-base）")
    parser.add_argument("--caption", action="store_true", help="启动后立即生成图片描述（BLIP）")
    parser.add_argument("--caption-limit", type=int, default=0, help="本次最多生成多少条描述（0=全部）")
    parser.add_argument("--recaption", action="store_true", help="重新生成描述（覆盖已有）")
    parser.add_argument("--videos", action="store_true",
                        help="抓取时一并抓视频元数据（需 QQ Agent 的 SnowLuma 引擎在运行）")
    parser.add_argument("--download-videos", action="store_true", help="启动后立即下载未完成的视频")
    parser.add_argument("--video-limit", type=int, default=0, help="本次最多下载多少个视频（0=全部）")
    return parser


def _force_utf8_console() -> None:
    """Windows consoles often default to GBK, which mangles Chinese log lines."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: Optional[list] = None) -> int:
    _force_utf8_console()
    args = build_parser().parse_args(argv)
    db_path = os.path.abspath(args.db)
    root_dir = os.path.abspath(args.root or os.path.dirname(db_path) or ".")
    store = Store(db_path)

    if args.status or args.groups:
        rows = store.query("SELECT group_id, album_count, photo_count, member_count, last_sync_at FROM groups "
                           "ORDER BY last_sync_at DESC")
        if not rows:
            print("数据库里还没有备份记录。")
            return 0
        for row in rows:
            stats = store.stats(row["group_id"])
            print(f"群 {row['group_id']}  相册 {stats['albums']}  照片 {stats['photos']}  "
                  f"已下载 {stats['photos_downloaded']} ({stats['downloaded_bytes'] / 1048576:.1f} MB)  "
                  f"评论 {stats['comments']}  点赞 {stats['likes']}  上次同步 {row['last_sync_at']}")
            print(f"  数据库: {db_path}")
            print(f"  图片目录: {os.path.join(root_dir, 'photos', row['group_id'])}")
        return 0

    if not args.group:
        print("请提供群号，例如： python -m qqalbum <群号>", file=sys.stderr)
        print("也可以先运行 --groups 查看已有备份。", file=sys.stderr)
        return 2

    group_id = str(args.group)

    def client_factory() -> QzoneClient:
        return QzoneClient(group_id, profile_dir=args.profile, browser_path=args.browser,
                           headless=args.headless, log=lambda msg: print("[browser]", msg, flush=True))

    manager = BackupManager(group_id, store, client_factory, root_dir=root_dir,
                            page_size=args.page_size, download_workers=args.workers)
    engine = None
    if args.siglip_dir or args.ai_index:
        try:
            engine = SiglipEngine.instance(args.siglip_dir)
        except SiglipUnavailable as exc:
            print(f"[warn] SigLIP2 不可用：{exc}", file=sys.stderr)
    caption_engine = None
    if args.caption_dir or args.caption:
        try:
            caption_engine = CaptionEngine.instance(args.caption_dir)
        except CaptionUnavailable as exc:
            print(f"[warn] BLIP 描述模型不可用：{exc}", file=sys.stderr)
    api = Api(store, manager, group_id, root_dir, engine=engine, model_dir=args.siglip_dir,
              caption_engine=caption_engine, caption_dir=args.caption_dir)

    print(f"群号        : {group_id}")
    print(f"数据库      : {db_path}")
    print(f"图片目录    : {os.path.join(root_dir, 'photos', group_id)}")
    print(f"浏览器配置  : {args.profile}")

    if args.sync or args.download:
        try:
            status = api.connect()
            session = status.get("session") or {}
            print(f"浏览器会话  : uin={session.get('uin')} logged_in={session.get('logged_in')}")
        except Exception as exc:
            print(f"[warn] 自动连接浏览器失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        if args.sync:
            api.start_sync({"incremental": not args.full, "videos": bool(args.videos)})
        if args.download:
            api.start_download({})

    if args.download_videos:
        # 视频走机器人引擎，不需要浏览器；单独提示一下引擎状态，免得"没反应"
        try:
            status = api.video_status()
            if status.get("engine_ok"):
                print(f"视频引擎    : {status.get('engine')}")
            else:
                print(f"[warn] 视频引擎不可用：{status.get('error')}", file=sys.stderr)
        except Exception as exc:
            print(f"[warn] 检查视频引擎失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        res = api.start_video_download({"limit": args.video_limit})
        print(f"下载视频    : {'已启动' if res['started'] else res['reason']}")

    if args.ai_index:
        res = api.start_ai_index({"limit": args.ai_limit, "retag": args.ai_retag})
        print(f"AI 索引     : {'已启动' if res['started'] else res['reason']}")

    if args.caption:
        res = api.start_caption({"limit": args.caption_limit, "recaption": args.recaption})
        print(f"图片描述    : {'已启动' if res['started'] else res['reason']}")

    if args.no_web:
        if not (args.sync or args.download or args.ai_index or args.caption or args.download_videos):
            print("--no-web 需要配合 --sync / --download / --ai-index / --caption / --download-videos 使用。",
                  file=sys.stderr)
            return 2

        def busy() -> bool:
            return (manager.state.running
                    or (api.indexer.is_running() if api.indexer else False)
                    or (api.captioner.is_running() if api.captioner else False))

        while busy():
            snap = manager.state.snapshot()
            ai = api.indexer.state.snapshot() if api.indexer else None
            cap = api.captioner.state.snapshot() if api.captioner else None
            line = f"\r{snap['stage']} {snap['current']}/{snap['total']} {snap['message'][:40]}"
            if ai:
                line += f" | tag {ai['stage']} {ai['current']}/{ai['total']}"
            if cap:
                line += f" | caption {cap['stage']} {cap['current']}/{cap['total']} {cap['rate']}/s"
            print(line, end="", flush=True)
            time.sleep(1)
        print()
        print(json.dumps(store.stats(group_id), ensure_ascii=False, indent=2))
        print(json.dumps(store.ai_stats(group_id), ensure_ascii=False, indent=2))
        print("完成。" if not manager.state.error else f"结束：{manager.state.error}")
        return 0

    port = find_free_port(args.port, args.host)
    httpd, url = serve(api, host=args.host, port=port, open_browser=not args.no_open)
    print(f"WebUI       : {url}")
    print("提示        : 首次使用需在弹出的 Edge 窗口里扫码登录 QQ 空间；登录态保存在浏览器配置目录。")
    print("按 Ctrl+C 退出。")

    stop = threading.Event()

    def _shutdown(signum, frame):  # noqa: ANN001
        stop.set()

    try:
        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)
    except (ValueError, AttributeError):
        pass

    try:
        while not stop.is_set():
            stop.wait(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        print("\n正在退出…")
        try:
            httpd.shutdown()
        except Exception:
            pass
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
