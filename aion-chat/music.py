"""
网易云音乐集成：pyncm 搜索 + 获取歌曲信息
支持 MUSIC_U Cookie 登录（VIP 可播放付费歌曲），未配置时退回匿名登录
会话每 2 小时自动刷新，获取音频失败时自动重试一次
"""

import logging, threading, time, re
from pyncm.apis.login import LoginViaAnonymousAccount, LoginViaCookie
from pyncm.apis.cloudsearch import GetSearchResult
from pyncm.apis.track import GetTrackDetail, GetTrackAudio, GetTrackLyrics, SetLikeTrack
from pyncm.apis.user import GetUserPlaylists
from pyncm.apis.playlist import SetCreatePlaylist, SetManipulatePlaylistTracks, GetPlaylistAllTracks, GetPlaylistInfo

log = logging.getLogger(__name__)

_init_lock = threading.Lock()
_inited = False
_last_login_time = 0.0
_SESSION_TTL = 2 * 3600  # 会话有效期：2小时


def _apply_session_timeout(seconds: float = 15.0):
    """pyncm 的 requests session 默认无超时，网络一抖调用线程会无限挂死——打上默认超时补丁"""
    try:
        from pyncm import GetCurrentSession
        sess = GetCurrentSession()
        if getattr(sess, "_aion_timeout_patched", False):
            return
        orig = sess.request
        def _req(*a, **kw):
            kw.setdefault("timeout", seconds)
            return orig(*a, **kw)
        sess.request = _req
        sess._aion_timeout_patched = True
    except Exception:
        pass


def _ensure_login():
    """确保已登录且会话未过期（优先 MUSIC_U Cookie，否则匿名）"""
    global _inited, _last_login_time
    now = time.time()
    if _inited and (now - _last_login_time < _SESSION_TTL):
        return
    with _init_lock:
        now = time.time()
        if _inited and (now - _last_login_time < _SESSION_TTL):
            return
        try:
            from config import SETTINGS
            _apply_session_timeout()
            music_u = SETTINGS.get("netease_music_u", "").strip()
            if music_u:
                LoginViaCookie(MUSIC_U=music_u)
                _inited = True
                _last_login_time = now
                log.info("pyncm MUSIC_U Cookie 登录成功（VIP）")
            else:
                LoginViaAnonymousAccount()
                _inited = True
                _last_login_time = now
                log.info("pyncm 匿名登录成功（未配置 MUSIC_U）")
            _apply_session_timeout()
        except Exception as e:
            log.error("pyncm 登录失败: %s", e)
            raise


def _force_relogin():
    """强制重新登录（会话可能已失效）"""
    global _inited, _last_login_time
    with _init_lock:
        _inited = False
        _last_login_time = 0
    _ensure_login()


def reload_login():
    """重新登录（settings 更新 MUSIC_U 后调用）"""
    _force_relogin()


def search_songs(keyword: str, limit: int = 5) -> list[dict]:
    """搜索歌曲，返回精简结果列表"""
    _ensure_login()
    resp = GetSearchResult(keyword, limit=limit)
    songs = resp.get("result", {}).get("songs", [])
    results = []
    for s in songs:
        artists = [a["name"] for a in s.get("ar", [])]
        album_info = s.get("al", {})
        results.append({
            "id": s["id"],
            "name": s["name"],
            "artists": artists,
            "artist": " / ".join(artists),
            "album": album_info.get("name", ""),
            "cover": (album_info.get("picUrl") or "") + "?param=200y200",
            "duration": s.get("dt", 0),  # 毫秒
        })
    return results


def get_song_detail(song_id: int) -> dict | None:
    """获取单曲详情"""
    _ensure_login()
    resp = GetTrackDetail([song_id])
    songs = resp.get("songs", [])
    if not songs:
        return None
    s = songs[0]
    artists = [a["name"] for a in s.get("ar", [])]
    album_info = s.get("al", {})
    return {
        "id": s["id"],
        "name": s["name"],
        "artists": artists,
        "artist": " / ".join(artists),
        "album": album_info.get("name", ""),
        "cover": (album_info.get("picUrl") or "") + "?param=200y200",
        "duration": s.get("dt", 0),
    }


def get_audio_url(song_id: int) -> str | None:
    """尝试获取播放 URL，失败时自动重新登录重试一次"""
    _ensure_login()
    resp = GetTrackAudio([song_id])
    for d in resp.get("data", []):
        url = d.get("url")
        if url:
            return url
    # 可能会话过期，强制重新登录后重试
    log.info("get_audio_url(%s) 返回空，尝试重新登录重试", song_id)
    _force_relogin()
    resp = GetTrackAudio([song_id])
    for d in resp.get("data", []):
        url = d.get("url")
        if url:
            return url
    return None


_LRC_TS = re.compile(r"\[(\d{1,2}):(\d{1,2})(?:[.:](\d{1,3}))?\]")

def get_lyrics(song_id: int) -> dict:
    """获取歌词。返回 {lrc, plain, synced}：lrc 原始带时间戳；plain 纯文本；synced 为排序后的 [{t, text}]"""
    _ensure_login()
    try:
        resp = GetTrackLyrics(str(song_id))
    except Exception as e:
        log.warning("get_lyrics(%s) 失败: %s", song_id, e)
        return {"lrc": "", "plain": "", "synced": []}
    lrc = (resp.get("lrc") or {}).get("lyric") or ""
    # 解析时间戳行（一首歌可能一行多时间戳）
    synced = []
    for line in lrc.splitlines():
        matches = list(_LRC_TS.finditer(line))
        if not matches:
            continue
        text = _LRC_TS.sub("", line).strip()
        for m in matches:
            mm = int(m.group(1)); ss = int(m.group(2))
            frac = m.group(3)
            ms = 0
            if frac:
                f = frac.ljust(3, "0")[:3]
                ms = int(f)
            t = mm * 60 + ss + ms / 1000.0
            synced.append({"t": round(t, 3), "text": text})
    synced.sort(key=lambda x: x["t"])
    plain = "\n".join(s["text"] for s in synced if s["text"]) if synced else _LRC_TS.sub("", lrc).strip()
    return {"lrc": lrc, "plain": plain, "synced": synced}


# ── 用户曲库：歌单 / 红心 / 歌单管理（均需 MUSIC_U 登录）──

def _track_brief(s: dict) -> dict:
    artists = [a["name"] for a in s.get("ar", []) or []]
    al = s.get("al", {}) or {}
    return {
        "id": s.get("id"),
        "name": s.get("name", ""),
        "artists": artists,
        "artist": " / ".join(artists),
        "album": al.get("name", ""),
        "cover": (al.get("picUrl") or "") + "?param=200y200",
        "duration": s.get("dt", 0),
    }


def get_user_playlists(uid: int) -> list[dict]:
    """获取用户歌单列表。第一个通常是「我喜欢的音乐」红心歌单。"""
    _ensure_login()
    resp = GetUserPlaylists(uid)
    raw = resp.get("playlist") or []
    return [{
        "id": p.get("id"),
        "name": p.get("name", ""),
        "cover": (p.get("coverImgUrl") or "") + "?param=200y200",
        "track_count": p.get("trackCount", 0),
        "play_count": p.get("playCount", 0),
        "description": p.get("description", "") or "",
    } for p in raw]


def get_playlist_tracks(playlist_id: int) -> list[dict]:
    """获取歌单全部曲目"""
    _ensure_login()
    resp = GetPlaylistAllTracks(playlist_id)
    songs = resp.get("songs") or resp.get("tracks") or []
    return [_track_brief(s) for s in songs]


def get_likelist(uid: int) -> list[dict]:
    """红心歌单「我喜欢的音乐」的曲目（取用户歌单第一个）"""
    _ensure_login()
    resp = GetUserPlaylists(uid)
    pls = resp.get("playlist") or []
    if not pls:
        return []
    return get_playlist_tracks(pls[0].get("id"))


def like_track(song_id: int, like: bool = True) -> bool:
    """红心 / 取消红心一首歌"""
    _ensure_login()
    try:
        SetLikeTrack(song_id, like=like)
        return True
    except Exception as e:
        log.error("like_track(%s, %s) 失败: %s", song_id, like, e)
        return False


def create_playlist(name: str) -> dict:
    """创建新歌单，返回 {id, name}"""
    _ensure_login()
    resp = SetCreatePlaylist(name)
    pid = resp.get("id") or (resp.get("playlist") or {}).get("id")
    return {"id": pid, "name": name}


def add_to_playlist(playlist_id: int, track_ids: list) -> bool:
    """往歌单加歌（track_ids 为歌曲 id 列表）"""
    _ensure_login()
    ids = [int(t) for t in track_ids if t is not None]
    SetManipulatePlaylistTracks(trackIds=ids, playlistId=playlist_id, op="add")
    return True


def remove_from_playlist(playlist_id: int, track_ids: list) -> bool:
    """从歌单删歌"""
    _ensure_login()
    ids = [int(t) for t in track_ids if t is not None]
    SetManipulatePlaylistTracks(trackIds=ids, playlistId=playlist_id, op="del")
    return True


def find_playlist_by_name(uid: int, name: str) -> dict | None:
    """按名查歌单（精确匹配，找不到返回 None）"""
    for p in get_user_playlists(uid):
        if p.get("name") == name:
            return p
    return None


# ── 每日推荐：pyncm 没暴露，用 WeapiCryptoRequest 直接调网易云 weapi ──

def get_daily_recommend(limit: int = 20) -> list[dict]:
    """网易云今日个性化推荐（需 MUSIC_U 登录）。pyncm 无该 API，
    复用其会话与 weapi 加密直接 POST /weapi/v3/discovery/recommend/songs。"""
    _ensure_login()
    try:
        from pyncm.apis import WeapiCryptoRequest

        @WeapiCryptoRequest
        def _daily_recommend(n):
            return "/api/v3/discovery/recommend/songs", {"n": n}

        resp = _daily_recommend(limit)
    except Exception as e:
        log.warning("get_daily_recommend 接口调用失败: %s", e)
        return []
    data = resp.get("data") or {}
    songs = data.get("dailySongs") or data.get("recommend") or []
    if not isinstance(songs, list) or not songs:
        # 有些返回是 {data:{recommend:{songs:[...]}}}（推荐歌单的曲目）
        rec = data.get("recommend")
        if isinstance(rec, dict):
            songs = rec.get("songs") or []
    return [_track_brief(s) for s in songs][:limit]
