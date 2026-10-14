"""Final verification: search videos through their covers, with uploader + interactions."""
import json
import sys
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
BASE = "http://127.0.0.1:8787"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=300) as r:
        return json.loads(r.read().decode())


def search(**kw):
    params = {"top_k": "20"}
    params.update({k: v for k, v in kw.items() if v})
    return get("/api/ai/search?" + urllib.parse.urlencode(params))


print("健康:", get("/api/health"))

print("\n=== 只看视频：结果里带齐你要的信息 ===")
data = search(q="a black background", media="videos", top_k=5)
print(f"media 回显={data.get('media')!r}  返回 {data['total']} 条")
for i, item in enumerate(data["items"][:4], 1):
    video = item.get("video") or {}
    tags = "、".join(t["name"] for t in (item.get("tags") or [])[:4])
    print(f"  {i}. 相似度 {item['score'] * 100:.1f}%   相册《{item.get('album_title')}》")
    print(f"     发布者 : {item.get('uploader_nick')} (uin {item.get('uploader_uin')})")
    print(f"     时间   : {str(item.get('upload_time'))[:16]}")
    print(f"     互动   : 评论 {item.get('comment_count')} / 点赞 {item.get('like_count')}")
    print(f"     封面描述: {str(item.get('caption'))[:60]}")
    print(f"     封面标签: {tags}")
    print(f"     视频   : id={str(video.get('video_id'))[:30]} 时长={video.get('duration_ms')} "
          f"本地={'有' if video.get('has_file') else '无'}")

print("\n=== 中文查询也能找到视频 ===")
for q in ["黑色背景", "树林", "海报"]:
    d = search(q=q, media="videos", top_k=10)
    print(f"  q={q!r:10} → {d['total']} 个视频")

print("\n=== 组合筛选：只看视频 + 按相册 + 按互动排序 ===")
d = search(q="black background", media="videos", album="摄影集", sort="interactions", top_k=5)
print(f"  《摄影集》+ 互动排序 → {d['total']} 条，sort={d.get('sort')}")

print("\n=== 只下载了视频才给播放地址 ===")
d = search(q="a black background", media="videos", top_k=60)
with_file = [i for i in d["items"] if (i.get("video") or {}).get("has_file")]
print(f"  前 60 个视频封面里，本地已有文件的: {len(with_file)}")

print("\n=== 图片侧不受影响 ===")
d = search(q="a black background", media="photos", top_k=5)
print(f"  media=photos → {d['total']} 条，含视频封面的: "
      f"{sum(1 for i in d['items'] if i.get('is_video_cover'))}（应为 0）")
d = search(q="a black background", top_k=5)
print(f"  media=(全部) → {d['total']} 条，含视频封面的: "
      f"{sum(1 for i in d['items'] if i.get('is_video_cover'))}")
