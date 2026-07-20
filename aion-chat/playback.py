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


def set_now_playing(data: dict):
    global _now_playing
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
