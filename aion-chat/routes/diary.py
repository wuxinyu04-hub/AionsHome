"""
日记本 API：列表查询、新增、编辑、删除。
"""

import asyncio
import re
import time
import aiosqlite
from fastapi import APIRouter, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from config import DIARY_TTS_CACHE_DIR
from database import get_db
from tts import synthesize_text_to_mp3
from ws import manager

router = APIRouter(prefix="/api/diaries", tags=["diaries"])
DIARY_TTS_MIN_CHARS = 300
DIARY_TTS_MAX_CHARS = 500
DIARY_TTS_CONCURRENCY = 2
_diary_tts_locks: dict[str, asyncio.Lock] = {}


class DiaryCreate(BaseModel):
    title: str = ""
    content: str
    mood: str = ""


class DiaryUpdate(BaseModel):
    title: str = ""
    content: str
    mood: str = ""


def _safe_entry_id(entry_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_\-]", "", entry_id or "")


def _diary_tts_path(entry_id: str):
    safe_id = _safe_entry_id(entry_id)
    if not safe_id:
        return None
    return DIARY_TTS_CACHE_DIR / f"{safe_id}.mp3"


def _diary_tts_url(entry_id: str) -> str:
    return f"/api/diaries/{entry_id}/tts/audio"


def _delete_diary_tts_cache(entry_id: str):
    safe_id = _safe_entry_id(entry_id)
    if not safe_id:
        return
    paths = [
        DIARY_TTS_CACHE_DIR / f"{safe_id}.mp3",
        DIARY_TTS_CACHE_DIR / f"{safe_id}.tmp",
    ]
    paths.extend(DIARY_TTS_CACHE_DIR.glob(f"{safe_id}_s*.mp3"))
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


async def _load_diary(entry_id: str):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT d.id, d.author, d.title, d.content, d.mood, d.source_type, d.source_ref, "
            "d.source_start_ts, d.source_end_ts, d.created_at, "
            "(s.entry_id IS NOT NULL) AS seen "
            "FROM diary_entries d LEFT JOIN diary_seen s ON s.entry_id = d.id WHERE d.id=?",
            (entry_id,),
        )
        return await cur.fetchone()


def _entry_to_dict(row) -> dict:
    """统一 diary_entries 行输出。user 日记视为已读（不进 seen 表，查不到 seen 行时兜底 1）。"""
    entry = dict(row)
    if "seen" not in entry:
        entry["seen"] = 1 if entry.get("author") == "user" else 0
    else:
        entry["seen"] = int(entry["seen"] or 0)
        if entry.get("author") == "user":
            entry["seen"] = 1
    return entry


@router.get("")
async def list_diaries(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    author: str = Query("", max_length=32),
):
    """分页获取日记，author 可选：user / aion / connor。"""
    offset = (page - 1) * page_size
    where = ""
    params: list[object] = []
    if author:
        where = "WHERE d.author=?"
        params.append(author)

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(f"SELECT COUNT(*) as cnt FROM diary_entries d {where}", params)
        total = (await cur.fetchone())["cnt"]
        cur = await db.execute(
            "SELECT d.id, d.author, d.title, d.content, d.mood, d.source_type, d.source_ref, "
            "d.source_start_ts, d.source_end_ts, d.created_at, "
            "(s.entry_id IS NOT NULL) AS seen "
            f"FROM diary_entries d LEFT JOIN diary_seen s ON s.entry_id = d.id "
            f"{where} ORDER BY d.created_at DESC LIMIT ? OFFSET ?",
            [*params, page_size, offset],
        )
        rows = await cur.fetchall()

    return {"items": [_entry_to_dict(r) for r in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/unread")
async def check_diary_unread():
    """统计未读日记条目数 = 未单篇已读的 aion/connor 条数（user 不计）。"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT COUNT(*) as cnt FROM diary_entries d "
            "WHERE d.author IN ('aion','connor') "
            "AND NOT EXISTS (SELECT 1 FROM diary_seen s WHERE s.entry_id = d.id)"
        )
        cnt = (await cur.fetchone())["cnt"]
    return {"unread": cnt}


@router.post("/mark-read")
async def mark_diary_read():
    """批量把所有 aion/connor 日记标记为已读（手动"一键已读"用，前端不再自动调）。"""
    now = time.time()
    async with get_db() as db:
        await db.execute(
            "INSERT OR IGNORE INTO diary_seen (entry_id, seen_at) "
            "SELECT id, ? FROM diary_entries WHERE author IN ('aion','connor')",
            (now,),
        )
        await db.commit()
    return {"ok": True}


@router.post("/{entry_id}/seen")
async def mark_diary_seen(entry_id: str):
    """标记单篇日记已读。幂等，不可逆。user 日记无需标记。"""
    now = time.time()
    async with get_db() as db:
        await db.execute(
            "INSERT OR IGNORE INTO diary_seen (entry_id, seen_at) VALUES (?, ?)",
            (entry_id, now),
        )
        await db.commit()
    return {"ok": True, "seen": True}


@router.post("/{entry_id}/tts")
async def synthesize_diary_tts(entry_id: str):
    """按需为 AI 日记生成独立缓存的 TTS 音频。"""
    row = await _load_diary(entry_id)
    if not row:
        return JSONResponse({"error": "日记不存在"}, status_code=404)

    entry = dict(row)
    author = entry.get("author")
    if author not in ("aion", "connor"):
        return JSONResponse({"error": "用户日记不支持语音合成"}, status_code=403)

    audio_path = _diary_tts_path(entry_id)
    if not audio_path:
        return JSONResponse({"error": "日记 ID 无效"}, status_code=400)
    if audio_path.exists():
        return {"ok": True, "cached": True, "url": _diary_tts_url(entry_id)}

    safe_id = _safe_entry_id(entry_id)
    lock = _diary_tts_locks.setdefault(safe_id, asyncio.Lock())
    async with lock:
        if audio_path.exists():
            return {"ok": True, "cached": True, "url": _diary_tts_url(entry_id)}

        from chatroom import load_chatroom_config

        cfg = load_chatroom_config()
        voice_key = "tts_aion_voice" if author == "aion" else "tts_connor_voice"
        voice = str(cfg.get(voice_key) or "").strip()
        if not voice:
            label = "Aion" if author == "aion" else cfg.get("connor_name", "Connor")
            return JSONResponse({"error": f"未配置 {label} 的 TTS 声线"}, status_code=400)

        title = str(entry.get("title") or "").strip()
        content = str(entry.get("content") or "").strip()
        tts_text = "\n\n".join(part for part in (title, content) if part)
        try:
            result = await synthesize_text_to_mp3(
                tts_text,
                voice,
                audio_path,
                min_chars=DIARY_TTS_MIN_CHARS,
                max_chars=DIARY_TTS_MAX_CHARS,
                concurrency=DIARY_TTS_CONCURRENCY,
                segment_prefix=safe_id,
                cleanup_segments=True,
            )
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:
            try:
                audio_path.unlink(missing_ok=True)
            except OSError:
                pass
            return JSONResponse({"error": f"语音合成失败: {e}"}, status_code=502)

    return {"ok": True, "cached": False, "url": _diary_tts_url(entry_id), **result}


@router.head("/{entry_id}/tts/audio")
@router.get("/{entry_id}/tts/audio")
async def diary_tts_audio(entry_id: str):
    safe_id = _safe_entry_id(entry_id)
    audio_path = _diary_tts_path(entry_id)
    if not safe_id or not audio_path or not audio_path.exists():
        return Response(status_code=404)
    return FileResponse(audio_path, media_type="audio/mpeg", filename=f"{safe_id}.mp3")


@router.post("")
async def create_diary(body: DiaryCreate):
    """用户手动新增一篇日记。"""
    content = body.content.strip()
    if not content:
        return {"error": "内容不能为空"}
    now = time.time()
    entry_id = f"di_user_{int(now * 1000)}"
    entry = {
        "id": entry_id,
        "author": "user",
        "title": body.title.strip(),
        "content": content,
        "mood": body.mood.strip(),
        "source_type": "manual",
        "source_ref": "",
        "source_start_ts": None,
        "source_end_ts": None,
        "created_at": now,
        "seen": 1,
    }
    async with get_db() as db:
        await db.execute(
            "INSERT INTO diary_entries "
            "(id, author, title, content, mood, source_type, source_ref, source_start_ts, source_end_ts, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                entry["id"], entry["author"], entry["title"], entry["content"],
                entry["mood"], entry["source_type"], entry["source_ref"],
                entry["source_start_ts"], entry["source_end_ts"], entry["created_at"],
            ),
        )
        await db.commit()
    await manager.broadcast({"type": "diary_new", "data": entry})
    return entry


@router.put("/{entry_id}")
async def update_diary(entry_id: str, body: DiaryUpdate):
    """编辑一篇日记的标题、正文和心情。编辑不重置已读状态。"""
    content = body.content.strip()
    if not content:
        return {"error": "内容不能为空"}
    async with get_db() as db:
        await db.execute(
            "UPDATE diary_entries SET title=?, content=?, mood=? WHERE id=?",
            (body.title.strip(), content, body.mood.strip(), entry_id),
        )
        await db.commit()
    row = await _load_diary(entry_id)
    if not row:
        return {"error": "日记不存在"}
    entry = _entry_to_dict(row)
    _delete_diary_tts_cache(entry_id)
    await manager.broadcast({"type": "diary_updated", "data": entry})
    return entry


@router.delete("/{entry_id}")
async def delete_diary(entry_id: str):
    """删除一篇日记。同步清理单篇已读记录（SQLite 默认不级联，手动清）。"""
    async with get_db() as db:
        await db.execute("DELETE FROM diary_entries WHERE id=?", (entry_id,))
        await db.execute("DELETE FROM diary_seen WHERE entry_id=?", (entry_id,))
        await db.commit()
    _delete_diary_tts_cache(entry_id)
    return {"ok": True}
