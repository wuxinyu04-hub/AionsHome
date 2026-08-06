"""哄睡 app 路由：库 / 合成 / 状态 / 音频 / 进度。

业务模块在 bedtime.py（避免与标准库 sleep 歧义）。路由前缀 /api/sleep。
完全独立于主聊天，不涉及记忆库/系统/日程/摄像头。
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from config import get_key, get_tts_provider

import bedtime
import sleep_upload

router = APIRouter(prefix="/api/sleep", tags=["sleep"])
logger = logging.getLogger("sleep_routes")

# 手动上传后台 task 引用：asyncio.create_task 不保留引用会被 GC，任务跑一半消失
_upload_tasks: set = set()

# 封面生成 inflight：同步等待端点，连点会并发烧 Gemini 配额，按 item_id 去重
_cover_inflight: set[str] = set()


def _require_tts_key() -> None:
    """按当前全局 TTS provider 检查对应 API Key；edge 免 key。未配置则抛 400。

    哄睡合成内核（bedtime._synthesize_text_block -> _request_tts_audio）已跟随
    全局 get_tts_provider()，故路由层只查「当前 provider 是否有 key」即可，
    不再硬编码 Fish Audio。"""
    provider = get_tts_provider()
    if provider == "edge":
        return  # Edge TTS 免 key
    if not get_key(provider):
        raise HTTPException(400, f"未配置当前 TTS provider（{provider}）的 API Key，请到设置页填写")


class SynthesizeIn(BaseModel):
    voice: str = ""


class ProgressIn(BaseModel):
    progress_sec: int = 0


class ExportIn(BaseModel):
    book_id: str = ""


class GenerateBookIn(BaseModel):
    book_id: str
    voice: str = ""


@router.get("/library")
async def library(category: str = ""):
    """列出条目（按分类可选）。预置库首次访问时自动同步进库。"""
    return {"items": await bedtime.list_items(category)}


@router.post("/generate-book")
async def generate_book(body: GenerateBookIn):
    """整本书后台串行囤讲书音频，跳过已生成/正在生成章节。"""
    voice = (body.voice or bedtime.default_sleep_voice()).strip()
    if not voice:
        raise HTTPException(400, "未选择音色，请先在哄睡页选择当前 TTS 服务商的声音")
    _require_tts_key()
    task = bedtime.auto_generate_book(body.book_id, voice)
    # 手动触发也不阻塞请求；详细结果写日志，库列表看逐章状态。
    import asyncio
    asyncio.create_task(task)
    return {"ok": True, "status": "queued", "book_id": body.book_id, "voice": voice}


@router.post("/export")
async def export_audio(body: ExportIn):
    """把 ready 音频 copy 到 data/sleep_export，不重命名原缓存。"""
    return await bedtime.export_items(body.book_id)


@router.post("/covers/batch")
async def covers_batch():
    """批量补封面：书库每本无封面书 + 非阅读无封面有音频的故事。后台跑，免费生图优先。"""
    targets = await bedtime.cover_targets()
    if not targets:
        return {"ok": True, "total": 0}
    asyncio.create_task(bedtime.run_cover_batch())
    return {"ok": True, "total": len(targets)}


@router.get("/covers/pending")
async def covers_pending():
    """还需补封面的数量（前端按钮文案用）。"""
    return {"pending": len(await bedtime.cover_targets())}


@router.get("/covers/status")
async def covers_status():
    return bedtime.cover_batch_status()


@router.post("/covers/cancel")
async def covers_cancel():
    await bedtime.cancel_cover_batch()
    return {"ok": True}


@router.post("/{item_id}/synthesize")
async def synthesize(item_id: str, body: SynthesizeIn):
    """触发后台合成。voice 按当前全局 TTS provider 的语义存。
    首次合成需 1-2 分钟，前端轮询 /status 或听 WS sleep_item_updated。"""
    item = await bedtime.get_item_raw(item_id)
    if not item:
        raise HTTPException(404, "条目不存在")
    if item.get("status") in ("synthesizing", "generating"):
        return {"ok": True, "status": item["status"]}

    script_text = item.get("script_text") or ""
    if not script_text:
        raise HTTPException(400, "脚本为空")

    voice = (body.voice or item.get("voice") or "").strip()
    if not voice:
        raise HTTPException(400, "未选择音色，请在播放器里选一个")
    bedtime.remember_sleep_voice(voice)
    _require_tts_key()

    # 原子 claim：双击/并发只有一个翻成功，另一个当作已在合成，避免重复 TTS 烧配额。
    # bedtime 合成内核已 provider-agnostic（_request_tts_audio 走全局 get_tts_provider()），
    # voice 按当前 provider 语义存（Fish Audio=reference_id / Step=预置音色ID / …）
    if not await bedtime.claim_synthesizing(item_id, voice):
        return {"ok": True, "status": "synthesizing"}
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
    scene: str = ""          # ASMR 剧情演绎子场景："" / argument / coldwar / daily


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
        raise HTTPException(400, "未选择音色")
    bedtime.remember_sleep_voice(voice)
    _require_tts_key()
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
    # ASMR 剧情子场景白名单，非法回退 ""（走通用剧情规则）
    scene = body.scene if body.scene in ("", "argument", "coldwar", "daily") else ""
    item_id = await bedtime.create_generated_item(body.category, title, voice, book_ref)
    bedtime.trigger_generate(item_id, body.category, prompt, voice, title, book, scene)
    return {"id": item_id, "status": "generating", "title": title}


class RegenerateIn(BaseModel):
    voice: str = ""


@router.post("/{item_id}/regenerate")
async def regenerate(item_id: str, body: RegenerateIn):
    """剧本为空的失败条目：复用原条目重写剧本再合成（保留 id/标题/分类/音色/book_ref）。

    /synthesize 对空剧本只能 400，故事库里的「点击重试」以前是死路——这里补上。
    不新建条目：避免重试一次多一条重复故事。
    """
    item = await bedtime.get_item_raw(item_id)
    if not item:
        raise HTTPException(404, "条目不存在")
    if item.get("status") in ("synthesizing", "generating"):
        return {"ok": True, "status": item["status"]}

    voice = (body.voice or item.get("voice") or "").strip() or bedtime.default_sleep_voice()
    if not voice:
        raise HTTPException(400, "未选择音色，请在播放器里选一个")
    bedtime.remember_sleep_voice(voice)
    _require_tts_key()

    title = item.get("title") or "今晚的故事"
    category = item.get("category") or "boyfriend"
    # 讲书条目：把原来的书和章节捞回来当底本，否则退化成按标题写
    book = None
    pub = bedtime.to_public(item)
    if pub.get("book_id"):
        book = await bedtime.load_book_chapter(pub["book_id"], pub.get("book_chapter", -1))
    # 原始梗概没有留存，用标题当主题：至少不会完全丢上下文
    prompt = "" if book else title.split(" · ")[-1].rstrip("…")

    if not await bedtime.claim_synthesizing(item_id, voice):
        return {"ok": True, "status": "generating"}
    bedtime.trigger_generate(item_id, category, prompt, voice, title, book)
    return {"ok": True, "status": "generating", "title": title}


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
    if item_id in _cover_inflight:
        return {"ok": True, "status": "in_progress"}
    if not await bedtime.get_item_raw(item_id):
        raise HTTPException(404, "条目不存在")
    _cover_inflight.add(item_id)
    try:
        cover = await bedtime.generate_cover(item_id, body.prompt)
        if not cover:
            raise HTTPException(500, "生成封面失败，稍后再试")
        return {"ok": True}
    finally:
        _cover_inflight.discard(item_id)


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


@router.delete("/{item_id}")
async def delete_item(item_id: str):
    """删除条目（含音频和封面文件）。"""
    ok = await bedtime.delete_item(item_id)
    if not ok:
        raise HTTPException(404, "条目不存在")
    return {"ok": True}


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
    """哄睡音色列表：跟随设置页全局 TTS provider（不再强制 Fish Audio）。

    bedtime 合成内核（_synthesize_text_block -> _request_tts_audio）已 provider-agnostic，
    音色列表也跟随全局 provider：设置页切 step，哄睡页出 Step 音色；切 fishaudio 就出
    Fish Audio 克隆声。后面接新 TTS 只要 tts.py + settings.py 加分支，哄睡零改动自动支持。
    直接复用设置页的 tts_voice_list（已含全 provider 分发，返回带 provider 字段）。
    """
    from routes.settings import tts_voice_list
    return await tts_voice_list()


@router.get("/{item_id}/netease-status")
async def netease_status(item_id: str):
    """查询一条的网易云上传状态（播放页按钮展示）：uploaded / song_id / 歌单名 / 手动运行态。"""
    row = await bedtime.get_item_raw(item_id) or {}
    return sleep_upload.get_netease_status(item_id, row.get("category", ""))


@router.post("/{item_id}/upload-netease")
async def upload_netease(item_id: str):
    """手动触发一条上传网易云。后台线程跑（10s~5min），立即返回，前端轮询 status。"""
    row = await bedtime.get_item_raw(item_id) or {}
    title = row.get("title", "")
    category = row.get("category", "")
    st = sleep_upload.get_netease_status(item_id, category)
    if st.get("uploaded"):
        return {"queued": False, "uploaded": True,
                "song_id": st["song_id"], "playlist_name": st["playlist_name"]}
    if st.get("running"):
        return {"queued": True, "running": True}
    task = asyncio.create_task(
        asyncio.to_thread(sleep_upload.start_manual_upload, item_id, title, category))
    _upload_tasks.add(task)
    task.add_done_callback(_upload_tasks.discard)
    return {"queued": True, "running": True}
