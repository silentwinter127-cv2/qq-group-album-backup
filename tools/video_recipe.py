"""Working recipe for group-album VIDEO metadata (proven, read-only).

Findings:
  * The album web API (qun_list_photo_v2) never returns video URLs -- that is why
    photos.is_video is always 0 and all 1,158 gifs; the 2 "video" albums were stored
    as plain images (btype=1) with only a cover thumbnail.
  * The video playback URL IS obtainable, via the bot's own engine (SnowLuma):
        POST http://127.0.0.1:3000/get_group_album_media_list
        Authorization: Bearer <httpAccessToken from QQ Agent data/config.json>
        {"group_id":123456789,"album_id":"<albumId>"}
    Response mediaList items are either image ({image:..}) or video ({video:{id,url,cover,..}}),
    and video.url is a directly downloadable .mp4 (verified HTTP 206, ftypisom).
  * Video URLs are signed (dis_k/dis_t) and expire, so they must be fetched when needed.
"""
import json
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

AGENT = "http://127.0.0.1:3210"
SNOWLUMA = "http://127.0.0.1:3000"
GROUP = 123456789


def agent_token():
    """Ask QQ Agent for the OneBot HTTP token (it is the SnowLuma bearer token)."""
    req = urllib.request.Request(f"{AGENT}/api/onebot-token?which=http")
    with urllib.request.urlopen(req, timeout=20) as r:
        return (json.loads(r.read().decode()) or {}).get("token") or ""


def snowluma(path, payload, token):
    req = urllib.request.Request(
        f"{SNOWLUMA}{path}", data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


token = agent_token()
print("token 长度:", len(token))

albums = snowluma("/get_group_album_list", {"group_id": GROUP}, token)
alist = albums.get("data") if isinstance(albums.get("data"), list) else (
    (albums.get("data") or {}).get("albumList") or [])
print(f"相册 {len(alist)} 个")

total_video = 0
for album in alist:
    aid = album.get("id")
    name = album.get("name")
    try:
        data = snowluma("/get_group_album_media_list",
                        {"group_id": GROUP, "album_id": aid}, token)
    except Exception as exc:
        print(f"  《{name}》失败 {type(exc).__name__}")
        continue
    media = (data.get("data") or {}).get("mediaList") or []
    vids = [m for m in media if isinstance(m, dict) and m.get("video")]
    if vids:
        total_video += len(vids)
        first = vids[0]["video"]
        print(f"  ★《{name}》: 本页 {len(media)} 项，视频 {len(vids)}")
        print(f"      video.id  = {first.get('id')}")
        print(f"      video.url = {str(first.get('url'))[:120]}")
        cover = first.get("cover") or {}
        print(f"      cover.lloc= {str(cover.get('lloc'))[:60]}")
        print(f"      时长/尺寸  = {first.get('time') or first.get('duration')} / "
              f"{first.get('width')}x{first.get('height')}")

print(f"\n本页统计到的视频总数: {total_video}")
print("\n注意：本页只覆盖每个相册的第一页媒体；翻页需要带上 nextAttachInfo。")
