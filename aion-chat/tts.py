"""
服务端流式 TTS 模块
- 按句子边界切分 AI 回复文本
- 异步并行调用硅基流动 TTS 合成
- 通过 WebSocket 推送音频 URL 给前端顺序播放
"""

import re, asyncio, logging, time
from pathlib import Path
import httpx

from config import get_key, TTS_CACHE_DIR, TTS_CACHE_MAX_BYTES

log = logging.getLogger("tts")


def _log_background_tts_failure(task: asyncio.Task):
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.warning("background TTS synthesis failed: %s", e)


def cleanup_tts_cache_dir(cache_dir: Path = TTS_CACHE_DIR, max_bytes: int = TTS_CACHE_MAX_BYTES, *, skip: set[Path] | None = None):
    """Delete oldest cached MP3 files until the directory is under max_bytes."""
    skip_resolved = {p.resolve() for p in (skip or set())}
    files = []
    total = 0
    for path in cache_dir.glob("*.mp3"):
        try:
            resolved = path.resolve()
            stat = path.stat()
        except OSError:
            continue
        total += stat.st_size
        if resolved not in skip_resolved:
            files.append((stat.st_mtime, stat.st_size, path))

    if total <= max_bytes:
        return

    for _mtime, size, path in sorted(files, key=lambda item: item[0]):
        try:
            path.unlink()
            total -= size
            log.info("TTS cache cleanup removed %s", path.name)
        except OSError as e:
            log.warning("TTS cache cleanup failed for %s: %s", path, e)
        if total <= max_bytes:
            break

# 需要从 TTS 文本中剥除的特殊标签
_STRIP_PATTERNS = [
    re.compile(r'\[CAM_CHECK\]'),
    re.compile(r'\[POI_SEARCH:[^\]]*\]'),
    re.compile(r'\[MUSIC:[^\]]*\]'),
    re.compile(r'\[ALARM:[^\]]*\]'),
    re.compile(r'\[REMINDER:[^\]]*\]'),
    re.compile(r'\[Monitor:[^\]]*\]'),
    re.compile(r'\[SCHEDULE_DEL:[^\]]*\]'),
    re.compile(r'\[SCHEDULE_LIST\]'),
    re.compile(r'\[LUCKIN:[^\]]*\]', re.IGNORECASE),
    re.compile(r'\[TOY:[^\]]*\]'),
    re.compile(r'\[MOMENT:[^\]]*\]'),
    re.compile(r'\[MEMORY:[^\]]*\]'),
    re.compile(r'\[微信消息[：:][^\]]*\]'),
    re.compile(r'\[心里嘀咕\s*[：:]\s*[^\]]*\]'),
    re.compile(r'\[查看动态:\d+\]'),
    re.compile(r'\[SELFIE:[^\]]*\]'),
    re.compile(r'\[DRAW:[^\]]*\]'),
    re.compile(r'\[DATE_(?:BACKGROUND|BG|STATE|ACTION)\s*:\s*[^\]]*\]', re.IGNORECASE),
    re.compile(r'\[DATE_END_READY\]', re.IGNORECASE),
    re.compile(r'\[悄悄话[：:][^\]]*\]'),
    re.compile(r'<meta>[\s\S]*?</meta>'),
    re.compile(r'<emotion>[^<]*</emotion>'),
]

# 句子结束符（用于切分）
_SENTENCE_ENDS = set('。！？…!?')
_COMMA_CHARS = set('，,、；;：:')

_SENTENCE_ENDS |= set('.!?')
_COMMA_CHARS |= set(',;:')

# MiniMax emotion 合法值（由 AI 在回复开头用 <emotion>X</emotion> 自标）
# 文档允许：happy/sad/angry/fearful/disgusted/surprised/calm/fluent/whisper
_EMOTION_PATTERN = re.compile(r'<emotion>([^<]+)</emotion>')
_VALID_EMOTIONS = {"happy", "sad", "angry", "fearful", "disgusted", "surprised", "calm", "fluent", "whisper"}
_DEFAULT_EMOTION = "calm"

# 后端情绪推断：AI 不打 <emotion> 标签时（或者标签非法），
# 后端从纯文本里扫关键词/标点挑一个。这是"隐式"的真正落地点 —
# 即使 AI 完全忘了写 emotion 标签，TTS 仍能给 MiniMax 带一个合理的语气，
# 对接 SenseAudio / Edge / 硅基流动时这条推断照样走（被 _strip_tags 剥掉，行为不变）。
_EMOTION_KEYWORDS = {
    "happy":   ("哈哈", "哈哈哈", "嘻嘻", "开心", "高兴", "笑", "嘿嘿", "好耶", "棒", "太好了", "喜欢", "想你了", "爱你", "可爱", "嘿嘿嘿"),
    "whisper": ("悄悄", "晚安", "嘘", "低声", "小声", "凑近", "耳语", "睡吧"),
    "angry":   ("讨厌", "滚", "哼", "气死", "过分", "混蛋", "烦死", "找打", "欠收拾"),
    "sad":     ("难过", "伤心", "哭", "呜呜", "失落", "心疼", "孤单", "想哭", "流泪", "呜呜呜"),
}


def _infer_emotion_from_text(text: str) -> str:
    """从纯文本里推断一个 emotion。无明显信号 → _DEFAULT_EMOTION。

    实现思路：扫前 400 字（情绪信号通常落在开头），关键词命中数加权，
    标点（！～）做轻度加成，挑分数最高的；完全没信号 → calm。
    仅依赖 stdlib，无外部模型依赖，不会延迟 TTS。
    """
    if not text:
        return _DEFAULT_EMOTION
    snippet = text[:400]
    if not snippet:
        return _DEFAULT_EMOTION
    scores = {emo: 0 for emo in _EMOTION_KEYWORDS}
    for emo, keywords in _EMOTION_KEYWORDS.items():
        for kw in keywords:
            scores[emo] += snippet.count(kw)
    # 标点加成：连用「！」多 → 兴奋 happy；连用「～」多 → 拖腔 whisper
    if snippet.count("！") + snippet.count("!") >= 2:
        scores["happy"] += 1
    if snippet.count("～") + snippet.count("~") >= 2:
        scores["whisper"] += 1
    best_emo = max(scores.items(), key=lambda kv: kv[1])[0]
    return best_emo if scores[best_emo] > 0 else _DEFAULT_EMOTION


def _extract_emotion(text: str) -> tuple[str, str]:
    """从文本里抽 <emotion>X</emotion>；没有或非法时让后端从纯文本推断一个。
    返回 (emotion, 去掉标签后的 text)。这样 MiniMax 几乎总能拿到非默认情绪。"""
    if not text:
        return _DEFAULT_EMOTION, text or ""
    m = _EMOTION_PATTERN.search(text)
    if not m:
        return _infer_emotion_from_text(text), text
    raw = m.group(1).strip().lower()
    cleaned = _EMOTION_PATTERN.sub('', text).strip()
    if raw in _VALID_EMOTIONS:
        return raw, cleaned
    # 标签写了但内容是乱码——仍走推断
    return _infer_emotion_from_text(cleaned), cleaned

def _strip_tags(text: str) -> str:
    """去除所有特殊标签，只保留纯文本"""
    for p in _STRIP_PATTERNS:
        text = p.sub('', text)
    return text.strip()


def _has_unclosed_tag(text: str) -> bool:
    """检查是否有未闭合的 [...] 或 <meta>"""
    # 检查 [TAG:... 没有闭合的 ]
    last_open = text.rfind('[')
    if last_open >= 0 and ']' not in text[last_open:]:
        return True
    # 检查 <meta> 没有闭合的 </meta>
    meta_opens = text.count('<meta>')
    meta_closes = text.count('</meta>')
    if meta_opens > meta_closes:
        return True
    return False


def _find_cut_position_for_text(buffer: str, min_chars: int, max_chars: int) -> int | None:
    clean_count = 0
    in_bracket = False
    in_meta = False
    best_comma_cut = None

    i = 0
    while i < len(buffer):
        ch = buffer[i]

        if ch == '[' and not in_meta:
            in_bracket = True
        elif ch == ']' and in_bracket:
            in_bracket = False
            i += 1
            continue
        elif buffer[i:i+6] == '<meta>':
            in_meta = True
            i += 6
            continue
        elif buffer[i:i+7] == '</meta>':
            in_meta = False
            i += 7
            continue

        if in_bracket or in_meta:
            i += 1
            continue

        clean_count += 1

        if clean_count >= min_chars:
            if ch in _SENTENCE_ENDS:
                return i
            if ch in _COMMA_CHARS:
                best_comma_cut = i

        if clean_count >= max_chars:
            if best_comma_cut is not None:
                return best_comma_cut
            return i

        i += 1

    return None


def split_text_for_tts(text: str, *, min_chars: int = 300, max_chars: int = 500) -> list[str]:
    """Split long text into TTS-sized chunks, preferring sentence boundaries."""
    remaining = (text or "").strip()
    segments: list[str] = []
    min_chars = max(1, min_chars)
    max_chars = max(min_chars, max_chars)

    while remaining:
        cleaned = _strip_tags(remaining).strip()
        if not cleaned:
            break
        if len(cleaned) <= max_chars:
            segments.append(cleaned)
            break
        if _has_unclosed_tag(remaining):
            segments.append(cleaned)
            break

        cut_pos = _find_cut_position_for_text(remaining, min_chars, max_chars)
        if cut_pos is None:
            cut_pos = min(len(remaining), max_chars) - 1

        part = _strip_tags(remaining[:cut_pos + 1]).strip()
        remaining = remaining[cut_pos + 1:].strip()
        if part:
            segments.append(part)

    return segments


async def _request_senseaudio_tts_audio(text: str, voice: str, *, seq: int | None = None) -> bytes | None:
    """SenseAudio（商汤）TTS 合成。响应里音频是 hex 编码字符串，需 fromhex 解码。"""
    key = get_key("senseaudio")
    if not key:
        log.warning("SenseAudio TTS: 无 API Key，跳过合成 seq=%s", seq)
        return None
    if not voice:
        log.warning("SenseAudio TTS: 未选择音色，跳过合成 seq=%s", seq)
        return None
    payload = {
        "model": "senseaudio-tts-1.5-260319",
        "text": text,
        "stream": False,
        "voice_setting": {"voice_id": voice, "speed": 1, "vol": 1, "pitch": 0},
        "audio_setting": {"format": "mp3", "sample_rate": 32000},
    }
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                resp = await client.post(
                    "https://api.senseaudio.cn/v1/t2a_v2",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json=payload,
                )
            if resp.status_code == 200:
                body = resp.json()
                # SenseAudio 把业务错误包在 HTTP 200 里：{"base_resp":{"status_code":400,"status_msg":"..."}}
                base = body.get("base_resp") or {}
                biz_code = base.get("status_code", 0)
                if biz_code and biz_code != 0:
                    log.warning("SenseAudio TTS 业务错误: code=%s msg=%s voice=%s seq=%s attempt=%d",
                                biz_code, base.get("status_msg"), voice, seq, attempt + 1)
                else:
                    data = body.get("data") or {}
                    hex_audio = data.get("audio") or ""
                    if hex_audio:
                        return bytes.fromhex(hex_audio)
                    log.warning("SenseAudio TTS: 响应无 audio 字段 seq=%s attempt=%d", seq, attempt + 1)
            else:
                log.warning("SenseAudio TTS API 错误: status=%d seq=%s attempt=%d", resp.status_code, seq, attempt + 1)
        except Exception as e:
            log.warning("SenseAudio TTS 请求异常: %s seq=%s attempt=%d", e, seq, attempt + 1)
        await asyncio.sleep(0.5 * (attempt + 1))
    return None


async def _request_edge_tts_audio(text: str, voice: str, *, seq: int | None = None) -> bytes | None:
    """Edge TTS（微软 Azure 神经语音免费逆向接口）合成。
    无需 API Key；接口本身流式，攒成完整 MP3 bytes 返回以保持上游签名一致。
    无 emotion 字段，参数被上游丢弃即可。
    """
    if not voice:
        log.warning("Edge TTS: 未选择音色，跳过合成 seq=%s", seq)
        return None
    try:
        import edge_tts
    except ImportError:
        log.warning("Edge TTS: 缺少 edge-tts 包，请 pip install edge-tts seq=%s", seq)
        return None
    for attempt in range(3):
        try:
            communicate = edge_tts.Communicate(text, voice)
            audio = bytearray()
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio":
                    data = chunk.get("data")
                    if data:
                        audio.extend(data)
            if audio:
                return bytes(audio)
            log.warning("Edge TTS: 流式响应无音频数据 seq=%s attempt=%d", seq, attempt + 1)
        except Exception as e:
            log.warning("Edge TTS 请求异常: %s seq=%s attempt=%d", e, seq, attempt + 1)
        await asyncio.sleep(0.5 * (attempt + 1))
    return None


async def _request_fishaudio_tts_audio(text: str, voice: str, *, seq: int | None = None, emotion: str = _DEFAULT_EMOTION, prosody: dict | None = None) -> bytes | None:
    """Fish Audio S2.1 Pro Free 合成。
    免费档持续到 2026-07 底（无字符上限，受公平使用政策约束）。
    接口返回原始 MP3 bytes（非 hex）。
    voice 字段映射到 reference_id；prosody={"speed":0.5-2.0,"volume":0} 控制语速音量。
    emotion 不作为字段传（S2 用文本内自然语言/括号表达控制，由调用方在 text 里处理）。
    """
    key = get_key("fishaudio")
    if not key:
        log.warning("FishAudio TTS: 无 API Key，跳过合成 seq=%s", seq)
        return None
    # 无 voice 时用 Fish Audio 默认声，voice 字段语义是 reference_id
    payload: dict = {
        "text": text,
        "format": "mp3",
    }
    if voice:
        payload["reference_id"] = voice
    if prosody:
        payload["prosody"] = prosody
    # timeout 30->120：哄睡段落 300-500 字 + prosody 慢速，合成常超 30s，短超时会把好请求掐死
    # （ReadTimeout 的 str(e) 是空串，日志里表现为"请求异常: " 后面没内容）
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15), trust_env=True) as client:
                resp = await client.post(
                    "https://api.fish.audio/v1/tts",
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                        "model": "s2.1-pro-free",
                    },
                    json=payload,
                )
            if resp.status_code == 200:
                # Fish Audio 直接返回 MP3 bytes（content-type: audio/mpeg）
                if resp.content:
                    return resp.content
                log.warning("FishAudio TTS: 响应为空 seq=%s attempt=%d", seq, attempt + 1)
            else:
                # 业务错误以 JSON 返回，截前 200 字记日志方便排错
                snippet = resp.text[:200] if resp.text else ""
                log.warning("FishAudio TTS API 错误: status=%d body=%s seq=%s attempt=%d",
                            resp.status_code, snippet, seq, attempt + 1)
        except Exception as e:
            # ReadTimeout 的 str 为空，补上类型名方便排错
            log.warning("FishAudio TTS 请求异常: %s%s seq=%s attempt=%d", type(e).__name__, f": {e}" if str(e) else "", seq, attempt + 1)
        await asyncio.sleep(2.0 * (attempt + 1))  # 代理抖动时给足恢复时间
    return None


# 阶跃星辰 Step TTS：step-tts-2 支持 voice_label（emotion + style）。
# emotion 从项目现有 9 值映射到中文标签；style 按场景选一个（prosody慢→慢速, whisper→温柔）。
_STEP_EMOTION_MAP = {
    "happy":     "高兴",
    "sad":       "悲伤",
    "angry":     "生气",
    "whisper":   "撒娇",
    "fearful":   "恐惧",
    "surprised": "惊讶",
}

async def _request_step_tts_audio(text: str, voice: str, *, seq: int | None = None, emotion: str = _DEFAULT_EMOTION, prosody: dict | None = None) -> bytes | None:
    """阶跃星辰 Step TTS（step-tts-2）。voice_label 传 emotion+style；prosody 映射 speed/volume。
    响应为原始 MP3 bytes。无 emotion 信号时不发 voice_label（保持默认语气）。"""
    key = get_key("step")
    if not key:
        log.warning("Step TTS: 无 API Key，跳过合成 seq=%s", seq)
        return None
    if not voice:
        log.warning("Step TTS: 未选择音色，跳过合成 seq=%s", seq)
        return None
    payload: dict = {
        "model": "step-tts-2",
        "input": text,
        "voice": voice,
        "response_format": "mp3",
    }
    # prosody → Step speed/volume
    speed = 1.0
    volume = 1.0
    if prosody:
        speed = prosody.get("speed", speed)
        volume = prosody.get("volume", volume)
    payload["speed"] = max(0.5, min(2.0, float(speed)))
    payload["volume"] = max(0.1, min(2.0, float(volume)))
    # voice_label：仅在有明确情绪信号时发送，避免传 null 触校验异常
    emo_tag = _STEP_EMOTION_MAP.get(emotion)
    style_tag = None
    # style 单选优先级：prosody 慢速 > whisper 温柔 > 无
    if prosody and prosody.get("speed", 1.0) < 1.0:
        style_tag = "慢速"
    elif emotion == "whisper":
        style_tag = "温柔"
    if emo_tag or style_tag:
        label: dict = {}
        if emo_tag:
            label["emotion"] = emo_tag
        if style_tag:
            label["style"] = style_tag
        payload["voice_label"] = label
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=15), trust_env=True) as client:
                resp = await client.post(
                    "https://api.stepfun.com/v1/audio/speech",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json=payload,
                )
            if resp.status_code == 200:
                if resp.content:
                    return resp.content
                log.warning("Step TTS: 响应为空 seq=%s attempt=%d", seq, attempt + 1)
            else:
                snippet = resp.text[:200] if resp.text else ""
                log.warning("Step TTS API 错误: status=%d body=%s seq=%s attempt=%d",
                            resp.status_code, snippet, seq, attempt + 1)
        except Exception as e:
            log.warning("Step TTS 请求异常: %s seq=%s attempt=%d", type(e).__name__, seq, attempt + 1)
        await asyncio.sleep(1.0 * (attempt + 1))
    return None


async def _request_minimax_tts_audio(text: str, voice: str, *, seq: int | None = None, emotion: str = _DEFAULT_EMOTION) -> bytes | None:
    """MiniMax T2A v2 合成。响应里音频是 hex 编码字符串，需 fromhex 解码。"""
    key = get_key("minimax")
    if not key:
        log.warning("MiniMax TTS: 无 API Key，跳过合成 seq=%s", seq)
        return None
    if not voice:
        log.warning("MiniMax TTS: 未选择音色，跳过合成 seq=%s", seq)
        return None
    payload = {
        # 男友陪伴向：HD 档拿质量；emotion 由 AI 自标，speed/pitch 保持温和青年男声基调
        "model": "speech-2.8-hd",
        "text": text,
        "stream": False,
        "voice_setting": {
            "voice_id": voice,
            "speed": 1,
            "vol": 1,
            "pitch": -1,
            "emotion": emotion,
        },
        "audio_setting": {"format": "mp3", "sample_rate": 32000, "bitrate": 128000},
    }
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                resp = await client.post(
                    "https://api.minimax.io/v1/t2a_v2",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json=payload,
                )
            if resp.status_code == 200:
                body = resp.json()
                # MiniMax 把业务错误包在 HTTP 200 里：{"base_resp":{"status_code":1004,"status_msg":"..."}}
                base = body.get("base_resp") or {}
                biz_code = base.get("status_code", 0)
                if biz_code and biz_code != 0:
                    log.warning("MiniMax TTS 业务错误: code=%s msg=%s voice=%s seq=%s attempt=%d",
                                biz_code, base.get("status_msg"), voice, seq, attempt + 1)
                else:
                    data = body.get("data") or {}
                    hex_audio = data.get("audio") or ""
                    if hex_audio:
                        return bytes.fromhex(hex_audio)
                    log.warning("MiniMax TTS: 响应无 audio 字段 seq=%s attempt=%d", seq, attempt + 1)
            else:
                log.warning("MiniMax TTS API 错误: status=%d seq=%s attempt=%d", resp.status_code, seq, attempt + 1)
        except Exception as e:
            log.warning("MiniMax TTS 请求异常: %s seq=%s attempt=%d", e, seq, attempt + 1)
        await asyncio.sleep(0.5 * (attempt + 1))
    return None


async def _request_tts_audio(text: str, voice: str, *, seq: int | None = None, emotion: str = _DEFAULT_EMOTION, provider: str | None = None, prosody: dict | None = None) -> bytes | None:
    from config import get_tts_provider
    provider = provider or get_tts_provider()
    if provider == "senseaudio":
        # SenseAudio 没有 emotion 参数，丢掉
        return await _request_senseaudio_tts_audio(text, voice, seq=seq)
    if provider == "minimax":
        return await _request_minimax_tts_audio(text, voice, seq=seq, emotion=emotion)
    if provider == "edge":
        # Edge TTS 无 emotion 字段，丢掉；无需 API Key
        return await _request_edge_tts_audio(text, voice, seq=seq)
    if provider == "fishaudio":
        # Fish Audio：prosody 控制语速音量；emotion 靠文本内表达（S2 自然语言/括号）
        return await _request_fishaudio_tts_audio(text, voice, seq=seq, emotion=emotion, prosody=prosody)
    if provider == "step":
        # 阶跃星辰 Step TTS：voice_label 传 emotion+style；prosody 映射 speed/volume
        return await _request_step_tts_audio(text, voice, seq=seq, emotion=emotion, prosody=prosody)
    key = get_key("siliconflow")
    if not key:
        log.warning("TTS: 无硅基流动 API Key，跳过合成 seq=%s", seq)
        return None

    resp = None
    for attempt in range(3):
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            resp = await client.post(
                "https://api.siliconflow.cn/v1/audio/speech",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": "FunAudioLLM/CosyVoice2-0.5B",
                    "input": text,
                    "voice": voice,
                    "response_format": "mp3",
                    "speed": 1.0,
                    "gain": 0
                }
            )
        if resp.status_code == 200:
            return resp.content
        log.warning("TTS API 错误: status=%d seq=%s attempt=%d", resp.status_code, seq, attempt + 1)
        await asyncio.sleep(0.5 * (attempt + 1))
    return None


async def synthesize_text_to_mp3(
    text: str,
    voice: str,
    output_path: Path,
    *,
    min_chars: int = 300,
    max_chars: int = 500,
    concurrency: int = 2,
    segment_prefix: str | None = None,
    cleanup_segments: bool = True,
    provider: str | None = None,
) -> dict:
    """Synthesize long text into one MP3 file by chunking and merging segments.
    provider: 强制使用的 TTS 服务商（如 'fishaudio'）；None 时走全局 get_tts_provider()。"""
    segments = split_text_for_tts(text, min_chars=min_chars, max_chars=max_chars)
    if not segments:
        raise ValueError("TTS text is empty")
    if not voice:
        raise ValueError("TTS voice is empty")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    prefix = re.sub(r'[^a-zA-Z0-9_\-]', '', segment_prefix or output_path.stem) or "tts"
    semaphore = asyncio.Semaphore(max(1, concurrency))
    created_paths: list[Path] = []

    async def _synthesize_segment(seq: int, segment: str) -> Path:
        async with semaphore:
            audio_data = await _request_tts_audio(segment, voice, seq=seq, provider=provider)
            if not audio_data:
                raise RuntimeError(f"TTS segment {seq} failed")
            path = output_path.parent / f"{prefix}_s{seq}.mp3"
            path.write_bytes(audio_data)
            created_paths.append(path)
            return path

    results = await asyncio.gather(
        *[_synthesize_segment(seq, segment) for seq, segment in enumerate(segments)],
        return_exceptions=True,
    )
    failures = [item for item in results if isinstance(item, Exception)]
    paths = [item for item in results if isinstance(item, Path)]
    if failures:
        for path in created_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise RuntimeError(f"TTS failed for {len(failures)} segment(s)") from failures[0]

    try:
        await asyncio.to_thread(TTSStreamer._merge_mp3_files, paths, output_path)
    finally:
        if cleanup_segments:
            for path in created_paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError as e:
                    log.warning("TTS segment cleanup failed for %s: %s", path, e)

    log.info("TTS long text merged: path=%s segments=%d", output_path.name, len(paths))
    return {"segments": len(paths), "chars": len(_strip_tags(text))}


async def synthesize_message_tts(msg_id: str, text: str, voice: str, ws_manager=None):
    """Synthesize an already-complete message and push normal tts_chunk events."""
    text = (text or "").strip()
    if not msg_id or not text or not voice:
        return
    streamer = TTSStreamer(msg_id, voice, ws_manager)
    streamer.feed(text)
    await streamer.flush()


def synthesize_message_tts_later(msg_id: str, text: str, voice: str, ws_manager=None):
    """Fire-and-forget TTS for messages created outside the normal streaming path."""
    text = (text or "").strip()
    if not msg_id or not text or not voice:
        return None
    task = asyncio.create_task(synthesize_message_tts(msg_id, text, voice, ws_manager))
    task.add_done_callback(_log_background_tts_failure)
    return task


class TTSStreamer:
    """服务端流式 TTS：积累文本 → 按句子切分 → 异步合成 → WS/Queue 推送"""

    def __init__(
        self,
        msg_id: str,
        voice: str,
        ws_manager=None,
        *,
        sse_queue: asyncio.Queue | None = None,
        min_chars: int = 100,
        max_chars: int = 200,
        cache_dir: Path | None = None,
        audio_url_prefix: str = "/api/tts/audio",
        merge_segments: bool = False,
        delete_segments_after_seconds: int | None = None,
        cache_max_bytes: int | None = TTS_CACHE_MAX_BYTES,
    ):
        self.msg_id = msg_id
        self.voice = voice
        self._ws = ws_manager
        self._sse_queue = sse_queue
        self._min_chars = max(1, min_chars)
        self._max_chars = max(self._min_chars, max_chars)
        self._cache_dir = cache_dir or TTS_CACHE_DIR
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._audio_url_prefix = audio_url_prefix.rstrip("/")
        self._buffer = ""       # 原始文本缓冲
        self._seq = 0           # 分段序号
        self._tasks: list[asyncio.Task] = []
        self._segment_paths: dict[int, Path] = {}
        self._merge_segments = merge_segments
        self._emotion = _DEFAULT_EMOTION  # 优先吃 AI 自标；没标就让后端从 buffer 推断
        self._emotion_locked = False       # True 后本条消息后续 chunk 不再重推断
        self._delete_segments_after_seconds = delete_segments_after_seconds
        self._cache_max_bytes = cache_max_bytes

    async def _notify(self, payload: dict):
        """通过 WebSocket 或 SSE Queue 推送事件"""
        if self._ws:
            if payload.get("type") in {"tts_chunk", "tts_done", "tts_merged"} and hasattr(self._ws, "send_tts_event"):
                await self._ws.send_tts_event(payload)
            else:
                await self._ws.broadcast(payload)
        if self._sse_queue:
            await self._sse_queue.put(payload)

    def feed(self, chunk: str):
        """喂入 AI 流式 chunk，检测到可切分的句子就异步发起合成"""
        if not chunk:
            return
        # 优先让 AI 自标的 <emotion> 锁定；没标就让后端从已积累的 buffer 推断一次，
        # 命中非默认信号后再锁。锁了之后本条消息剩下的 chunk 都用这个 emotion。
        if not self._emotion_locked:
            m = _EMOTION_PATTERN.search(chunk)
            if m:
                raw = m.group(1).strip().lower()
                if raw in _VALID_EMOTIONS:
                    self._emotion = raw
                    self._emotion_locked = True
                    chunk = _EMOTION_PATTERN.sub('', chunk)
            else:
                inferred = _infer_emotion_from_text(self._buffer + chunk)
                if inferred != _DEFAULT_EMOTION:
                    self._emotion = inferred
                    self._emotion_locked = True
        self._buffer += chunk
        self._try_split()

    def _try_split(self):
        """尝试从 buffer 中切出完整句子送去合成"""
        while True:
            # 有未闭合的标签，先不切
            if _has_unclosed_tag(self._buffer):
                break

            # 先清除标签，计算纯文本长度
            clean = _strip_tags(self._buffer)
            if len(clean) < self._min_chars:
                break

            cut_pos = self._find_cut_position()
            if cut_pos is None:
                break

            segment = self._buffer[:cut_pos + 1]
            self._buffer = self._buffer[cut_pos + 1:]

            cleaned = _strip_tags(segment)
            if cleaned.strip():
                self._dispatch(cleaned.strip())

    def _find_cut_position(self) -> int | None:
        """
        在原始 buffer 中找到切分位置。
        逻辑：纯文本到达 min_chars 后，开始找句号；最远到 max_chars，找逗号；还没有就强切。
        返回原始 buffer 中的切分索引。
        """
        return _find_cut_position_for_text(self._buffer, self._min_chars, self._max_chars)

    def _dispatch(self, text: str):
        """发起异步合成任务"""
        seq = self._seq
        self._seq += 1
        safe_id = re.sub(r'[^a-zA-Z0-9_\-]', '', self.msg_id)
        task = asyncio.create_task(self._synthesize(text, seq, safe_id))
        self._tasks.append(task)

    async def flush(self):
        """流结束后，处理 buffer 中剩余文本并等待所有合成任务完成"""
        remaining = _strip_tags(self._buffer).strip()
        if remaining:
            self._dispatch(remaining)
        self._buffer = ""

        # 等待所有合成任务完成
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        # 通知前端该消息的 TTS 分段已全部推送完毕
        await self._notify({
            "type": "tts_done",
            "data": {"msg_id": self.msg_id, "created_at": time.time()}
        })

        if self._merge_segments:
            asyncio.create_task(self._finalize_merged_audio())

    async def _finalize_merged_audio(self):
        safe_id = re.sub(r'[^a-zA-Z0-9_\-]', '', self.msg_id)
        if not safe_id:
            return
        expected = list(range(self._seq))
        if not expected:
            return
        paths = [self._segment_paths.get(seq) for seq in expected]
        if any(path is None or not path.exists() for path in paths):
            log.warning("TTS merge skipped for %s: missing one or more segments", self.msg_id)
            return

        merged_path = self._cache_dir / f"{safe_id}.mp3"
        try:
            await asyncio.to_thread(self._merge_mp3_files, paths, merged_path)
            await self._notify({
                "type": "tts_merged",
                "data": {
                    "msg_id": self.msg_id,
                    "url": f"{self._audio_url_prefix}/{safe_id}",
                    "created_at": time.time(),
                }
            })
            log.info("TTS merged audio ready: msg=%s segments=%d", self.msg_id, len(paths))
        except Exception as e:
            log.error("TTS merge failed for %s: %s", self.msg_id, e)
            return

        if self._delete_segments_after_seconds is not None:
            asyncio.create_task(self._delete_segments_later(paths, self._delete_segments_after_seconds))

    @staticmethod
    def _merge_mp3_files(paths: list[Path], merged_path: Path):
        tmp_path = merged_path.with_suffix(".tmp")
        with tmp_path.open("wb") as out:
            for path in paths:
                out.write(path.read_bytes())
        tmp_path.replace(merged_path)

    async def _delete_segments_later(self, paths: list[Path], delay_seconds: int):
        await asyncio.sleep(max(0, delay_seconds))
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError as e:
                log.warning("TTS delayed segment cleanup failed for %s: %s", path, e)

    async def _synthesize(self, text: str, seq: int, safe_id: str):
        """调用硅基流动 TTS 合成 → 保存文件 → WS 推送"""
        chunk_name = f"{safe_id}_s{seq}"
        try:
            audio_data = await _request_tts_audio(text, self.voice, seq=seq, emotion=self._emotion)
            if not audio_data:
                return

            cache_path = self._cache_dir / f"{chunk_name}.mp3"
            cache_path.write_bytes(audio_data)
            self._segment_paths[seq] = cache_path
            if self._cache_max_bytes and self._cache_dir.resolve() == TTS_CACHE_DIR.resolve():
                await asyncio.to_thread(cleanup_tts_cache_dir, self._cache_dir, self._cache_max_bytes, skip={cache_path})

            await self._notify({
                "type": "tts_chunk",
                "data": {
                    "msg_id": self.msg_id,
                    "seq": seq,
                    "url": f"{self._audio_url_prefix}/{chunk_name}",
                    "text": text,
                    "created_at": time.time(),
                }
            })
            log.info("TTS chunk pushed: msg=%s seq=%d len=%d", self.msg_id, seq, len(text))

        except Exception as e:
            log.error("TTS 合成失败 seq=%d: %s", seq, e)
