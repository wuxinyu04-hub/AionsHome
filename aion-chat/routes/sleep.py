"""哄睡 app 路由：库 / 合成 / 状态 / 音频 / 进度。

业务模块在 bedtime.py（避免与标准库 sleep 歧义）。路由前缀 /api/sleep。
完全独立于主聊天，不涉及记忆库/系统/日程/摄像头。
"""

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from config import get_key

import bedtime

router = APIRouter(prefix="/api/sleep", tags=["sleep"])
logger = logging.getLogger("sleep_routes")


class SynthesizeIn(BaseModel):
    voice: str = ""


class ProgressIn(BaseModel):
    progress_sec: int = 0


@router.get("/library")
async def library(category: str = ""):
    """列出条目（按分类可选）。预置库首次访问时自动同步进库。"""
    return {"items": await bedtime.list_items(category)}


@router.post("/{item_id}/synthesize")
async def synthesize(item_id: str, body: SynthesizeIn):
    """触发后台合成。voice = Fish Audio reference_id（克隆声）。
    首次合成需 1-2 分钟，前端轮询 /status 或听 WS sleep_item_updated。"""
    item = await bedtime.get_item_raw(item_id)
    if not item:
        raise HTTPException(404, "条目不存在")
    if item.get("status") == "synthesizing":
        return {"ok": True, "status": "synthesizing"}

    script_text = item.get("script_text") or ""
    if not script_text:
        raise HTTPException(400, "脚本为空")

    voice = (body.voice or item.get("voice") or "").strip()
    if not voice:
        raise HTTPException(400, "未选择音色（reference_id），请在播放器里选一个 Fish Audio 克隆声")
    if not get_key("fishaudio"):
        raise HTTPException(400, "未配置 Fish Audio API Key，请到设置页填写")

    # bedtime 内部强制按 fishaudio 合成（不受全局 provider 影响），voice 用 reference_id
    bedtime.trigger_synthesize(item_id, script_text, voice)
    return {"ok": True, "status": "synthesizing"}


@router.get("/{item_id}/status")
async def status(item_id: str):
    item = await bedtime.get_item_raw(item_id)
    if not item:
        raise HTTPException(404, "条目不存在")
    return bedtime.to_public(item)


@router.get("/{item_id}/audio")
async def audio(item_id: str):
    """返回合并后的 mp3。FileResponse 默认支持 Range，前端可拖拽进度。"""
    item = await bedtime.get_item_raw(item_id)
    if not item or not item.get("audio_path"):
        raise HTTPException(404, "音频尚未合成")
    path = bedtime.audio_abs_path(item["audio_path"])
    if not path.exists():
        raise HTTPException(404, "音频文件丢失，请重新合成")
    return FileResponse(str(path), media_type="audio/mpeg")


@router.put("/{item_id}/progress")
async def set_progress(item_id: str, body: ProgressIn):
    await bedtime.update_progress(item_id, body.progress_sec)
    return {"ok": True}


@router.get("/{item_id}/progress")
async def get_progress(item_id: str):
    item = await bedtime.get_item_raw(item_id)
    if not item:
        raise HTTPException(404, "条目不存在")
    return {"progress_sec": item.get("progress_sec", 0)}


@router.get("/{item_id}/script")
async def get_script(item_id: str):
    """返回剧本文本（播放页一句一句浮现用）。"""
    item = await bedtime.get_item_raw(item_id)
    if not item:
        raise HTTPException(404, "条目不存在")
    return {"script_text": item.get("script_text") or "", "title": item.get("title") or ""}


class GenerateIn(BaseModel):
    category: str = "boyfriend"
    prompt: str = ""
    voice: str = ""
    title: str = ""
    book_id: str = ""       # 讲书模式：共读书库的书（books 表）
    chapter_index: int = -1  # <0 = 用 books.current_chapter（同步共读进度）


@router.post("/generate")
async def generate(body: GenerateIn):
    """梗概/书章 -> AI 生成温暖剧本 -> 自动合成。后台异步，前端轮询 status（generating->synthesizing->ready）。

    讲书模式带 book_id 时：从共读书库取真实章节原文当底本，默认接着当前阅读进度那一章念。
    """
    prompt = body.prompt.strip()
    book = None
    if body.book_id:
        book = await bedtime.load_book_chapter(body.book_id, body.chapter_index)
        if not book:
            raise HTTPException(404, "书或章节不存在")
        if not book.get("text"):
            raise HTTPException(400, "这一章没有可读的文本")
    # 冥想可以不输入（自动"整体放松"），其余模式没书就必须有梗概
    if not prompt and not book and body.category != "meditation":
        raise HTTPException(400, "请输入梗概")
    voice = body.voice.strip()
    if not voice:
        raise HTTPException(400, "未选择音色（reference_id）")
    if not get_key("fishaudio"):
        raise HTTPException(400, "未配置 Fish Audio API Key")
    if body.title.strip():
        title = body.title.strip()
    elif book:
        title = f"{book['book_title']} · {book['ch_title']}"
    elif prompt:
        title = prompt[:20] + "…"
    else:
        title = "晚安冥想"
    book_ref = ""
    if book:
        import json as _json
        book_ref = _json.dumps({"book_id": body.book_id, "chapter": book["ch_index"]}, ensure_ascii=False)
    item_id = await bedtime.create_generated_item(body.category, title, voice, book_ref)
    bedtime.trigger_generate(item_id, body.category, prompt, voice, title, book)
    return {"id": item_id, "status": "generating", "title": title}


class TitleIn(BaseModel):
    title: str


@router.put("/{item_id}/title")
async def rename(item_id: str, body: TitleIn):
    """自定义故事名。"""
    title = body.title.strip()
    if not title:
        raise HTTPException(400, "名字不能为空")
    if not await bedtime.get_item_raw(item_id):
        raise HTTPException(404, "条目不存在")
    await bedtime.rename_item(item_id, title[:60])
    return {"ok": True, "title": title[:60]}


@router.post("/{item_id}/played")
async def played(item_id: str):
    """播放计数 +1（前端开播时上报）。"""
    return {"ok": True, "play_count": await bedtime.increment_play(item_id)}


class CoverIn(BaseModel):
    prompt: str = ""


@router.post("/{item_id}/cover")
async def gen_cover(item_id: str, body: CoverIn):
    """AI 生成封面（Gemini 生图，约 10-30s，同步等待返回）。prompt 可选补充描述。"""
    if not await bedtime.get_item_raw(item_id):
        raise HTTPException(404, "条目不存在")
    cover = await bedtime.generate_cover(item_id, body.prompt)
    if not cover:
        raise HTTPException(500, "生成封面失败，稍后再试")
    return {"ok": True}


@router.get("/{item_id}/cover")
async def cover(item_id: str):
    item = await bedtime.get_item_raw(item_id)
    if not item or not item.get("cover_path"):
        raise HTTPException(404, "还没有封面")
    path = bedtime.DATA_DIR / item["cover_path"]
    if not path.exists():
        raise HTTPException(404, "封面文件丢失")
    return FileResponse(str(path))


@router.get("/noise")
async def noise_list():
    """白噪音素材列表：data/sleep_noise/ 下的音频文件，用户自己放，放了就有。"""
    return {"files": bedtime.list_noise_files()}


@router.get("/noise/{name}")
async def noise_file(name: str):
    if ".." in name or "/" in name or "\\" in name:
        raise HTTPException(400, "非法文件名")
    path = bedtime.NOISE_DIR / name
    if not path.exists():
        raise HTTPException(404, "素材不存在")
    return FileResponse(str(path))


@router.get("/voices")
async def voices():
    """哄睡专用音色列表：强制 Fish Audio 克隆声，与全局 TTS provider 解耦。

    bedtime 合成强制 fishaudio（见 synthesize 路由），音色列表也必须始终走
    Fish Audio，否则用户全局没切到 fishaudio 时下拉空白、无法选声、无法生成。
    复用设置页的 _list_fishaudio_voices（精选男声 + 用户克隆声）。
    """
    from routes.settings import _list_fishaudio_voices
    key = get_key("fishaudio")
    if not key:
        return {"voices": [], "error": "未配置 Fish Audio API Key，请到设置页填写"}
    result = await _list_fishaudio_voices(key)
    return {"voices": result.get("voices", [])}
