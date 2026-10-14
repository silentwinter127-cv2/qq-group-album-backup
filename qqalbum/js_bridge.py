"""Browser-side JavaScript bridge for the QZone group-album endpoints.

The JS below is injected into a logged-in h5.qzone.qq.com page. It reuses the
page's cookies and computes g_tk exactly like QZone's own front-end does, so all
requests are same-origin and correctly signed.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------
# JavaScript bridge (kept as one blob so it can be injected with a single call)
# --------------------------------------------------------------------------
JS_BRIDGE = r"""
window.QQAlbum = (function () {
  const PROXY = 'https://h5.qzone.qq.com/proxy/domain/';
  const ENDPOINTS = {
    albumList: PROXY + 'u.photo.qzone.qq.com/cgi-bin/upp/qun_list_album_v2',
    photoList: PROXY + 'u.photo.qzone.qq.com/cgi-bin/upp/qun_list_photo_v2',
    commentList: PROXY + 'u.photo.qzone.qq.com/cgi-bin/upp/qun_list_photocmt_v2',
    memberInfo: PROXY + 'u.photo.qzone.qq.com/cgi-bin/upp/qun_list_member_info',
    albumCapacity: PROXY + 'u.photo.qzone.qq.com/cgi-bin/upp/qun_album_capacity_v2',
    feedList: PROXY + 'r.qzone.qq.com/cgi-bin/user/group_feeds_get.cgi',
    local: 'https://h5.qzone.qq.com/groupphoto/index',
    localPost: 'https://h5.qzone.qq.com/groupphoto/inqq'
  };

  function cookieValue(name) {
    const m = document.cookie.match(new RegExp('(?:^|;\\s*)' + name + '=([^;]*)'));
    return m ? m[1] : '';
  }

  function tk() {
    const uin = (cookieValue('uin') || cookieValue('p_uin') || '').replace(/^o/, '');
    const skey = cookieValue('p_skey') || cookieValue('skey') || '';
    let h = 5381;
    for (let i = 0; i < skey.length; i++) h += (h << 5) + skey.charCodeAt(i);
    return {g_tk: (h & 0x7fffffff), uin: uin};
  }

  function qzonetoken() {
    const qs = new URLSearchParams(location.search);
    return window.g_qzonetoken || qs.get('qzonetoken') || cookieValue('qzonetoken') || '';
  }

  function decodeEntities(s) {
    if (typeof s !== 'string' || s.indexOf('&') < 0) return s;
    return s.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
            .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&#x27;/g, "'")
            .replace(/&nbsp;/g, ' ');
  }

  function unwrap(text) {
    const trimmed = text.trim();
    const m = trimmed.match(/^[A-Za-z_$][\w$.]*\((.*)\)\s*;?$/s);
    return m ? m[1] : trimmed;
  }

  async function request(url, params, method) {
    const t = tk();
    const all = Object.assign({}, params, {g_tk: t.g_tk, uin: t.uin});
    const init = {credentials: 'include', method: method || 'GET'};
    let target = url;
    if (method === 'POST') {
      init.headers = {'Content-Type': 'application/x-www-form-urlencoded'};
      init.body = new URLSearchParams(all).toString();
    } else {
      target = url + (url.indexOf('?') >= 0 ? '&' : '?') + new URLSearchParams(all).toString();
    }
    const res = await fetch(target, init);
    const text = await res.text();
    let body = null;
    try { body = JSON.parse(unwrap(text)); } catch (e) { body = null; }
    return {status: res.status, json: body, raw: body ? null : text.slice(0, 800)};
  }

  return {
    tk: tk,
    decodeEntities: decodeEntities,
    endpoints: ENDPOINTS,
    request: request,
    qzonetoken: qzonetoken,

    /* ---- fresh feed fetch ---------------------------------------------- */
    /* The global QQAlbum object is replaced on every page load, so the raw
       `request` closure above dies with the old page context. The crawler uses
       this self-contained entry point to always run against the live page. */
    fresh: async function (kind, gid, attach, num) {
      if (kind === 'feeds') {
        const common = {uin: tk().uin, platform: 'qzone', inCharset: 'utf-8',
                        outCharset: 'utf-8', source: 'qzone', cmd: 'qunGetFeeds',
                        refresh_type: 2, relation_type: 0, fupdate: 1,
                        qunid: gid, attach_info: attach || ''};
        const attempt = async (url, method) => {
          const out = await request(url, Object.assign({}, common, {format: 'json',
                                                                    getcnt: num || 20}), method);
          if (out.status === 500 || out.raw) {
            const out2 = await request(url, Object.assign({}, common, {format: 'jsonp',
                                                                      getcnt: num || 20}), method);
            if (out2.json) return out2;
          }
          return out;
        };
        let out = await attempt(ENDPOINTS.feedList, 'GET');
        if (!out.json || (out.json && out.json.code !== 0)) {
          const alt = await request('https://h5.qzone.qq.com/proxy/domain/r.qzone.qq.com/'
                                    + 'cgi-bin/user/group_feeds_get.cgi',
                                    Object.assign({}, common, {format: 'jsonp', getcnt: num || 20}),
                                    'POST');
          if (alt.json) out = alt;
        }
        return {status: out.status, code: out.json && out.json.code,
                message: out.json && out.json.message, json: out.json, raw: out.raw};
      }
      return {status: 0, raw: 'unsupported kind'};
    },

    /* ---- albums -------------------------------------------------------- */
    getAlbums: async function (gid, start, num) {
      const common = {qunId: gid, qunid: gid, cmd: 'qunGetAlbumList', format: 'json',
                      platform: 'qzone', inCharset: 'utf-8', outCharset: 'utf-8',
                      source: 'qzone', getMemberRole: 0};
      const out = await request(ENDPOINTS.albumList,
        Object.assign({}, common, {start: start || 0, num: num || 100, attach_info: ''}), 'GET');
      const data = (out.json && out.json.data) || {};
      return {status: out.status, code: out.json && out.json.code, message: out.json && out.json.message,
              album: data.album || [], total: data.total, attach_info: decodeEntities(data.attach_info || ''),
              userinfo: data.userinfo || null, raw: out.raw};
    },

    /* ---- photos -------------------------------------------------------- */
    getPhotos: async function (gid, albumId, start, num) {
      const params = {qunId: gid, qunid: gid, albumId: albumId, albumid: albumId,
                      cmd: 'qunGetPhotoList', format: 'json', platform: 'qzone',
                      inCharset: 'utf-8', outCharset: 'utf-8', source: 'qzone',
                      getCommentCnt: 1, getMemberRole: 0, getalbum: 0,
                      start: start || 0, num: num || 500, attach_info: ''};
      const out = await request(ENDPOINTS.photoList, params, 'GET');
      const data = (out.json && out.json.data) || {};
      return {status: out.status, code: out.json && out.json.code, message: out.json && out.json.message,
              photos: data.photos || data.photolist || [], total: data.total,
              memberRole: data.memberRole, raw: out.raw};
    },

    /* ---- comments of one photo ---------------------------------------- */
    getPhotoComments: async function (gid, albumId, photoId, start, num) {
      const params = {qunId: gid, qunid: gid, albumId: albumId, albumid: albumId,
                      photoId: photoId, lloc: photoId, tid: photoId,
                      cmd: 'qunGetPhotoComment', format: 'json', platform: 'qzone',
                      inCharset: 'utf-8', outCharset: 'utf-8', source: 'qzone',
                      start: start || 0, num: num || 50};
      const out = await request(ENDPOINTS.commentList, params, 'GET');
      return {status: out.status, code: out.json && out.json.code, message: out.json && out.json.message,
              json: out.json, raw: out.raw};
    },

    /* ---- dynamic feeds (comments + likes + uploader) ------------------- */
    getFeedsRaw: async function (gid, attachInfo, getcnt) {
      const params = {qunid: gid, cmd: 'qunGetFeeds', format: 'json', platform: 'qzone',
                      inCharset: 'utf-8', outCharset: 'utf-8', source: 'qzone',
                      refresh_type: 2, relation_type: 0, fupdate: 1,
                      getcnt: getcnt || 20, attach_info: attachInfo || ''};
      const out = await request(ENDPOINTS.feedList, params, 'GET');
      return {status: out.status, code: out.json && out.json.code, message: out.json && out.json.message,
              json: out.json, raw: out.raw};
    },

    /* ---- members ------------------------------------------------------- */
    getMembers: async function (gid) {
      const params = {groupid: gid, qunId: gid, uin: tk().uin, format: 'json',
                      platform: 'qzone', inCharset: 'utf-8', outCharset: 'utf-8',
                      source: 'qzone', cmd: 'getGroupMember'};
      const out = await request(ENDPOINTS.local, params, 'GET');
      return {status: out.status, code: out.json && out.json.code, json: out.json, raw: out.raw};
    },

    getMemberInfo: async function (gid, uins) {
      const params = {qunId: gid, groupId: gid, uin: (uins || []).join(','), format: 'json',
                      platform: 'qzone', inCharset: 'utf-8', outCharset: 'utf-8', source: 'qzone'};
      const out = await request(ENDPOINTS.memberInfo, params, 'GET');
      return {status: out.status, code: out.json && out.json.code, json: out.json, raw: out.raw};
    },

    getServerTime: async function (gid) {
      const out = await request(ENDPOINTS.local, {groupid: gid, qunId: gid, cmd: 'getSvrTime',
                                                 format: 'json', platform: 'qzone'}, 'GET');
      return {status: out.status, json: out.json, raw: out.raw};
    },

    /* ---- likes --------------------------------------------------------- */
    like: async function (payload) {
      const out = await request(PROXY + 'w.qzone.qq.com/cgi-bin/likes/internal_dolike_app',
                                payload, 'GET');
      return {status: out.status, json: out.json, raw: out.raw};
    },
    unlike: async function (payload) {
      const out = await request(PROXY + 'w.qzone.qq.com/cgi-bin/likes/internal_unlike_app',
                                payload, 'GET');
      return {status: out.status, json: out.json, raw: out.raw};
    }
  };
})();
'bridge-ready';
"""


def js_bridge() -> str:
    return JS_BRIDGE


def js_call(name: str, *args: Any) -> str:
    """Build an expression that awaits window.QQAlbum.<name>(...)."""
    payload = ", ".join(json.dumps(a, ensure_ascii=False) for a in args)
    return f"window.QQAlbum.{name}({payload})"


def parse_attach_info(attach: str) -> Dict[str, Any]:
    """Parse QZone attach_info strings such as 'offset=5&total=5179&basetime=1'."""
    out: Dict[str, Any] = {}
    if not attach:
        return out
    for part in attach.replace("&amp;", "&").split("&"):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip()
        value = value.strip()
        if value.lstrip("-").isdigit():
            out[key] = int(value)
        elif value.lower() in ("true", "false"):
            out[key] = value.lower() == "true"
        else:
            out[key] = value
    return out


def build_attach_info(values: Dict[str, Any]) -> str:
    allowed = ("offset", "total", "basetime", "hasmore", "flag", "refresh_type")
    return "&".join(f"{k}={values[k]}" for k in allowed if k in values)


def build_attach_info_like(original: Dict[str, Any], offset: int) -> str:
    out = dict(original)
    out["offset"] = offset
    return build_attach_info(out)


def flatten_feed(singlefeed: List[Any]) -> Dict[str, Any]:
    """Convert one raw `singlefeed` array into a readable dict (mirrors QZone's adapter)."""
    def at(i: int) -> Any:
        return singlefeed[i] if isinstance(singlefeed, list) and i < len(singlefeed) else None

    head = at(0) or {}
    author = at(1) or {}
    subject = at(2) or {}
    media = at(5) or {}
    comment_block = at(10) or {}
    like_block = at(11) or {}
    extra = at(18) or {}

    comments: List[Dict[str, Any]] = []
    for c in (comment_block.get("commments") or []):
        replies = [{
            "id": str(r.get("replyid") or ""),
            "content": r.get("content") or "",
            "time": int(r.get("date") or 0),
            "uin": (r.get("user") or {}).get("uin") or "",
            "nick": (r.get("user") or {}).get("nickname") or "",
        } for r in (c.get("replys") or [])]
        comments.append({
            "id": str(c.get("commentid") or ""),
            "content": c.get("content") or "",
            "time": int(c.get("date") or 0),
            "reply_total": int(c.get("replynum") or 0),
            "uin": (c.get("user") or {}).get("uin") or "",
            "nick": (c.get("user") or {}).get("nickname") or "",
            "replies": replies,
        })

    likes = [{
        "uin": (l.get("user") or {}).get("uin") or "",
        "nick": (l.get("user") or {}).get("nickname") or "",
    } for l in (like_block.get("likemans") or [])]

    pics: List[Dict[str, Any]] = []
    for p in (media.get("picdata") or []):
        urls = p.get("photourl") or {}
        best = urls.get("1") or urls.get("11") or urls.get("2") or urls.get("3") or {}
        video = p.get("videodata") or {}
        pics.append({
            "pic_key": p.get("lloc") or "",
            "url": best.get("url") or "",
            "width": int(best.get("width") or 0),
            "height": int(best.get("height") or 0),
            "focus_x": int(best.get("focus_x") or 0),
            "focus_y": int(best.get("focus_y") or 0),
            "videoflag": p.get("videoflag") or 0,
            "videostatus": video.get("videostatus") or 0,
            "videourl": video.get("videourl") or "",
        })

    other = {}
    if subject:
        other = {
            "subid": subject.get("subid"),
            "cellid": subject.get("cellid"),
            "orglikekey": subject.get("orglikekey") or head.get("orglikekey") or "",
        }

    return {
        "fid": str(other.get("subid") or head.get("subid") or ""),
        "org_like_key": other.get("orglikekey") or "",
        "time": int(head.get("time") or 0),
        "user": {"uin": author.get("user", {}).get("uin") or author.get("uin") or "",
                 "nick": author.get("user", {}).get("nickname") or ""},
        "comment_total": int(comment_block.get("num") or 0),
        "comments": comments,
        "like_total": int(like_block.get("num") or 0),
        "is_liked": int(like_block.get("isliked") or 0),
        "likes": likes,
        "album": {
            "album_id": media.get("albumid") or subject.get("cellid") or "",
            "album_name": media.get("albumname") or "",
            "album_desc": media.get("desc") or "",
            "photo_num": int(media.get("uploadnum") or 0),
        },
        "pics": pics,
        "busi_param": extra.get("busi_param") or {},
        "summary": (at(4) or {}).get("summary") or "",
    }


def batch_id_of(feed: Dict[str, Any]) -> str:
    """The upload batch id, used to link a photo back to its feed (comments/likes)."""
    bp = feed.get("busi_param") or {}
    if isinstance(bp, dict) and bp.get("21"):
        return str(bp["21"])
    return str(feed.get("fid") or "")


def batch_id_from_photo(photo: Dict[str, Any]) -> str:
    return str(photo.get("batchid") or "")
