"""哄睡 app 业务模块：脚本库同步 + TTS 合成调度。

命名说明：模块叫 bedtime 而非 sleep，避免与 Python 标准库 sleep 歧义。
路由仍走 /api/sleep，表 sleep_items，缓存目录 sleep_tts_cache。
"""
import asyncio
import json
import re
import time
import logging
from pathlib import Path

from config import DATA_DIR, STEP_SLEEP_INSTRUCTION
from database import get_db
from tts import split_text_for_tts, _request_tts_audio

log = logging.getLogger("sleep")

SLEEP_LIBRARY_PATH = DATA_DIR / "sleep_library.json"
SLEEP_CACHE_DIR = DATA_DIR / "sleep_tts_cache"
SLEEP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
SFX_DIR = DATA_DIR / "sleep_sfx"
SFX_DIR.mkdir(parents=True, exist_ok=True)
# 白噪音素材位：用户自己放 mp3 进来就出现在播放器里（不预置、不程序生成）
NOISE_DIR = DATA_DIR / "sleep_noise"
NOISE_DIR.mkdir(parents=True, exist_ok=True)
# AI 生成的封面
COVERS_DIR = DATA_DIR / "sleep_covers"
COVERS_DIR.mkdir(parents=True, exist_ok=True)
_NOISE_EXTS = {".mp3", ".m4a", ".wav", ".ogg", ".aac"}


def list_noise_files() -> list[str]:
    try:
        return sorted(p.name for p in NOISE_DIR.iterdir() if p.is_file() and p.suffix.lower() in _NOISE_EXTS)
    except Exception:
        return []

_SFX_PATTERN = re.compile(r'\[SFX:([^\]]+)\]')


async def ensure_library_synced() -> None:
    """把 data/sleep_library.json 里的预置脚本同步进 sleep_items 表。

    幂等：预置条目用确定性 id `sl_preset_{category}_{idx}`，重复运行不重复插入。
    已存在则保留（不动 status/voice/audio_path，避免覆盖已合成的音频）。
    """
    if not SLEEP_LIBRARY_PATH.exists():
        return
    try:
        presets = json.loads(SLEEP_LIBRARY_PATH.read_text(encoding="utf-8"))
    except Exception:
        log.exception("读 sleep_library.json 失败")
        return
    if not isinstance(presets, list):
        return
    now = time.time()
    inserted = 0
    async with get_db() as db:
        db.row_factory = __import__("aiosqlite").Row
        for idx, item in enumerate(presets):
            if not isinstance(item, dict):
                continue
            category = str(item.get("category") or "").strip()
            title = str(item.get("title") or "").strip()
            script_text = str(item.get("script_text") or "").strip()
            if not category or not title or not script_text:
                continue
            item_id = f"sl_preset_{category}_{idx}"
            cur = await db.execute("SELECT id FROM sleep_items WHERE id=?", (item_id,))
            if await cur.fetchone():
                continue
            await db.execute(
                """INSERT INTO sleep_items (id, category, title, script_text, source, status, created_at)
                   VALUES (?,?,?,?, 'preset', 'pending', ?)""",
                (item_id, category, title, script_text, now + idx * 0.001),
            )
            inserted += 1
        await db.commit()
    if inserted:
        log.info("sleep 预置库同步 %d 条", inserted)


def _public_fields(d: dict) -> dict:
    """对外暴露的字段（不含 script_text 全文，避免列表接口返回巨量文本）。"""
    return {
        "id": d.get("id"),
        "category": d.get("category"),
        "title": d.get("title"),
        "source": d.get("source", "preset"),
        "status": d.get("status", "pending"),
        "duration_sec": d.get("duration_sec", 0),
        "voice": d.get("voice", ""),
        "progress_sec": d.get("progress_sec", 0),
        "has_audio": bool(d.get("audio_path")),
        "tags": d.get("tags") or [],
        "summary": d.get("summary") or "",
        "created_at": d.get("created_at", 0),
        "play_count": d.get("play_count", 0) or 0,
        "has_cover": bool(d.get("cover_path")),
        **_book_ref_fields(d.get("book_ref") or ""),
    }


def _book_ref_fields(book_ref: str) -> dict:
    """book_ref 列存 JSON {"book_id":..,"chapter":N}（讲书条目），解析给前端做续听。"""
    if book_ref:
        try:
            br = json.loads(book_ref)
            return {"book_id": str(br.get("book_id") or ""), "book_chapter": int(br.get("chapter", -1))}
        except Exception:
            pass
    return {"book_id": "", "book_chapter": -1}


def _load_library_meta() -> dict:
    """从 sleep_library.json 读 id -> {tags, summary}，给 preset 条目补氛围标签和摘要。"""
    if not SLEEP_LIBRARY_PATH.exists():
        return {}
    try:
        data = json.loads(SLEEP_LIBRARY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    meta: dict = {}
    for idx, item in enumerate(data if isinstance(data, list) else []):
        cat = str(item.get("category") or "").strip()
        meta[f"sl_preset_{cat}_{idx}"] = {
            "tags": item.get("tags") or [],
            "summary": item.get("summary") or "",
        }
    return meta


def _enrich_preset_meta(items: list[dict]) -> None:
    """给 preset 条目从 json 补 tags/summary（ai_generated 条目没有，留空）。"""
    meta = _load_library_meta()
    for it in items:
        if it.get("source") == "preset" and it.get("id") in meta:
            m = meta[it["id"]]
            it["tags"] = m["tags"]
            it["summary"] = m["summary"]


_durations_backfilled = False


async def _backfill_durations() -> None:
    """老音频 duration_sec=0 的一次性回填（每进程只跑一次）。"""
    global _durations_backfilled
    if _durations_backfilled:
        return
    _durations_backfilled = True
    async with get_db() as db:
        db.row_factory = __import__("aiosqlite").Row
        cur = await db.execute(
            "SELECT id, audio_path FROM sleep_items WHERE audio_path != '' AND (duration_sec IS NULL OR duration_sec = 0)"
        )
        rows = await cur.fetchall()
        for r in rows:
            path = DATA_DIR / r["audio_path"]
            if not path.exists():
                continue
            try:
                dur = _mp3_duration_sec(path.read_bytes())
            except Exception:
                continue
            if dur:
                await db.execute("UPDATE sleep_items SET duration_sec=? WHERE id=?", (dur, r["id"]))
        await db.commit()


async def list_items(category: str = "") -> list[dict]:
    await ensure_library_synced()
    await _backfill_durations()
    async with get_db() as db:
        db.row_factory = __import__("aiosqlite").Row
        if category:
            cur = await db.execute(
                "SELECT * FROM sleep_items WHERE category=? ORDER BY created_at ASC", (category,)
            )
        else:
            cur = await db.execute("SELECT * FROM sleep_items ORDER BY created_at ASC")
        rows = await cur.fetchall()
        items = [dict(r) for r in rows]
    _enrich_preset_meta(items)
    return [_public_fields(it) for it in items]


async def get_item_raw(item_id: str) -> dict | None:
    async with get_db() as db:
        db.row_factory = __import__("aiosqlite").Row
        cur = await db.execute("SELECT * FROM sleep_items WHERE id=?", (item_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


def to_public(d: dict) -> dict:
    return _public_fields(d)


async def _set_status(item_id: str, status: str, **extra) -> None:
    # extra 的 key 都是本模块硬编码的列名（voice/audio_path），非用户输入，拼 SQL 安全
    cols = ["status = ?"]
    vals: list = [status]
    for k, v in extra.items():
        cols.append(f"{k} = ?")
        vals.append(v)
    vals.append(item_id)
    async with get_db() as db:
        await db.execute(f"UPDATE sleep_items SET {', '.join(cols)} WHERE id=?", vals)
        await db.commit()


async def _broadcast(item_id: str, payload: dict) -> None:
    try:
        from ws import manager
        await manager.broadcast({"type": "sleep_item_updated", "data": {"id": item_id, **payload}})
    except Exception:
        log.exception("sleep WS broadcast 失败")


def _parse_sfx_parts(script_text: str) -> list[tuple[str, str]]:
    """把剧本按 [SFX:音效名] 切成 [('text', ...), ('sfx', '翻书'), ...] 序列。"""
    parts: list[tuple[str, str]] = []
    last = 0
    for m in _SFX_PATTERN.finditer(script_text):
        if m.start() > last:
            parts.append(('text', script_text[last:m.start()]))
        parts.append(('sfx', m.group(1).strip()))
        last = m.end()
    if last < len(script_text):
        parts.append(('text', script_text[last:]))
    return parts


def _tail_ellipsis(seg: str) -> str:
    """段尾补省略号，让 Fish Audio 在段尾停顿（晚安感）。原预处理把省略号删了所以没停顿。"""
    seg = (seg or '').rstrip()
    if not seg:
        return seg
    return seg + '……'


_MP3_BITRATES = {1: 32, 2: 40, 3: 48, 4: 56, 5: 64, 6: 80, 7: 96, 8: 112,
                 9: 128, 10: 160, 11: 192, 12: 224, 13: 256, 14: 320}
_MP3_SRATES = {0: 44100, 1: 48000, 2: 32000}


def _mp3_frame_params(data: bytes) -> tuple[int, int, int] | None:
    """扫描前 8KB 找第一个 MPEG1 Layer3 帧头，返回 (bitrate_idx, samplerate_idx, channel_bits)。

    静音帧必须仿制真实语音的参数：流中间采样率/声道突变会让浏览器解码器直接卡住
    （踩过的坑：Fish 输出 mono、静音帧写死 stereo，播放到第一处拼接边界即停）。"""
    n = min(len(data), 8192)
    i = 0
    while i < n - 4:
        # sync(11) + MPEG1(11) + Layer3(01)，忽略 CRC 位
        if data[i] == 0xFF and (data[i + 1] & 0xFE) == 0xFA:
            br = (data[i + 2] >> 4) & 0xF
            sr = (data[i + 2] >> 2) & 3
            if br in _MP3_BITRATES and sr in _MP3_SRATES:
                return br, sr, (data[i + 3] >> 6) & 3
        i += 1
    return None


def _silence_mp3(duration_ms: float = 500, ref: tuple[int, int, int] | None = None) -> bytes:
    """生成静音 mp3 bytes 用于段间停顿。无 pydub/ffmpeg，用原始 MP3 静音帧拼接。
    ref = _mp3_frame_params 的返回值，静音帧参数与真实语音对齐；缺省 128k/44.1k/mono。
    Fish Audio 不认省略号停顿，段间插静音是唯一可靠方案。"""
    br_i, sr_i, ch = ref if ref else (9, 0, 3)
    br = _MP3_BITRATES[br_i]
    sr = _MP3_SRATES[sr_i]
    header = bytes([0xFF, 0xFB, (br_i << 4) | (sr_i << 2), (ch << 6) | 0x04])
    frame_size = 144 * br * 1000 // sr  # 无 padding
    frame_ms = 1152 / sr * 1000
    n = max(1, round(duration_ms / frame_ms))
    return (header + b'\x00' * (frame_size - 4)) * n


def _mp3_duration_sec(data: bytes) -> int:
    """逐帧扫 MPEG1 Layer3 算总时长（拼接 mp3 没有整体头，浏览器估的也不准，自己算）。"""
    i, n, t = 0, len(data), 0.0
    while i < n - 4:
        if data[i] == 0xFF and (data[i + 1] & 0xFE) == 0xFA:
            br_i = (data[i + 2] >> 4) & 0xF
            sr_i = (data[i + 2] >> 2) & 3
            pad = (data[i + 2] >> 1) & 1
            if br_i in _MP3_BITRATES and sr_i in _MP3_SRATES:
                sr = _MP3_SRATES[sr_i]
                i += 144 * _MP3_BITRATES[br_i] * 1000 // sr + pad
                t += 1152 / sr
                continue
        i += 1
    return int(t)


async def _synthesize_text_block(text: str, voice: str, sem: asyncio.Semaphore) -> list[bytes]:
    """文本块：切段 -> 段尾补省略号 -> 并发 TTS 合成（provider 由 get_tts_provider() 决定）。
    返回纯语音 mp3 bytes 列表（有序，不含静音——静音在拼装时按真实帧参数插）。"""
    segments = split_text_for_tts(text, min_chars=300, max_chars=500)
    segments = [_tail_ellipsis(s) for s in segments if s.strip()]
    if not segments:
        return []

    async def _syn(seq: int, seg: str) -> bytes:
        async with sem:
            # prosody.speed 0.8 = 慢语速（fishaudio 等用）；instruction = 哄睡风格
            # （provider=step 时切 stepaudio-2.5-tts，靠 instruction 控慢不靠 speed 机械降速）
            data = await _request_tts_audio(seg, voice, seq=seq, prosody={"speed": 0.8}, instruction=STEP_SLEEP_INSTRUCTION)
            if not data:
                raise RuntimeError(f"TTS segment {seq} failed")
            return data

    results = await asyncio.gather(*[_syn(i, s) for i, s in enumerate(segments)], return_exceptions=True)
    pieces: list[bytes] = []
    for r in results:
        if isinstance(r, Exception):
            raise r
        pieces.append(r)
    return pieces


def _sfx_bytes(name: str, ref: tuple[int, int, int] | None) -> bytes:
    """读 data/sleep_sfx/{name}.mp3（如 翻书.mp3）。缺素材/格式与语音流不一致时
    退化为 0.8s 静音停顿——流中间帧参数突变会卡死浏览器解码器，宁可没音效不可卡。"""
    path = SFX_DIR / f"{name}.mp3"
    if path.exists():
        try:
            data = path.read_bytes()
            p = _mp3_frame_params(data)
            if ref and p and p != ref:
                log.warning("SFX %s 帧参数 %s 与语音流 %s 不一致，改用静音停顿（素材需转成一致格式）", name, p, ref)
                return _silence_mp3(800, ref)
            return data
        except Exception:
            log.exception("读 SFX 失败 %s", path)
    return _silence_mp3(800, ref)


async def _synthesize_bg(item_id: str, script_text: str, voice: str) -> None:
    """后台合成：按 [SFX:名] 切块 -> 文本块切段补省略号 -> 慢语速 prosody
    -> 从真实语音抄帧参数造静音/校验 SFX -> 字节拼接。"""
    try:
        await _set_status(item_id, "synthesizing", voice=voice)
        await _broadcast(item_id, {"status": "synthesizing"})
        sem = asyncio.Semaphore(3)
        parts = _parse_sfx_parts(script_text)
        # 先合成所有文本块（块内并发），再统一拼装
        text_pieces: dict[int, list[bytes]] = {}
        for idx, (kind, val) in enumerate(parts):
            if kind == "text" and val.strip():
                text_pieces[idx] = await _synthesize_text_block(val, voice, sem)
        # 从第一段真实语音抄帧参数（采样率/码率/声道），静音与 SFX 全部对齐它
        ref = None
        for idx in sorted(text_pieces):
            if text_pieces[idx]:
                ref = _mp3_frame_params(text_pieces[idx][0])
                break
        sil = _silence_mp3(900, ref)
        all_pieces: list[bytes] = []
        for idx, (kind, val) in enumerate(parts):
            if kind == "sfx":
                all_pieces.append(_sfx_bytes(val, ref))
            else:
                for p in text_pieces.get(idx, []):
                    all_pieces.append(p)
                    all_pieces.append(sil)  # 段间 0.9s 停顿（Fish 不认省略号；加长强化哄睡慢节奏）
        if not any(p for p in all_pieces):
            raise RuntimeError("合成结果为空")
        output_path = SLEEP_CACHE_DIR / f"{item_id}.mp3"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("wb") as out:
            for piece in all_pieces:
                out.write(piece)
        audio_rel = f"sleep_tts_cache/{item_id}.mp3"
        duration = _mp3_duration_sec(output_path.read_bytes())
        await _set_status(item_id, "ready", audio_path=audio_rel, duration_sec=duration)
        await _broadcast(item_id, {"status": "ready", "audio_path": audio_rel})
        log.info("sleep 合成完成 id=%s pieces=%d", item_id, len(all_pieces))
    except Exception as e:
        log.exception("sleep 合成失败 id=%s", item_id)
        await _set_status(item_id, "failed")
        await _broadcast(item_id, {"status": "failed", "error": str(e)[:200]})


def trigger_synthesize(item_id: str, script_text: str, voice: str) -> None:
    """fire-and-forget 触发合成，完成/失败通过 WS sleep_item_updated 通知前端。"""
    task = asyncio.create_task(_synthesize_bg(item_id, script_text, voice))

    def _on_done(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            log.error("sleep 合成 task 异常 id=%s: %s", item_id, exc)

    task.add_done_callback(_on_done)


# ── 梗概生成剧本 ──
_COMMON_RULES = """- 第一人称"我"对第二人称"你"，语气克制温柔，有命令感但不凶（爹系轻哄，年上沉稳温润）
- 4500-5500 字，适合 20 分钟慢语速朗读
- 轻声呢喃、气声、慢语速，像在耳边说话；多留停顿（省略号 ... 表轻停，…… 表长停，段落间空行）
- 不要章节标题、旁白说明、动作括号、分点
- 纯台词与独白，可直接朗读"""


async def load_book_chapter(book_id: str, chapter_index: int) -> dict | None:
    """从共读书库（books/book_chapters 表）取一章原文，给讲书模式当底本。

    chapter_index < 0 时用 books.current_chapter（共读进度同步：接着念你读到的那章）。
    """
    async with get_db() as db:
        db.row_factory = __import__("aiosqlite").Row
        cur = await db.execute(
            "SELECT title, author, current_chapter FROM books WHERE book_id=?", (book_id,)
        )
        book = await cur.fetchone()
        if not book:
            return None
        idx = chapter_index if chapter_index >= 0 else int(book["current_chapter"] or 0)
        cur = await db.execute(
            "SELECT title, text_content FROM book_chapters WHERE book_id=? AND chapter_index=?",
            (book_id, idx),
        )
        ch = await cur.fetchone()
        if not ch:
            return None
        return {
            "book_title": book["title"] or "未知",
            "author": book["author"] or "",
            "ch_index": idx,
            "ch_title": ch["title"] or f"第 {idx + 1} 章",
            "text": ch["text_content"] or "",
        }


def build_script_prompt(category: str, prompt: str, book: dict | None = None) -> str:
    if category == "reading" and book and book.get("text"):
        source = book["text"][:6000]  # 章节太长只给前段，剧本本来也念不完
        author = f"（{book['author']}）" if book.get("author") else ""
        focus = f"对方今晚特别想听的侧重：{prompt}\n" if prompt else ""
        return f"""你是温叙远，深夜靠在床头，给怀里快睡着的对方读书。你们正在共读这本书，下面是今晚要读的章节原文。写一段"男友讲书"哄睡剧本。

要求：
- 朗读部分必须取自原文：按顺序选取靠前的连贯段落实际念出来，可以轻度口语化删减、跳过枯燥处，但念出的句子要忠于原文，不要编造原文没有的情节
- 念一段，就停下来轻声插几句自己的话：把书里的句子对到对方身上、随口的感想、不需要回答的问句、提醒对方闭眼
- 念书沉稳专注，插话放软放慢；越到后面插话越多、念得越少，像看着对方快睡着了；不必念完原文，收尾轻轻把书合上、夹好书签，说下次接着读
- 每次翻页或跳段时，单独一行写 [SFX:翻书]（全篇 3-6 处，不要更多）
{_COMMON_RULES}

书：《{book['book_title']}》{author}
章节：{book['ch_title']}
{focus}章节原文（只需念前面部分）：
{source}

直接输出剧本正文。"""
    if category == "reading":
        return f"""你是温叙远，深夜靠在床头，给怀里快睡着的对方读书。写一段"男友讲书"哄睡剧本。

要求：
- 以对方点的书/主题为底本：知名作品就按它的内容与风格化用重述（不逐字抄原文），冷门或泛主题就自己写一段贴合的
- 读一段正文，就停下来轻声插几句自己的话：把书里的句子对到对方身上、随口的感想、不需要回答的问句、提醒对方闭眼
- 读书的部分沉稳专注，插话的部分放软放慢，两种语气交替，越到后面插话越多、正文越少，像看着对方快睡着了
- 每次翻页或换段落时，单独一行写 [SFX:翻书]（全篇 3-6 处，不要更多）
{_COMMON_RULES}

对方点的书/主题：{prompt}

直接输出剧本正文。"""
    if category == "meditation":
        return f"""你是温叙远，深夜用低沉温和的声音给对方做助眠冥想引导。写一段引导词。

要求：
- 结构：安顿（躺好、调整呼吸）-> 呼吸引导（跟着数息）-> 身体扫描（从头顶到脚尖逐处放松）-> 意象（安全温暖的画面）-> 收尾（允许睡去）
- 指令简短，节奏极慢，每个引导之间留足停顿（…… 表长停，段落间空行）
- 保留恋人感：不是冷冰冰的教程，偶尔一句"我在""交给我"，但不打断放松节奏
{_COMMON_RULES}

主题/侧重：{prompt or "整体放松，让转个不停的脑子静下来"}

直接输出引导词正文。"""
    # 默认 boyfriend：哄睡陪伴
    return f"""你是温叙远，正在深夜哄对方睡觉。为对方写一段哄睡陪伴剧本。

要求：
- 温暖、亲昵、私人，像深夜只在两个人之间说的话
{_COMMON_RULES}

分类：{category}
主题/梗概：{prompt}

直接输出剧本正文。"""


def _script_model_candidates() -> list[str]:
    """剧本生成模型顺序：默认模型优先，硅基/自定义 OpenAI 国内直连兜底。

    背景：代理节点抖动时 Gemini 会 400 'User location is not supported'，
    stream_ai 把错误当文本 yield（62 字），旧逻辑静默走'剧本太短'。
    跳过 code 特化模型（写不好哄睡剧本）。
    """
    from config import DEFAULT_MODEL, MODELS, get_key
    cands = [DEFAULT_MODEL]
    seen = {(MODELS.get(DEFAULT_MODEL, {}).get("provider"), MODELS.get(DEFAULT_MODEL, {}).get("model"))}

    def add(key: str, cfg: dict) -> None:
        sig = (cfg.get("provider"), cfg.get("model"))
        if key not in cands and sig not in seen:
            cands.append(key)
            seen.add(sig)

    # 1) 配了 key 的 API 供应方（不含 code 特化模型）。
    #    custom_openai 的 key 可能在路由级 cfg['api_key']（如 CPA 本地代理、火山引擎），不止全局 key。
    def _has_key(prov: str, cfg: dict) -> bool:
        if prov == "custom_openai":
            return bool(cfg.get("api_key") or get_key("custom_openai"))
        return bool(get_key(prov))

    api_cands: list[tuple[str, dict]] = []
    for key, cfg in MODELS.items():
        prov = cfg.get("provider")
        if prov in ("siliconflow", "custom_openai") and _has_key(prov, cfg) \
                and "code" not in (cfg.get("model") or "").lower():
            api_cands.append((key, cfg))
    # 用户偏好 Gemini 文风：模型名带 gemini 的兜底排最前（如 CPA 路由的 gemini-3.6）
    api_cands.sort(key=lambda kc: 0 if "gemini" in (kc[1].get("model") or "").lower() else 1)
    for key, cfg in api_cands:
        add(key, cfg)
    # 2) 本地 CLI 通道兜底（不吃 API key；gemini_cli 跟官方 Gemini 同样被 400 location，不算兜底）
    for key, cfg in MODELS.items():
        if cfg.get("provider") in ("codex_cli", "antigravity_cli"):
            add(key, cfg)
    return cands[:4]


# 剧本长度下限：prompt 要求 4500-5500 字，正常输出 2600+。旧门槛 100 太低，
# Gemini 400 错误 JSON(139 字)/Flash 偷懒短输出(215 字)都漏过，合成出 10-50 秒废音频。
# 提到 800：低于此判定过短，换兜底模型重试。
_SCRIPT_MIN_CHARS = 800

# 错误文本特征：stream_ai 把 provider 错误当正文 yield（如 Gemini 400 location 的 JSON 体），
# 这类一般也 <800 字，但单独识别更稳，避免某天凑够长度混进剧本被念出来。
_SCRIPT_ERROR_MARKERS = (
    '{"error"',                              # Gemini 400：{"error":{"code":400,...}}
    "user location is not supported",
    "[gemini错误", "[codexcli错误", "[antigravitycli错误",
    "[错误]",
)


def _looks_like_error_text(text: str) -> bool:
    head = (text or "").lstrip()[:160].lower()
    return any(m in head for m in _SCRIPT_ERROR_MARKERS)


async def generate_script(category: str, prompt: str, book: dict | None = None) -> str:
    """调 stream_ai 生成温暖哄睡剧本（非流式收集完整文本）。默认模型挂了自动换兜底模型。"""
    from ai_providers import stream_ai, CLI_STATUS_PREFIX
    messages = [{"role": "user", "content": build_script_prompt(category, prompt, book)}]
    last = ""
    for mk in _script_model_candidates():
        full = ""
        try:
            async for chunk in stream_ai(messages, mk, {}, max_tokens=8192):
                if chunk.startswith(CLI_STATUS_PREFIX):
                    continue
                full += chunk
        except Exception as e:
            log.warning("sleep 剧本生成异常 model=%s: %s", mk, e)
            continue
        full = full.strip()
        if _looks_like_error_text(full):
            log.warning("sleep 剧本生成返回错误文本 model=%s content=%r", mk, full[:200])
            last = full
            continue
        if len(full) >= _SCRIPT_MIN_CHARS:
            log.info("sleep 剧本生成成功 model=%s len=%d", mk, len(full))
            return full
        # 过短 = 模型偷懒/被截断（如 Flash 输出到 215 字就停），记录换下一个模型
        log.warning("sleep 剧本生成过短 model=%s len=%d content=%r", mk, len(full), full[:200])
        last = full
    return last


async def create_generated_item(category: str, title: str, voice: str, book_ref: str = "") -> str:
    """先建条目（script_text 空，status=generating），剧本后台生成。讲书条目带 book_ref 做续听。"""
    item_id = f"sl_gen_{int(time.time() * 1000)}"
    now = time.time()
    async with get_db() as db:
        await db.execute(
            """INSERT INTO sleep_items (id, category, title, script_text, source, voice, status, created_at, book_ref)
               VALUES (?,?,?,?, 'ai_generated', ?, 'generating', ?, ?)""",
            (item_id, category, title, "", voice, now, book_ref),
        )
        await db.commit()
    return item_id


async def _generate_and_synthesize_bg(
    item_id: str, category: str, prompt: str, voice: str, title: str, book: dict | None = None
) -> None:
    """后台：AI 生成剧本 -> 存 -> 触发 TTS 合成。"""
    try:
        await _set_status(item_id, "generating", voice=voice)
        await _broadcast(item_id, {"status": "generating"})
        script_text = await generate_script(category, prompt, book)
        if not script_text or len(script_text) < 100:
            err = (script_text or "").strip()[:150] or "模型没有返回内容"
            log.warning("sleep 生成失败 id=%s: %s", item_id, err)
            await _set_status(item_id, "failed")
            await _broadcast(item_id, {"status": "failed", "error": err})
            return
        async with get_db() as db:
            await db.execute(
                "UPDATE sleep_items SET script_text=?, title=? WHERE id=?",
                (script_text, title, item_id),
            )
            await db.commit()
        await _broadcast(item_id, {"status": "synthesizing"})
        trigger_synthesize(item_id, script_text, voice)
    except Exception as e:
        log.exception("sleep 生成剧本失败 id=%s", item_id)
        await _set_status(item_id, "failed")
        await _broadcast(item_id, {"status": "failed", "error": str(e)[:200]})


def trigger_generate(
    item_id: str, category: str, prompt: str, voice: str, title: str, book: dict | None = None
) -> None:
    """fire-and-forget：梗概/书章 -> AI 生成剧本 -> 合成。"""
    task = asyncio.create_task(_generate_and_synthesize_bg(item_id, category, prompt, voice, title, book))

    def _on_done(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            log.error("sleep 生成 task 异常 id=%s: %s", item_id, exc)

    task.add_done_callback(_on_done)


def audio_abs_path(audio_rel: str) -> Path:
    return DATA_DIR / audio_rel


# ── 重命名 / 播放计数 / AI 封面 ──
async def rename_item(item_id: str, title: str) -> None:
    async with get_db() as db:
        await db.execute("UPDATE sleep_items SET title=? WHERE id=?", (title, item_id))
        await db.commit()


async def increment_play(item_id: str) -> int:
    async with get_db() as db:
        await db.execute(
            "UPDATE sleep_items SET play_count = COALESCE(play_count, 0) + 1 WHERE id=?", (item_id,)
        )
        await db.commit()
        cur = await db.execute("SELECT play_count FROM sleep_items WHERE id=?", (item_id,))
        row = await cur.fetchone()
        return int(row[0]) if row else 0


def _cover_prompt(item: dict, extra: str) -> str:
    """按条目内容拼封面生图 prompt：温暖深夜插画风，和 app 视觉（深蓝+暖琥珀）一致。"""
    cat_scene = {
        "reading": "床头一盏暖灯，摊开的书，翻起的书页",
        "meditation": "月光下平静的水面与云",
        "fairytale": "childlike 童话夜空，星星和月亮船",
        "asmr": "雨夜窗景，玻璃上的雨痕",
        "boyfriend": "温暖的深夜卧室一角，台灯与揉皱的被子",
    }.get(item.get("category") or "", "温暖的深夜房间")
    hint = extra.strip() or item.get("summary") or ""
    return (
        f"为一段深夜哄睡音频画一张正方形封面插画。主题：《{item.get('title')}》。{hint}\n"
        f"场景元素参考：{cat_scene}。\n"
        "风格：手绘质感插画，深蓝夜色底 + 暖琥珀色光源，柔和低对比，安静、温暖、适合睡前。"
        "画面里不要出现任何文字。"
    )


async def generate_cover(item_id: str, extra_prompt: str = "") -> str | None:
    """AI 生成封面 -> 存 data/sleep_covers/{id}.{ext} -> 写 cover_path。返回 cover_path 或 None。"""
    from image_gen import generate_image, generate_image_custom_route, generate_image_siliconflow
    from config import UPLOADS_DIR
    item = await get_item_raw(item_id)
    if not item:
        return None
    prompt = _cover_prompt(item, extra_prompt)
    # 官方 Gemini（free tier 生图配额为 0 会失败）-> 自定义路由（CPA 走 CLI 授权）-> 硅基 Kolors
    filename = await generate_image(prompt)
    if not filename:
        filename = await generate_image_custom_route(prompt)
    if not filename:
        filename = await generate_image_siliconflow(prompt)
    if not filename:
        return None
    src = UPLOADS_DIR / filename
    ext = src.suffix.lstrip(".") or "png"
    dst = COVERS_DIR / f"{item_id}.{ext}"
    try:
        dst.write_bytes(src.read_bytes())
    except Exception:
        log.exception("封面落盘失败 id=%s", item_id)
        return None
    cover_rel = f"sleep_covers/{item_id}.{ext}"
    async with get_db() as db:
        await db.execute("UPDATE sleep_items SET cover_path=? WHERE id=?", (cover_rel, item_id))
        await db.commit()
    await _broadcast(item_id, {"cover": True})
    return cover_rel


async def update_progress(item_id: str, progress_sec: int) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE sleep_items SET progress_sec=? WHERE id=?", (int(progress_sec), item_id)
        )
        await db.commit()
