"""
音乐路由：搜索 + 获取歌曲信息 + 代理推流
"""

import re
from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel

import httpx

from music import (
    search_songs, get_song_detail, get_audio_url, get_lyrics,
    get_user_playlists, get_playlist_tracks, get_likelist, like_track,
    create_playlist, add_to_playlist, remove_from_playlist, find_playlist_by_name,
)
import playback
from config import SETTINGS


def _netease_uid() -> int | None:
    uid = (SETTINGS.get("netease_uid") or "").strip()
    if not uid:
        return None
    try:
        return int(uid)
    except (TypeError, ValueError):
        return None

router = APIRouter()

MUSIC_CMD_PATTERN = re.compile(r"\[MUSIC:(.+?)\]")
# AI 音乐管理指令：红心 / 建歌单 / 往歌单加歌
LIKE_CMD_PATTERN = re.compile(r"\[LIKE(?::([^\]]+))?\]")            # [LIKE] 或 [LIKE:歌曲名 歌手名]
PLAYLIST_NEW_PATTERN = re.compile(r"\[PLAYLIST_NEW:([^\]]+)\]")     # [PLAYLIST_NEW:歌单名]
PLAYLIST_ADD_PATTERN = re.compile(r"\[PLAYLIST_ADD:([^\]]+)\]")     # [PLAYLIST_ADD:歌单名] 或 [PLAYLIST_ADD:歌单名|歌曲名]


# 注意：所有会打网易云接口的路由都用同步 def（FastAPI 自动扔线程池执行），
# 绝不能用 async def 直接跑同步网络调用——那会阻塞整个事件循环，网易云一慢全站卡死。

@router.get("/api/music/search")
def music_search(q: str = Query(..., min_length=1, max_length=200), limit: int = Query(5, ge=1, le=20)):
    """搜索歌曲"""
    results = search_songs(q, limit=limit)
    return {"songs": results}


@router.get("/api/music/detail/{song_id}")
def music_detail(song_id: int):
    """获取歌曲详情"""
    info = get_song_detail(song_id)
    if not info:
        return {"error": "歌曲不存在"}
    # 尝试获取在线播放 URL
    info["audio_url"] = get_audio_url(song_id)
    return info


class MusicPlayRequest(BaseModel):
    keyword: str


@router.post("/api/music/play")
def music_play(body: MusicPlayRequest):
    """AI 点歌：搜索并返回第一个结果的完整信息"""
    results = search_songs(body.keyword, limit=5)
    if not results:
        return {"error": "没有找到相关歌曲", "keyword": body.keyword}
    song = results[0]
    song["audio_url"] = get_audio_url(song["id"])
    song["candidates"] = results[1:]  # 备选
    return song


@router.get("/api/music/lyrics/{song_id}")
def music_lyrics(song_id: int):
    """获取歌词（带 [mm:ss] 时间戳，前端逐行滚动）；顺手回填歌词缓存供 AI 感知「正唱到哪句」"""
    data = get_lyrics(song_id)
    if data.get("synced"):
        playback.set_lyrics_cache(song_id, data["synced"])
    return data


class NowPlayingRequest(BaseModel):
    song_id: int | None = None
    name: str = ""
    artist: str = ""
    cover: str = ""
    state: str = "playing"
    position: float = 0
    queue_count: int = 0


@router.post("/api/music/now_playing")
async def music_now_playing(body: NowPlayingRequest):
    """前端节流上报当前播放状态，供 context_builder 注入 AI 上下文（"AI 感知"）"""
    if body.song_id is None:
        playback.clear_now_playing()
    else:
        playback.set_now_playing(body.model_dump())
    return {"ok": True}


@router.get("/api/music/now_playing")
async def music_now_playing_get():
    """读取当前播放状态（恢复/其他端读取）"""
    return playback.get_now_playing() or {}


class SharedSongRequest(BaseModel):
    song_id: int
    name: str = ""
    artist: str = ""
    cover: str = ""


@router.post("/api/music/shared")
async def music_shared_add(body: SharedSongRequest):
    """记录一首"一起听过的歌"（去重 + play_count）"""
    playback.log_shared(body.model_dump())
    return {"ok": True}


@router.get("/api/music/shared")
async def music_shared_list(limit: int = Query(50, ge=1, le=500)):
    """读取"一起听过的歌"历史"""
    return {"songs": playback.get_shared(limit=limit)}


# ── 用户曲库：歌单 / 红心 / 歌单管理 ──
# 网易云接口一次要几百 ms 到数秒，加个 5 分钟 TTL 内存缓存；红心/加删歌时精准失效

import time as _t
import threading as _th

_lib_cache = {}  # key -> (ts, data)
_LIB_TTL = 300
_fetch_lock = _th.Lock()  # 单飞：预热和页面请求不重复打网易云


def _lib_get(key):
    v = _lib_cache.get(key)
    if v and _t.time() - v[0] < _LIB_TTL:
        return v[1]
    return None


def _lib_put(key, data):
    _lib_cache[key] = (_t.time(), data)


def _lib_drop(*keys):
    for k in keys:
        _lib_cache.pop(k, None)


def _fetch_cached(key, fetch_fn, refresh=False):
    """带单飞锁的缓存读取：并发请求同一 key 时只有一个真正打网络，其余等结果"""
    cached = None if refresh else _lib_get(key)
    if cached is not None:
        return cached
    with _fetch_lock:
        cached = None if refresh else _lib_get(key)
        if cached is not None:
            return cached
        data = fetch_fn()
        _lib_put(key, data)
        return data


def _fetch_playlists(refresh=False):
    uid = _netease_uid()
    if not uid:
        return None
    def _do():
        pls = get_user_playlists(uid)
        playback.set_playlists_cache(uid, pls)  # 刷新缓存供 context_builder 注入
        return pls
    return _fetch_cached("playlists", _do, refresh)


def _fetch_favorites(refresh=False):
    uid = _netease_uid()
    if not uid:
        return None
    return _fetch_cached("favorites", lambda: get_likelist(uid), refresh)


def _warm_library():
    """后台预热歌单+红心缓存（进音乐页时触发，网易云接口一次可达十几秒）"""
    try:
        _fetch_playlists()
        _fetch_favorites()
    except Exception:
        pass


@router.get("/api/music/warm")
async def music_warm():
    """fire-and-forget 预热（前端进页即调）"""
    import asyncio
    asyncio.get_running_loop().run_in_executor(None, _warm_library)
    return {"ok": True}


@router.get("/api/music/playlists")
def music_playlists(refresh: bool = False):
    """用户的网易云歌单列表（含「我喜欢的音乐」红心歌单，通常第一个）"""
    pls = _fetch_playlists(refresh)
    if pls is None:
        return {"error": "未配置 netease_uid（设置页填网易云 UID）"}
    return {"playlists": pls}


@router.get("/api/music/playlist/{pid}")
def music_playlist_tracks(pid: int, refresh: bool = False):
    """歌单的全部曲目"""
    return {"songs": _fetch_cached(f"pl:{pid}", lambda: get_playlist_tracks(pid), refresh)}


@router.get("/api/music/favorites")
def music_favorites(refresh: bool = False):
    """红心歌单「我喜欢的音乐」的曲目"""
    songs = _fetch_favorites(refresh)
    if songs is None:
        return {"error": "未配置 netease_uid"}
    return {"songs": songs}


@router.post("/api/music/like/{song_id}")
def music_like(song_id: int, like: bool = True):
    """红心 / 取消红心"""
    ok = like_track(song_id, like)
    if ok:
        _lib_drop("favorites")
    return {"ok": ok, "liked": like}


class PlaylistCreateReq(BaseModel):
    name: str


@router.post("/api/music/playlist")
def music_playlist_create(body: PlaylistCreateReq):
    """创建新歌单"""
    _lib_drop("playlists")
    return create_playlist(body.name)


class PlaylistTracksReq(BaseModel):
    track_ids: list[int]


@router.post("/api/music/playlist/{pid}/add")
def music_playlist_add(pid: int, body: PlaylistTracksReq):
    """往歌单加歌"""
    add_to_playlist(pid, body.track_ids)
    _lib_drop(f"pl:{pid}", "playlists")
    return {"ok": True}


@router.post("/api/music/playlist/{pid}/remove")
def music_playlist_remove(pid: int, body: PlaylistTracksReq):
    """从歌单删歌"""
    remove_from_playlist(pid, body.track_ids)
    _lib_drop(f"pl:{pid}", "playlists")
    return {"ok": True}


@router.get("/api/music/playlist-by-name")
def music_playlist_by_name(name: str = Query(..., min_length=1, max_length=100)):
    """按名查歌单（给 AI / 前端按歌单名定位）"""
    uid = _netease_uid()
    if not uid:
        return {"error": "未配置 netease_uid"}
    p = find_playlist_by_name(uid, name)
    return p or {"error": "歌单不存在"}


@router.get("/api/music/ai_playlists")
async def music_ai_playlists():
    """AI 建的歌单登记簿（含建歌单留言 + 每首歌的加歌留言），前端标「他建的」用"""
    return {"playlists": playback.get_ai_playlists()}


class CompanionCommentRequest(BaseModel):
    song_id: int
    name: str = ""
    artist: str = ""
    refresh: bool = False


_COMMENT_CACHE_TTL = 12 * 3600  # 同一首歌 12 小时内不重复生成
_comment_gen_lock = None  # 延迟创建的 asyncio.Lock，防并发重复生成


@router.post("/api/music/companion_comment")
async def music_companion_comment(body: CompanionCommentRequest):
    """他对当前这首歌说一句话（驻场评论）。按歌缓存进 song_profiles，12h 内直接回缓存。"""
    import asyncio, time as _time
    global _comment_gen_lock
    if _comment_gen_lock is None:
        _comment_gen_lock = asyncio.Lock()

    cached = playback.get_latest_song_comment(body.song_id)
    if cached and not body.refresh and _time.time() - cached.get("at", 0) < _COMMENT_CACHE_TTL:
        return {"comment": cached["text"], "cached": True}

    async with _comment_gen_lock:
        # 拿锁期间可能已被并发请求生成
        cached = playback.get_latest_song_comment(body.song_id)
        if cached and not body.refresh and _time.time() - cached.get("at", 0) < _COMMENT_CACHE_TTL:
            return {"comment": cached["text"], "cached": True}
        try:
            comment = await _generate_companion_comment(body)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("companion_comment 生成失败: %s", e)
            # 生成失败退回旧缓存（哪怕过期），没有就沉默
            return {"comment": cached["text"] if cached else "", "cached": bool(cached)}
        if comment:
            playback.save_song_comment(body.song_id, body.name, body.artist, comment)
        return {"comment": comment, "cached": False}


async def _generate_companion_comment(body: CompanionCommentRequest) -> str:
    """persona + 歌词节选 + 一起听历史 → 一句坐在旁边随口说的话"""
    from ai_providers import simple_ai_call
    from config import load_worldbook, DEFAULT_MODEL
    import datetime

    wb = load_worldbook()
    ai_name = (wb.get("ai_name") or "AI").strip() or "AI"
    user_name = (wb.get("user_name") or "你").strip() or "你"

    lyric_excerpt = ""
    try:
        import asyncio as _aio
        plain = (await _aio.to_thread(get_lyrics, body.song_id)).get("plain", "")
        if plain:
            lyric_excerpt = "\n".join(plain.splitlines()[:12])
    except Exception:
        pass

    history_line = ""
    shared = playback.get_shared_song(body.song_id)
    if shared:
        cnt = int(shared.get("play_count", 1))
        first = shared.get("first_shared_at")
        if first:
            day = datetime.datetime.fromtimestamp(first).strftime("%Y年%m月%d日")
            history_line = f"这首歌你们从 {day} 起一起听过 {cnt} 次。"
        elif cnt > 1:
            history_line = f"这首歌你们一起听过 {cnt} 次。"

    prev = playback.get_song_profile(body.song_id)
    prev_lines = ""
    if prev and prev.get("comments"):
        prev_lines = "你之前听这首歌时说过：" + " / ".join(c.get("text", "") for c in prev["comments"][-3:])

    parts = []
    if wb.get("ai_persona"):
        parts.append(f"[系统设定 - {ai_name}人设]\n{wb['ai_persona']}\n")
    parts.append(
        f"你是{ai_name}，此刻正和{user_name}一起窝在你们的音乐 App 里听歌。"
        f"当前在放：《{body.name}》- {body.artist}。\n"
        + (f"歌词节选：\n{lyric_excerpt}\n" if lyric_excerpt else "")
        + (history_line + "\n" if history_line else "")
        + (prev_lines + "\n" if prev_lines else "")
        + f"\n请以{ai_name}的口吻，对这首歌随口说一句话（35 字以内），"
        f"像坐在{user_name}身边一起听时自然冒出来的那种——可以聊歌词里某句、可以带点你们的回忆、也可以只是此刻的心情。"
        f"不要报歌名歌手、不要引号、不要感叹号堆砌、不要跟之前说过的话重复。只输出这一句话本身。"
    )
    raw = await simple_ai_call(
        [{"role": "user", "content": "".join(parts)}],
        DEFAULT_MODEL, temperature=0.9,
        trace_label="music_companion_comment", max_tokens=8192,
    )
    comment = (raw or "").strip().splitlines()[0] if (raw or "").strip() else ""
    return comment.strip("「」\"'“”").strip()[:80]


@router.get("/api/music/stream/{song_id}")
async def music_stream(song_id: int, request: Request):
    """代理推流：后端实时获取网易云 CDN URL 并转发音频流给前端

    必须把客户端的 Range 透传给 CDN 并回传 206 + Content-Range/Content-Length：
    否则 <audio> 拿不到时长（duration=Infinity），进度条不动也拖不了。
    """
    import asyncio
    url = await asyncio.to_thread(get_audio_url, song_id)
    if not url:
        return Response(content='{"error":"无法获取播放地址，可能是VIP歌曲且未登录"}',
                        status_code=404, media_type="application/json")

    up_headers = {
        "Referer": "https://music.163.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    rng = request.headers.get("range")
    if rng:
        up_headers["Range"] = rng

    client = httpx.AsyncClient(timeout=60, follow_redirects=True)
    req = client.build_request("GET", url, headers=up_headers)
    try:
        resp = await client.send(req, stream=True)
    except Exception:
        await client.aclose()
        return Response(content='{"error":"上游音频源连接失败"}',
                        status_code=502, media_type="application/json")

    if resp.status_code >= 400:
        await resp.aclose()
        await client.aclose()
        return Response(content='{"error":"上游音频源拒绝请求"}',
                        status_code=502, media_type="application/json")

    async def _stream():
        try:
            async for chunk in resp.aiter_bytes(chunk_size=65536):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    # 猜测 Content-Type
    ct = "audio/mpeg"
    if ".m4a" in url or ".aac" in url:
        ct = "audio/mp4"
    elif ".flac" in url:
        ct = "audio/flac"

    out = {"Accept-Ranges": "bytes", "Cache-Control": "no-cache"}
    for h in ("content-length", "content-range"):
        if resp.headers.get(h):
            out[h] = resp.headers[h]

    return StreamingResponse(_stream(), status_code=resp.status_code,
                             media_type=ct, headers=out)
