"""音乐播放状态 + 共享曲目记忆（"我们一起听过的歌"）

- now_playing：内存态，前端节流上报；context_builder 读取后注入 AI 上下文，
  让 AI "感知"当前在放什么、能自然评论。
- shared_songs：持久化到 data/shared_songs.json，按 song_id 去重 + play_count，
  对应 GitHub Duetto "记住你们分享过的每一首歌" 的思路。
"""
import json, os, threading, time, logging

log = logging.getLogger(__name__)
_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_SHARED_FILE = os.path.join(_DATA_DIR, "shared_songs.json")
_AI_PLAYLISTS_FILE = os.path.join(_DATA_DIR, "ai_playlists.json")
_SONG_PROFILES_FILE = os.path.join(_DATA_DIR, "song_profiles.json")

_lock = threading.Lock()
_now_lock = threading.Lock()
_now_playing = None  # {song_id, name, artist, state, position, queue_count, updated_at}

_NOW_PLAYING_TTL = 300  # 5 分钟无更新视为已停止

# 用户歌单列表缓存：避免每条 AI 消息都打一次网易云接口
_pl_lock = threading.Lock()
_playlists_cache = None  # {"uid":..., "playlists":[...], "ts":...}
_PLAYLIST_TTL = 600  # 10 分钟


def set_playlists_cache(uid: int, playlists: list):
    """路由拉到歌单后刷新缓存"""
    global _playlists_cache
    with _pl_lock:
        _playlists_cache = {"uid": uid, "playlists": playlists, "ts": time.time()}


def get_playlists_cache(uid: int) -> list | None:
    """context_builder 读缓存注入 AI 上下文（无缓存或过期返回 None，不触发网络请求）"""
    with _pl_lock:
        if not _playlists_cache:
            return None
        if _playlists_cache.get("uid") != uid:
            return None
        if time.time() - _playlists_cache.get("ts", 0) > _PLAYLIST_TTL:
            return None
        return list(_playlists_cache.get("playlists") or [])


_last_shared_log_id = None  # 上一首已计入"一起听"的歌，防止同曲重复计数


def set_now_playing(data: dict):
    global _now_playing, _last_shared_log_id
    with _now_lock:
        _now_playing = {
            "song_id": data.get("song_id"),
            "name": data.get("name", ""),
            "artist": data.get("artist", ""),
            "state": data.get("state", "playing"),
            "position": float(data.get("position", 0) or 0),
            "queue_count": int(data.get("queue_count", 0) or 0),
            "updated_at": time.time(),
        }
    # 「只要在听，就是在一起听」：真实播放的每一首都记入共享记忆（换歌时记一次）
    sid = data.get("song_id")
    if sid is not None and data.get("state", "playing") != "paused" and sid != _last_shared_log_id:
        _last_shared_log_id = sid
        try:
            log_shared({"song_id": sid, "name": data.get("name", ""),
                        "artist": data.get("artist", ""), "cover": data.get("cover", "")})
        except Exception as e:
            log.warning("自动记录一起听失败: %s", e)


def clear_now_playing():
    global _now_playing
    with _now_lock:
        _now_playing = None


def get_now_playing() -> dict | None:
    with _now_lock:
        if not _now_playing:
            return None
        if time.time() - _now_playing.get("updated_at", 0) > _NOW_PLAYING_TTL:
            return None
        return dict(_now_playing)


def _load_shared() -> list:
    try:
        if os.path.exists(_SHARED_FILE):
            with open(_SHARED_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
    except Exception as e:
        log.warning("读取 shared_songs 失败: %s", e)
    return []


def _save_shared(songs: list):
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        tmp = _SHARED_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(songs, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _SHARED_FILE)
    except Exception as e:
        log.warning("写入 shared_songs 失败: %s", e)


def log_shared(song: dict):
    """记录一首共享歌曲（去重 + play_count）。song: {song_id/id, name, artist, cover}"""
    if not song:
        return
    sid = song.get("song_id") if "song_id" in song else song.get("id")
    if sid is None:
        return
    try:
        sid = int(sid)
    except (TypeError, ValueError):
        return
    with _lock:
        songs = _load_shared()
        now = time.time()
        for s in songs:
            if s.get("song_id") == sid:
                s["play_count"] = int(s.get("play_count", 1)) + 1
                s["last_played_at"] = now
                if not s.get("cover") and song.get("cover"):
                    s["cover"] = song["cover"]
                if not s.get("name") and song.get("name"):
                    s["name"] = song["name"]
                if not s.get("artist") and song.get("artist"):
                    s["artist"] = song["artist"]
                _save_shared(songs)
                return
        songs.append({
            "song_id": sid,
            "name": song.get("name", ""),
            "artist": song.get("artist", ""),
            "cover": song.get("cover", ""),
            "first_shared_at": now,
            "last_played_at": now,
            "play_count": 1,
        })
        # 保留最近 500 首
        if len(songs) > 500:
            songs = songs[-500:]
        _save_shared(songs)


def get_shared_song(song_id) -> dict | None:
    """按 id 查一首一起听过的歌（play_count / first_shared_at 供档案与评论用）"""
    try:
        sid = int(song_id)
    except (TypeError, ValueError):
        return None
    with _lock:
        for s in _load_shared():
            if s.get("song_id") == sid:
                return dict(s)
    return None


def get_shared(limit: int = 20, exclude_within_seconds: float | None = None) -> list:
    """取最近共享曲目。exclude_within_seconds：排除最近 N 秒内播过的，
    给 AI 注入时做冷却，避免「最近一起听过的歌」变成自我加强的选歌池、反复点同样几首。"""
    with _lock:
        songs = _load_shared()
    if exclude_within_seconds is not None:
        now = time.time()
        songs = [s for s in songs if now - s.get("last_played_at", 0) > exclude_within_seconds]
    songs.sort(key=lambda s: s.get("last_played_at", 0), reverse=True)
    return songs[:limit]


# ── AI 建的歌单登记簿：区分"他给我建的歌单"，保留建歌单/加歌时的留言 ──

_ai_pl_lock = threading.Lock()


def _load_json_list(path: str) -> list:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
    except Exception as e:
        log.warning("读取 %s 失败: %s", os.path.basename(path), e)
    return []


def _save_json(path: str, data):
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        log.warning("写入 %s 失败: %s", os.path.basename(path), e)


def log_ai_playlist(playlist_id, name: str, note: str = ""):
    """登记一个 AI 创建的歌单（note = 他建歌单时说的话）"""
    if playlist_id is None:
        return
    try:
        pid = int(playlist_id)
    except (TypeError, ValueError):
        return
    with _ai_pl_lock:
        pls = _load_json_list(_AI_PLAYLISTS_FILE)
        for p in pls:
            if p.get("playlist_id") == pid:
                if note:
                    p["note"] = note
                if name:
                    p["name"] = name
                _save_json(_AI_PLAYLISTS_FILE, pls)
                return
        pls.append({
            "playlist_id": pid,
            "name": name or "",
            "note": note or "",
            "created_at": time.time(),
            "songs": [],
        })
        _save_json(_AI_PLAYLISTS_FILE, pls)


def log_ai_playlist_song(playlist_id, song: dict, note: str = ""):
    """AI 往自己建的歌单里加歌时记一笔（song: {id, name, artist}，note = 加歌留言）"""
    try:
        pid = int(playlist_id)
        sid = int(song.get("id"))
    except (TypeError, ValueError):
        return
    with _ai_pl_lock:
        pls = _load_json_list(_AI_PLAYLISTS_FILE)
        for p in pls:
            if p.get("playlist_id") != pid:
                continue
            songs = p.setdefault("songs", [])
            for s in songs:
                if s.get("id") == sid:
                    if note:
                        s["note"] = note
                    _save_json(_AI_PLAYLISTS_FILE, pls)
                    return
            songs.append({
                "id": sid,
                "name": song.get("name", ""),
                "artist": song.get("artist", ""),
                "note": note or "",
                "added_at": time.time(),
            })
            _save_json(_AI_PLAYLISTS_FILE, pls)
            return


def remove_ai_playlist_song(playlist_id, song_id):
    """AI 从自己建的歌单里移除歌曲时，同步清登记簿里的那条（含留言）"""
    try:
        pid = int(playlist_id)
        sid = int(song_id)
    except (TypeError, ValueError):
        return
    with _ai_pl_lock:
        pls = _load_json_list(_AI_PLAYLISTS_FILE)
        changed = False
        for p in pls:
            if p.get("playlist_id") != pid:
                continue
            songs = p.get("songs", [])
            n = len(songs)
            p["songs"] = [s for s in songs if s.get("id") != sid]
            if len(p["songs"]) != n:
                changed = True
            break
        if changed:
            _save_json(_AI_PLAYLISTS_FILE, pls)


def get_ai_playlists() -> list:
    """AI 建的歌单登记簿（含每首歌的留言）"""
    with _ai_pl_lock:
        return _load_json_list(_AI_PLAYLISTS_FILE)


def is_ai_playlist(playlist_id) -> bool:
    try:
        pid = int(playlist_id)
    except (TypeError, ValueError):
        return False
    return any(p.get("playlist_id") == pid for p in get_ai_playlists())


# ── 歌曲档案：他对每首歌说过的话（驻场评论按歌缓存，Duetto song profile 思路） ──

_profile_lock = threading.Lock()


def get_song_profile(song_id) -> dict | None:
    try:
        sid = int(song_id)
    except (TypeError, ValueError):
        return None
    with _profile_lock:
        for p in _load_json_list(_SONG_PROFILES_FILE):
            if p.get("song_id") == sid:
                return p
    return None


def save_song_comment(song_id, name: str, artist: str, comment: str):
    """存一条他对这首歌说的话（每首最多留 5 条，新的在后）"""
    if not comment:
        return
    try:
        sid = int(song_id)
    except (TypeError, ValueError):
        return
    with _profile_lock:
        profiles = _load_json_list(_SONG_PROFILES_FILE)
        for p in profiles:
            if p.get("song_id") == sid:
                comments = p.setdefault("comments", [])
                comments.append({"text": comment, "at": time.time()})
                p["comments"] = comments[-5:]
                if name:
                    p["name"] = name
                if artist:
                    p["artist"] = artist
                _save_json(_SONG_PROFILES_FILE, profiles)
                return
        profiles.append({
            "song_id": sid, "name": name or "", "artist": artist or "",
            "comments": [{"text": comment, "at": time.time()}],
        })
        # 最多留 300 首的档案
        if len(profiles) > 300:
            profiles = profiles[-300:]
        _save_json(_SONG_PROFILES_FILE, profiles)


def get_latest_song_comment(song_id) -> dict | None:
    """这首歌最近一条他说过的话 {text, at}"""
    p = get_song_profile(song_id)
    if not p:
        return None
    comments = p.get("comments") or []
    return dict(comments[-1]) if comments else None


# ── 歌词内存缓存：前端拉歌词时回填，context_builder 据播放位置取"正唱到哪句"（零网络） ──

_lyrics_lock = threading.Lock()
_lyrics_cache = {}  # song_id -> [{t, text}]
_LYRICS_CACHE_MAX = 50


def set_lyrics_cache(song_id, synced: list):
    try:
        sid = int(song_id)
    except (TypeError, ValueError):
        return
    if not isinstance(synced, list):
        return
    with _lyrics_lock:
        _lyrics_cache[sid] = synced
        while len(_lyrics_cache) > _LYRICS_CACHE_MAX:
            _lyrics_cache.pop(next(iter(_lyrics_cache)))


def get_current_lyric(song_id, position: float) -> str:
    """按播放位置取当前歌词行（缓存未命中返回空，不触发网络）"""
    try:
        sid = int(song_id)
    except (TypeError, ValueError):
        return ""
    with _lyrics_lock:
        synced = _lyrics_cache.get(sid)
    if not synced:
        return ""
    cur = ""
    for line in synced:
        if line.get("t", 0) <= (position or 0) + 0.2:
            if line.get("text"):
                cur = line["text"]
        else:
            break
    return cur
