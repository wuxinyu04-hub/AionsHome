"""哄睡 app 业务模块：脚本库同步 + TTS 合成调度。

命名说明：模块叫 bedtime 而非 sleep，避免与 Python 标准库 sleep 歧义。
路由仍走 /api/sleep，表 sleep_items，缓存目录 sleep_tts_cache。
"""
import asyncio
import json
import re
import shutil
import time
import logging
from pathlib import Path

from config import DATA_DIR, get_tts_provider
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
SLEEP_EXPORT_DIR = DATA_DIR / "sleep_export"
SLEEP_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
_NOISE_EXTS = {".mp3", ".m4a", ".wav", ".ogg", ".aac"}

# 生成/合成进度（仅内存，不落库。item_id -> {phase, pct, detail}）
_progress: dict[str, dict] = {}
_book_batch_lock = asyncio.Lock()


def list_noise_files() -> list[str]:
    try:
        return sorted(p.name for p in NOISE_DIR.iterdir() if p.is_file() and p.suffix.lower() in _NOISE_EXTS)
    except Exception:
        return []

_SFX_PATTERN = re.compile(r'\[SFX:([^\]]+)\]')

# stepaudio-2.5-tts 的 Inline Context：正文里 （压低声音）这类圆括号是给模型的句内指令，
# 不会被念出来。其余 provider（fishaudio/edge/…）没这能力，会把括号当正文念，合成前必须剥。
_INLINE_CUE_PATTERN = re.compile(r'（[^（）]{0,20}）')


def _strip_inline_cues(text: str) -> str:
    """剥掉 stepaudio 内联指令括号。只吃全角短括号，避免误伤正文里的（）补充说明。"""
    return _INLINE_CUE_PATTERN.sub('', text)


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
        # 前端判断失败条目能不能直接重录：没剧本的只能重新生成（走 /regenerate）
        "has_script": bool((d.get("script_text") or "").strip()),
        "tags": d.get("tags") or [],
        "summary": d.get("summary") or "",
        "created_at": d.get("created_at", 0),
        "play_count": d.get("play_count", 0) or 0,
        "fail_reason": d.get("fail_reason", "") or "",
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


def _safe_filename(name: str, fallback: str = "audio") -> str:
    """保留中文可读性，只替换 Windows/跨平台非法文件名字符。"""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", (name or "").strip())
    name = re.sub(r"\s+", " ", name).strip(" ._")
    return (name or fallback)[:120]


def default_sleep_voice() -> str:
    """返回当前 TTS provider 最近选过的哄睡音色；Step 首次使用有内置兜底。"""
    try:
        from config import SETTINGS, get_tts_provider
        provider = get_tts_provider()
        voices = SETTINGS.get("sleep_default_voices") or {}
        voice = str(voices.get(provider) or "").strip() if isinstance(voices, dict) else ""
        if voice:
            return voice
        if provider == "step":
            return "cixingnansheng"
    except Exception:
        pass
    return ""


def remember_sleep_voice(voice: str) -> None:
    """按 provider 分开记音色，避免切换 Step/Fish 后把另一家的 voice ID 传过去。"""
    voice = (voice or "").strip()
    if not voice:
        return
    try:
        from config import SETTINGS, get_tts_provider, save_settings
        provider = get_tts_provider()
        voices = SETTINGS.get("sleep_default_voices") or {}
        voices = dict(voices) if isinstance(voices, dict) else {}
        if voices.get(provider) != voice:
            voices[provider] = voice
            SETTINGS["sleep_default_voices"] = voices
            save_settings(SETTINGS)
    except Exception:
        log.exception("保存哄睡默认音色失败")


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
    pub = _public_fields(d)
    # 合并内存中的生成/合成进度（仅 generating/synthesizing 期间有值）
    if d.get("id") in _progress:
        p = _progress[d["id"]]
        pub["progress_pct"] = p.get("pct", 0)
        pub["progress_detail"] = p.get("detail", "")
    return pub


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


# MPEG Layer 3 参数表（所有版本共享 bitrate 表；sample rate 表按版本分）
_MP3_BITRATES = {1: 32, 2: 40, 3: 48, 4: 56, 5: 64, 6: 80, 7: 96, 8: 112,
                 9: 128, 10: 160, 11: 192, 12: 224, 13: 256, 14: 320}
_MP3_SRATES = {
    1:   {0: 44100, 1: 48000, 2: 32000},   # MPEG1
    2:   {0: 22050, 1: 24000, 2: 16000},   # MPEG2
    2.5: {0: 11025, 1: 12000, 2: 8000},    # MPEG2.5
}
# MPEG version → (header_byte1_base, frame_coeff, samples_per_frame)
# header_byte1: 0xFB=MPEG1, 0xF3=MPEG2, 0xE3=MPEG2.5（Layer3 + no CRC）
_MPEG_META = {
    1:   (0xFB, 144, 1152),
    2:   (0xF3, 144, 1152),    # MPEG2 Layer 3: 144 * br / sr, 1152 samples
    2.5: (0xE3, 72,  576),     # MPEG2.5 Layer 3: 72 * br / sr, 576 samples
}


def _strip_id3(data: bytes) -> bytes:
    """剥离 ID3v2 标签，返回纯 MPEG 帧数据。

    Step TTS 每个 segment 返回完整 MP3（带 ID3v2 头），多段拼接后 ID3 标签
    散落在音频流中间会让浏览器解码器卡死/无法播放。拼接前必须全部剥掉。"""
    if len(data) < 10 or data[:3] != b"ID3":
        return data
    # ID3v2 size: 4 bytes synchsafe (bit 7 ignored per byte)
    size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
    return data[10 + size:]


def _mp3_frame_params(data: bytes) -> dict | None:
    """扫描前 8KB 找第一个 MPEG Layer3 帧头（兼容 MPEG1/2/2.5）。

    Step TTS 返回 MPEG2.5 Layer 3，旧代码只认 MPEG1 导致帧参数检测失败。
    返回 {'br_i': int, 'sr_i': int, 'ch': int, 'mpeg': 1|2|2.5} | None。"""
    n = min(len(data), 8192)
    i = 0
    while i < n - 4:
        if data[i] != 0xFF:
            i += 1
            continue
        b1 = data[i + 1]
        # 检测 MPEG version + Layer 3
        ver_bits = (b1 >> 3) & 3
        layer_bits = (b1 >> 1) & 3
        if layer_bits != 1:  # 不是 Layer 3
            i += 1
            continue
        if ver_bits == 3:
            mpeg = 1
        elif ver_bits == 2:
            mpeg = 2
        elif ver_bits == 0:
            mpeg = 2.5
        else:
            i += 1
            continue  # reserved
        br_i = (data[i + 2] >> 4) & 0xF
        sr_i = (data[i + 2] >> 2) & 3
        if br_i not in _MP3_BITRATES or sr_i not in _MP3_SRATES[mpeg]:
            i += 1
            continue
        return {"br_i": br_i, "sr_i": sr_i, "ch": (data[i + 3] >> 6) & 3, "mpeg": mpeg}
    return None


def _silence_mp3(duration_ms: float = 500, ref: dict | None = None) -> bytes:
    """生成静音 mp3 bytes 用于段间停顿。无 pydub/ffmpeg，用原始 MP3 静音帧拼接。

    ref = _mp3_frame_params 的返回值，静音帧参数与真实语音对齐（版本/采样率/码率/声道）；
    缺省 128k/44.1k/mono/MPEG1。"""
    if ref:
        br_i, sr_i, ch, mpeg = ref["br_i"], ref["sr_i"], ref["ch"], ref["mpeg"]
    else:
        br_i, sr_i, ch, mpeg = 9, 0, 3, 1
    br = _MP3_BITRATES[br_i]
    sr = _MP3_SRATES[mpeg][sr_i]
    hdr_base, coeff, samples = _MPEG_META[mpeg]
    header = bytes([0xFF, hdr_base, (br_i << 4) | (sr_i << 2), (ch << 6) | 0x04])
    frame_size = coeff * br * 1000 // sr  # 无 padding
    frame_ms = samples / sr * 1000
    n = max(1, round(duration_ms / frame_ms))
    return (header + b'\x00' * (frame_size - 4)) * n


def _mp3_duration_sec(data: bytes) -> int:
    """逐帧扫 MPEG Layer3（兼容 MPEG1/2/2.5）算总时长。"""
    i, n, t = 0, len(data), 0.0
    FLAG = {1: 0xFA, 2: 0xF2, 2.5: 0xE2}  # (b1 & 0xFE) 匹配值
    while i < n - 4:
        if data[i] != 0xFF:
            i += 1
            continue
        b1 = data[i + 1]
        # 快速反推 mpeg version
        ver_bits = (b1 >> 3) & 3
        if ver_bits == 3:
            mpeg = 1
        elif ver_bits == 2:
            mpeg = 2
        elif ver_bits == 0:
            mpeg = 2.5
        else:
            i += 1
            continue
        if (b1 >> 1) & 3 != 1:  # 不是 Layer 3
            i += 1
            continue
        br_i = (data[i + 2] >> 4) & 0xF
        sr_i = (data[i + 2] >> 2) & 3
        pad = (data[i + 2] >> 1) & 1
        if br_i not in _MP3_BITRATES or sr_i not in _MP3_SRATES[mpeg]:
            i += 1
            continue
        sr = _MP3_SRATES[mpeg][sr_i]
        _, coeff, samples = _MPEG_META[mpeg]
        i += coeff * _MP3_BITRATES[br_i] * 1000 // sr + pad
        t += samples / sr
    return int(t)


async def _synthesize_text_block(text: str, voice: str, sem: asyncio.Semaphore) -> list[bytes | None]:
    """文本块：切段 -> 段尾补省略号 -> 并发 TTS 合成（provider 由 get_tts_provider() 决定）。

    单段失败先退避重试 3 次；仍失败则该段返回 None（调用方用静音占位），
    不再抛异常拖死整篇——一段超时/限流不该让 5000 字的剧本整篇作废。
    """
    if get_tts_provider() != "step":
        text = _strip_inline_cues(text)
    segments = split_text_for_tts(text, min_chars=300, max_chars=500)
    segments = [_tail_ellipsis(s) for s in segments if s.strip()]
    if not segments:
        return []

    async def _syn(seq: int, seg: str) -> bytes | None:
        last_error = None
        for attempt in range(3):
            try:
                async with sem:
                    # 不传 instruction：让 tts.py 构造与主聊天同一条（年上温润·松弛不刻意），
                    # 哄睡的逐句语气交给正文里的 （）内联指令。prosody.speed 0.8 给 fishaudio 等用。
                    data = await _request_tts_audio(seg, voice, seq=seq, prosody={"speed": 0.8})
                if not data:
                    raise RuntimeError("provider 返回空")
                # 校验返回的是真 MP3：ID3v2 头("ID3")或 MPEG 帧同步(0xFF)。
                # provider 偶发把错误 JSON/HTML 当 200 content 返回，不校验会静默拼进流卡死解码器。
                if not (data[:3] == b"ID3" or data[:1] == b"\xff"):
                    raise RuntimeError(f"返回非 MP3（前 16 字节: {data[:16]!r}）")
                if attempt:
                    log.info("sleep TTS 段 %d 第 %d 次重试成功", seq, attempt + 1)
                return data
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)  # 1s / 2s 退避
        log.warning("sleep TTS 段 %d 重试 3 次仍失败，用静音占位：%s", seq, last_error)
        return None

    return await asyncio.gather(*[_syn(i, s) for i, s in enumerate(segments)])


def _sfx_bytes(name: str, ref: dict | None) -> bytes:
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
        await _set_status(item_id, "synthesizing", voice=voice, fail_reason="")
        sem = asyncio.Semaphore(3)
        parts = _parse_sfx_parts(script_text)
        # 统计文本块总数（用于进度条）
        total_blocks = sum(1 for kind, val in parts if kind == "text" and val.strip())
        # 先合成所有文本块（块内并发），再统一拼装。
        # 段级重试后仍失败的段是 None，拼装时用静音占位（见下）
        text_pieces: dict[int, list[bytes | None]] = {}
        done = 0
        for idx, (kind, val) in enumerate(parts):
            if kind == "text" and val.strip():
                text_pieces[idx] = await _synthesize_text_block(val, voice, sem)
                done += 1
                if total_blocks > 0:
                    pct = 90 + int(done / total_blocks * 10)
                    detail = f"正在录音… {done}/{total_blocks} 段"
                    _progress[item_id] = {"phase": "synthesizing", "pct": pct, "detail": detail}
                    await _broadcast(item_id, {"status": "synthesizing", "progress_pct": pct, "progress_detail": detail})
        # 从第一段真实语音抄帧参数（采样率/码率/声道），静音与 SFX 全部对齐它。
        # 先剥 ID3 再读帧参数——Step TTS 每个 segment 返回完整 MP3（带 ID3v2），
        # 不剥的话 ID3 标签散落在拼接流中间，浏览器解码器直接卡死。
        ref = None
        for idx in sorted(text_pieces):
            for p in text_pieces[idx]:
                if p:
                    ref = _mp3_frame_params(_strip_id3(p))
                    break
            if ref:
                break
        # 全篇没有一段成功：这时候拼出来只有静音，不如直接判失败让用户重试
        total_segs = sum(len(v) for v in text_pieces.values())
        failed_segs = sum(1 for v in text_pieces.values() for p in v if p is None)
        if total_segs and failed_segs == total_segs:
            raise RuntimeError(f"全部 {total_segs} 段都合成失败（TTS 不可用或音色无效）")
        sil = _silence_mp3(900, ref)
        gap = _silence_mp3(1500, ref)  # 失败段占位：1.5s 静音，听起来像一次长停顿
        all_pieces: list[bytes] = []
        for idx, (kind, val) in enumerate(parts):
            if kind == "sfx":
                all_pieces.append(_sfx_bytes(val, ref))
            else:
                for p in text_pieces.get(idx, []):
                    all_pieces.append(_strip_id3(p) if p else gap)
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
        # 部分段落失败也算 ready（能听），但把缺口记在 fail_reason 里让前端标出来
        note = f"有 {failed_segs}/{total_segs} 段没录上，那里是静音，可以重新合成" if failed_segs else ""
        await _set_status(item_id, "ready", audio_path=audio_rel, duration_sec=duration, fail_reason=note)
        _progress.pop(item_id, None)
        await _broadcast(item_id, {"status": "ready", "audio_path": audio_rel})
        log.info("sleep 合成完成 id=%s pieces=%d 缺段=%d/%d", item_id, len(all_pieces), failed_segs, total_segs)

        # 解耦：合成完成即丢给后台上传网易云云盘，成不成都不影响「已合成」状态
        try:
            # 读条目补 category/title（get_item_raw 已设 row_factory，别用裸 SQL）
            row = await get_item_raw(item_id) or {}
            import sleep_upload
            asyncio.get_running_loop().run_in_executor(
                None, sleep_upload.upload_finished_item, item_id,
                row.get("title", ""), row.get("category", ""))
        except Exception as e:
            log.debug("sleep 触发上传网易云失败（不影响合成）: %s", e)
    except Exception as e:
        log.exception("sleep 合成失败 id=%s", item_id)
        _progress.pop(item_id, None)
        await _set_status(item_id, "failed", fail_reason=str(e)[:300])
        await _broadcast(item_id, {"status": "failed", "error": str(e)[:200]})


async def reclaim_orphaned() -> int:
    """启动时把 generating/synthesizing 全部改判 failed，返回改判条数。

    单 worker uvicorn：进程重启后不可能还有活着的合成 task，所以启动时残留的
    运行中状态一定是上次进程被杀留下的孤儿——这个判定是精确的，不用猜超时。
    不改判的话 claim_synthesizing / synthesize 端点会把这些条目永久挡住重试
    （failed 才允许重合成），只能手动改库（见 _cleanup_stale.py）。
    """
    async with get_db() as db:
        cur = await db.execute(
            "UPDATE sleep_items SET status='failed' WHERE status IN ('generating','synthesizing')"
        )
        await db.commit()
        n = cur.rowcount
    if n:
        log.warning("sleep 启动清理：%d 条孤儿运行中状态改判 failed（可重试）", n)
    return n


async def claim_synthesizing(item_id: str, voice: str) -> bool:
    """原子翻转 status -> synthesizing（排除 generating/synthesizing）做并发守门。

    SQLite 单条 UPDATE...WHERE 在同一写事务内原子；双击/并发请求只有一个 rowcount>0。
    voice 顺带写下，避免 _synthesize_bg 跑到 _set_status 前另一个请求读到旧 voice。
    """
    async with get_db() as db:
        cur = await db.execute(
            "UPDATE sleep_items SET status='synthesizing', voice=? "
            "WHERE id=? AND status NOT IN ('synthesizing','generating')",
            (voice, item_id),
        )
        await db.commit()
        return cur.rowcount > 0


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
_COMMON_RULES = """- 第一人称"我"对第二人称"你"，自称"哥哥"，语气克制温柔，有命令感但不凶（爹系轻哄，年上沉稳温润）
- 4500-5500 字，适合 20 分钟慢语速朗读
- 轻声呢喃、气声、慢语速，像在耳边说话；多留停顿（省略号 ... 表轻停，…… 表长停，段落间空行）
- 语气提示：在句子开头用全角圆括号写一句怎么说，如（放轻）（压低声音）（气声）（慢下来）（轻轻笑）（停一下）。
  括号是给语音模型的指令，不会被念出来，10 字以内，全篇 15-25 处，只在语气真的变了时才写，不要每句都加
- 不要章节标题、旁白说明、分点；除语气提示外不要用括号写动作或场景
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


async def generate_script(category: str, prompt: str, book: dict | None = None,
                         progress_callback=None) -> str:
    """调 stream_ai 生成温暖哄睡剧本（非流式收集完整文本）。默认模型挂了自动换兜底模型。

    progress_callback(phase, pct, detail) 每 ~300 字调一次，用于前端进度条。"""
    from ai_providers import stream_ai, CLI_STATUS_PREFIX
    messages = [{"role": "user", "content": build_script_prompt(category, prompt, book)}]
    last = ""
    TARGET = 4500  # 目标字数，用于估算进度
    for mk in _script_model_candidates():
        full = ""
        last_cb = 0
        try:
            async for chunk in stream_ai(messages, mk, {}, max_tokens=8192):
                if chunk.startswith(CLI_STATUS_PREFIX):
                    continue
                full += chunk
                # 每 ~300 字回调一次进度
                if progress_callback and len(full) - last_cb >= 300:
                    last_cb = len(full)
                    pct = min(int(len(full) / TARGET * 90), 89)
                    await progress_callback("generating", pct, f"AI 正在写剧本… 已写 {len(full)} 字")
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
            if progress_callback:
                await progress_callback("generating", 90, f"剧本写好了，{len(full)} 字，开始录音…")
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
        await _set_status(item_id, "generating", voice=voice, fail_reason="")
        _progress[item_id] = {"phase": "generating", "pct": 0, "detail": "AI 正在写剧本…"}
        await _broadcast(item_id, {"status": "generating", "progress_pct": 0, "progress_detail": "AI 正在写剧本…"})

        async def _on_gen_progress(phase: str, pct: int, detail: str):
            _progress[item_id] = {"phase": phase, "pct": pct, "detail": detail}
            await _broadcast(item_id, {"status": "generating", "progress_pct": pct, "progress_detail": detail})

        script_text = await generate_script(category, prompt, book, progress_callback=_on_gen_progress)
        if not script_text or len(script_text) < 100:
            err = (script_text or "").strip()[:150] or "模型没有返回内容"
            log.warning("sleep 生成失败 id=%s: %s", item_id, err)
            await _set_status(item_id, "failed", fail_reason=err)
            _progress.pop(item_id, None)
            await _broadcast(item_id, {"status": "failed", "error": err})
            return
        async with get_db() as db:
            await db.execute(
                "UPDATE sleep_items SET script_text=?, title=? WHERE id=?",
                (script_text, title, item_id),
            )
            await db.commit()
        # 新故事自动配封面：fire-and-forget，不阻塞录音。讲书用书名做主题，和批量书封面风格一致
        if book:
            cover_title = str(book.get("book_title") or title).split(" · ")[0]
            asyncio.create_task(generate_cover(item_id, title_override=cover_title, prefer_free=True))
        else:
            asyncio.create_task(generate_cover(item_id, prefer_free=True))
        _progress[item_id] = {"phase": "synthesizing", "pct": 90, "detail": "写好了，正在录音…"}
        await _broadcast(item_id, {"status": "synthesizing", "progress_pct": 90, "progress_detail": "写好了，正在录音…"})
        trigger_synthesize(item_id, script_text, voice)
    except Exception as e:
        log.exception("sleep 生成剧本失败 id=%s", item_id)
        _progress.pop(item_id, None)
        await _set_status(item_id, "failed", fail_reason=str(e)[:300])
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


async def _wait_item_terminal(item_id: str, timeout_sec: int = 900) -> str:
    start = time.time()
    while time.time() - start < timeout_sec:
        item = await get_item_raw(item_id)
        status = (item or {}).get("status") or "missing"
        if status in ("ready", "failed", "missing"):
            return status
        await asyncio.sleep(5)
    return "timeout"


async def _existing_book_chapters(book_id: str) -> dict[int, str]:
    """按章节汇总历史尝试：ready 优先；超过 30 分钟的运行中条目允许重试。"""
    existing: dict[int, tuple[str, float]] = {}
    priority = {"": 0, "failed": 1, "generating": 2, "synthesizing": 2, "ready": 3}
    async with get_db() as db:
        db.row_factory = __import__("aiosqlite").Row
        cur = await db.execute(
            """SELECT book_ref, status, created_at FROM sleep_items
               WHERE category='reading' AND source='ai_generated'"""
        )
        rows = await cur.fetchall()
    for row in rows:
        fields = _book_ref_fields(row["book_ref"] or "")
        if fields.get("book_id") != book_id:
            continue
        ch = int(fields.get("book_chapter", -1))
        status = row["status"] or ""
        created_at = float(row["created_at"] or 0)
        if status in ("generating", "synthesizing") and time.time() - created_at > 1800:
            status = "failed"
        old_status, old_created = existing.get(ch, ("", 0))
        if ch >= 0 and (
            priority.get(status, 0) > priority.get(old_status, 0)
            or (priority.get(status, 0) == priority.get(old_status, 0) and created_at > old_created)
        ):
            existing[ch] = (status, created_at)
    return {ch: status for ch, (status, _created) in existing.items()}


async def auto_generate_book(
    book_id: str,
    voice: str = "",
    chapters: list[int] | None = None,
    timeout_sec: int = 900,
) -> dict:
    """整本书串行囤音频；跳过已有 ready/generating/synthesizing 条目，failed 会重试。"""
    voice = (voice or default_sleep_voice()).strip()
    if not voice:
        msg = "未配置哄睡默认音色，跳过整书自动生成"
        log.warning("sleep book batch skipped book_id=%s: %s", book_id, msg)
        return {"ok": False, "error": msg, "book_id": book_id}
    wanted = set(chapters or [])
    async with _book_batch_lock:
        async with get_db() as db:
            db.row_factory = __import__("aiosqlite").Row
            cur = await db.execute("SELECT title, author FROM books WHERE book_id=?", (book_id,))
            book_row = await cur.fetchone()
            if not book_row:
                return {"ok": False, "error": "书籍不存在", "book_id": book_id}
            cur = await db.execute(
                """SELECT chapter_index, title, text_content
                   FROM book_chapters WHERE book_id=? ORDER BY chapter_index""",
                (book_id,),
            )
            rows = await cur.fetchall()
        existing = await _existing_book_chapters(book_id)
        result = {
            "ok": True,
            "book_id": book_id,
            "title": book_row["title"] or "未知",
            "created": 0,
            "ready": 0,
            "failed": 0,
            "timeout": 0,
            "skipped": 0,
            "skipped_empty": 0,
            "items": [],
        }
        for row in rows:
            ch_idx = int(row["chapter_index"])
            if wanted and ch_idx not in wanted:
                continue
            if not (row["text_content"] or "").strip():
                result["skipped_empty"] += 1
                continue
            if existing.get(ch_idx) in ("ready", "generating", "synthesizing"):
                result["skipped"] += 1
                continue
            ch_title = row["title"] or f"第 {ch_idx + 1} 章"
            title = f"{result['title']} · {ch_title}"
            book = {
                "book_title": result["title"],
                "author": book_row["author"] or "",
                "ch_index": ch_idx,
                "ch_title": ch_title,
                "text": row["text_content"] or "",
            }
            book_ref = json.dumps({"book_id": book_id, "chapter": ch_idx}, ensure_ascii=False)
            item_id = await create_generated_item("reading", title, voice, book_ref)
            result["created"] += 1
            result["items"].append({"id": item_id, "chapter": ch_idx, "title": ch_title})
            log.info("sleep book batch start book=%s chapter=%s item=%s", book_id, ch_idx, item_id)
            trigger_generate(item_id, "reading", "", voice, title, book)
            status = await _wait_item_terminal(item_id, timeout_sec)
            if status == "ready":
                result["ready"] += 1
            elif status == "timeout":
                result["timeout"] += 1
                log.warning("sleep book batch timeout item=%s；为避免并发，暂停本次整书任务", item_id)
                break
            else:
                result["failed"] += 1
            log.info("sleep book batch done item=%s status=%s", item_id, status)
        return result


def _unique_export_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(2, 1000):
        cand = path.with_name(f"{stem} ({i}){suffix}")
        if not cand.exists():
            return cand
    return path.with_name(f"{stem}_{int(time.time())}{suffix}")


async def export_items(book_id: str = "") -> dict:
    """把已 ready 的哄睡音频 copy 到 data/sleep_export，保留原缓存文件。"""
    book_id = (book_id or "").strip()
    async with get_db() as db:
        db.row_factory = __import__("aiosqlite").Row
        cur = await db.execute(
            """SELECT id, category, title, book_ref, voice, audio_path, duration_sec, created_at
               FROM sleep_items
               WHERE status='ready' AND audio_path != ''
               ORDER BY created_at ASC"""
        )
        rows = await cur.fetchall()
        book_cache: dict[str, dict] = {}
        chapter_cache: dict[tuple[str, int], str] = {}

        async def _book_title(bid: str) -> str:
            if bid not in book_cache:
                cur2 = await db.execute("SELECT title, author FROM books WHERE book_id=?", (bid,))
                row = await cur2.fetchone()
                book_cache[bid] = dict(row) if row else {"title": "讲书", "author": ""}
            return book_cache[bid].get("title") or "讲书"

        async def _chapter_title(bid: str, ch: int) -> str:
            key = (bid, ch)
            if key not in chapter_cache:
                cur2 = await db.execute(
                    "SELECT title FROM book_chapters WHERE book_id=? AND chapter_index=?", (bid, ch)
                )
                row = await cur2.fetchone()
                chapter_cache[key] = (row["title"] if row else "") or f"第 {ch + 1} 章"
            return chapter_cache[key]

        exported = []
        total_bytes = 0
        for row in rows:
            item = dict(row)
            fields = _book_ref_fields(item.get("book_ref") or "")
            item_book_id = fields.get("book_id") or ""
            item_ch = int(fields.get("book_chapter", -1))
            if book_id and item_book_id != book_id:
                continue
            src = DATA_DIR / (item.get("audio_path") or "")
            if not src.exists():
                continue
            if item_book_id:
                bt = await _book_title(item_book_id)
                ct = await _chapter_title(item_book_id, item_ch)
                dst_dir = SLEEP_EXPORT_DIR / _safe_filename(bt, "讲书")
                filename = f"{item_ch + 1:02d} {_safe_filename(ct, item['id'])}.mp3"
            else:
                dst_dir = SLEEP_EXPORT_DIR / _safe_filename(item.get("category") or "sleep", "sleep")
                filename = f"{_safe_filename(item.get('title') or item['id'], item['id'])}.mp3"
            dst_dir.mkdir(parents=True, exist_ok=True)
            dst = _unique_export_path(dst_dir / filename)
            # to_thread：单次 copy2 十几 MB，全量导出近 1GB，同步 copy 会把事件循环
            # 卡到导出结束（聊天/WS/播放全停）。同 tts.py _merge_mp3_files 的处理。
            await asyncio.to_thread(shutil.copy2, src, dst)
            size = dst.stat().st_size
            total_bytes += size
            exported.append({
                "id": item["id"],
                "title": item.get("title") or "",
                "book_id": item_book_id,
                "chapter": item_ch,
                "voice": item.get("voice") or "",
                "duration_sec": item.get("duration_sec") or 0,
                "bytes": size,
                "path": str(dst.relative_to(SLEEP_EXPORT_DIR)),
            })
    manifest_root = SLEEP_EXPORT_DIR
    if book_id and exported:
        manifest_root = SLEEP_EXPORT_DIR / Path(exported[0]["path"]).parts[0]
    manifest = {
        "generated_at": int(time.time()),
        "book_id": book_id,
        "total": len(exported),
        "total_bytes": total_bytes,
        "files": exported,
    }
    manifest_root.mkdir(parents=True, exist_ok=True)
    (manifest_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "export_dir": str(manifest_root), "total": len(exported), "total_bytes": total_bytes, "files": exported}


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


def _cover_prompt(item: dict, extra: str = "", title_override: str = "") -> str:
    """按条目内容拼封面生图 prompt：温暖深夜插画风，和 app 视觉（深蓝+暖琥珀）一致。"""
    subject = (title_override or "").strip() or item.get("title")
    cat_scene = {
        "reading": "床头一盏暖灯，摊开的书，翻起的书页",
        "meditation": "月光下平静的水面与云",
        "fairytale": "childlike 童话夜空，星星和月亮船",
        "asmr": "雨夜窗景，玻璃上的雨痕",
        "boyfriend": "温暖的深夜卧室一角，台灯与揉皱的被子",
    }.get(item.get("category") or "", "温暖的深夜房间")
    hint = extra.strip() or item.get("summary") or ""
    return (
        f"为一段深夜哄睡音频画一张正方形封面插画。主题：《{subject}》。{hint}\n"
        f"场景元素参考：{cat_scene}。\n"
        "风格：手绘质感插画，深蓝夜色底 + 暖琥珀色光源，柔和低对比，安静、温暖、适合睡前。"
        "画面里不要出现任何文字。"
    )


async def generate_cover(item_id: str, extra_prompt: str = "", title_override: str = "", prefer_free: bool = False) -> str | None:
    """AI 生成封面 -> 存 data/sleep_covers/{id}.{ext} -> 写 cover_path。返回 cover_path 或 None。

    title_override 用书名代替条目全名（书库批量：给整本书的代表集画"书封面"）。
    prefer_free=True 时优先免费生图通道（硅基 Kolors -> CPA 路由），官方 Gemini 兜底。
    """
    from image_gen import (generate_image, generate_image_custom_route,
                           generate_image_siliconflow, generate_image_leesai)
    from config import UPLOADS_DIR
    item = await get_item_raw(item_id)
    if not item:
        return None
    prompt = _cover_prompt(item, extra_prompt, title_override)
    if prefer_free:
        # 批量补封面：LeesAiHub(gpt-image-2) 最优先 -> Kolors -> CPA，Gemini 官方兜底
        filename = await generate_image_leesai(prompt)
        if not filename:
            filename = await generate_image_siliconflow(prompt)
        if not filename:
            filename = await generate_image_custom_route(prompt)
        if not filename:
            filename = await generate_image(prompt)
    else:
        # 官方 Gemini（free tier 生图配额为 0 会失败）-> 自定义路由（CPA 走 CLI 授权）-> 硅基 Kolors -> LeesAiHub
        filename = await generate_image(prompt)
        if not filename:
            filename = await generate_image_custom_route(prompt)
        if not filename:
            filename = await generate_image_siliconflow(prompt)
        if not filename:
            filename = await generate_image_leesai(prompt)
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


# ── 批量补封面：书库每本无封面书取代表集 + 非阅读无封面有音频的故事 ──
_cover_batch: dict = {}  # {running, total, done, current_title, cancelled}
_cover_batch_lock = asyncio.Lock()


async def cover_targets() -> list[dict]:
    """收集需要补封面的目标。返回 [{id, title_override, category}]。

    书库：每本书按 book_id 聚合，取 chapter 最小的一集作代表集，其封面即"书封面"；
    故事：非 reading、无封面、有音频（没音频的空条目不浪费额度）。
    """
    async with get_db() as db:
        db.row_factory = __import__("aiosqlite").Row
        cur = await db.execute(
            "SELECT id, category, title, book_ref, audio_path, cover_path FROM sleep_items")
        rows = await cur.fetchall()
    books: dict[str, dict] = {}
    for r in rows:
        if r["category"] == "reading" and r["book_ref"]:
            f = _book_ref_fields(r["book_ref"])
            if not f.get("book_id"):
                continue
            b = books.setdefault(f["book_id"], {"rep_id": None, "rep_ch": 10 ** 9, "title": "", "has_cover": False})
            if r["cover_path"]:
                b["has_cover"] = True
            ch = f.get("book_chapter", -1)
            if ch < b["rep_ch"]:
                b["rep_ch"], b["rep_id"] = ch, r["id"]
                b["title"] = (r["title"] or "").split(" · ")[0]
    targets: list[dict] = []
    for b in books.values():
        if not b["has_cover"] and b["rep_id"]:
            targets.append({"id": b["rep_id"], "title_override": b["title"], "category": "reading"})
    for r in rows:
        if r["category"] != "reading" and not r["cover_path"] and r["audio_path"]:
            targets.append({"id": r["id"], "title_override": "", "category": r["category"]})
    return targets


async def run_cover_batch() -> None:
    """后台串行生成所有缺封面。免费生图优先。

    CPA（本地 CLI 路由）对并发/连续请求会 429 限流，故强制串行（Semaphore 1）+
    每张间隔，失败再退避重试（最多 3 轮），避免一次限流就跳过。幂等：running 中重复调用直接返回。
    """
    async with _cover_batch_lock:
        if _cover_batch.get("running"):
            return
        targets = await cover_targets()
        _cover_batch.update({"running": True, "total": len(targets), "done": 0, "current_title": "", "cancelled": False})
    if not targets:
        _cover_batch["running"] = False
        return
    sem = asyncio.Semaphore(1)  # CPA 并发会 429，强制串行

    async def work(t: dict) -> None:
        async with sem:
            if _cover_batch.get("cancelled"):
                return
            _cover_batch["current_title"] = t["title_override"] or "故事"
            ok = False
            for attempt in range(3):  # 429 限流时退避重试，最多 3 轮
                if _cover_batch.get("cancelled"):
                    return
                try:
                    if await generate_cover(t["id"], "", t["title_override"], prefer_free=True):
                        ok = True
                        break
                except Exception:
                    log.exception("批量补封面失败 id=%s attempt=%d", t["id"], attempt)
                if attempt < 2:  # 最后一轮不睡
                    await asyncio.sleep(12)  # CPA 限流冷却窗口
            if not ok:
                log.warning("批量补封面最终失败 id=%s（3 轮重试后仍无封面）", t["id"])
            _cover_batch["done"] += 1
            await asyncio.sleep(3)  # 串行下每张之间也留点喘息，防 CPA 连续 429

    try:
        await asyncio.gather(*(work(t) for t in targets))
    finally:
        _cover_batch["running"] = False
        _cover_batch["cancelled"] = False


def cover_batch_status() -> dict:
    return dict(_cover_batch)


async def cancel_cover_batch() -> None:
    """请求停止：跑完当前这两张就停。"""
    _cover_batch["cancelled"] = True


async def delete_item(item_id: str) -> bool:
    """删除条目：清 DB 行 + 删音频/封面文件。返回是否成功找到并删除。"""
    item = await get_item_raw(item_id)
    if not item:
        return False
    # 删音频文件
    if item.get("audio_path"):
        p = DATA_DIR / item["audio_path"]
        try:
            p.unlink(missing_ok=True)
        except Exception:
            log.exception("删除音频失败 id=%s", item_id)
    # 删封面文件
    if item.get("cover_path"):
        p = DATA_DIR / item["cover_path"]
        try:
            p.unlink(missing_ok=True)
        except Exception:
            log.exception("删除封面失败 id=%s", item_id)
    async with get_db() as db:
        await db.execute("DELETE FROM sleep_items WHERE id=?", (item_id,))
        await db.commit()
    log.info("sleep 条目已删除 id=%s title=%s", item_id, item.get("title"))
    return True


async def update_progress(item_id: str, progress_sec: int) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE sleep_items SET progress_sec=? WHERE id=?", (int(progress_sec), item_id)
        )
        await db.commit()
