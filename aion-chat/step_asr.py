"""Step 阶跃星辰 ASR —— 流式识别端点的共用封装。

为什么单独一个模块：voice.py 的唤醒词、routes/voice.py 的手机远程录音和语音
消息转写，三处都要调 ASR，之前各自复制了一份 multipart 请求 + emoji 清洗。
8/2 那次改 URL 只改到其中一处的经历说明这种复制迟早出事，统一到这里。

计费线（重要）：必须走 step_plan 前缀 = 订阅套餐；裸 /v1 扣按量余额会欠费。
注意端点名不是 transcriptions —— step_plan 线的 ASR 只有 asr/sse 这一个路径，
`step_plan/v1/audio/transcriptions` 是不存在的（返回纯文本 404）。

    能力              请求方式   Step Plan 路径
    语音识别(流式)     POST      /step_plan/v1/audio/asr/sse

请求体是 JSON + base64 音频，响应是 SSE 流（text/event-stream）。
format.type 传容器名（wav/webm/mp3/...）时服务端自己嗅探，传 pcm 时必须
同时给 codec/rate/bits/channel，因为裸 PCM 没有头部可嗅。
"""

import base64
import json
import re
import time

import httpx

from config import get_key

ASR_URL = "https://api.stepfun.com/step_plan/v1/audio/asr/sse"
ASR_MODEL = "stepaudio-2.5-asr"

# 识别结果里偶尔混进 emoji，进唤醒词匹配和聊天记录之前先剃掉。
#
# 千万别把区间写成 \U000024C2-\U0001F251：那一段横跨整个 CJK 汉字区
# （U+4E00–U+9FFF），会把中文全剃成空串——唤醒词「哥哥」永远匹配不上。
# voice.py 的旧版就是这么写的，8/2 排查唤醒词才发现。区间必须逐段列，
# 每段都不能跨过 U+4E00–U+9FFF。
_EMOJI_RE = re.compile(
    "["
    "\U0000200D"              # ZWJ
    "\U0000FE00-\U0000FE0F"   # 变体选择符
    "\U00002600-\U000027BF"   # 杂项符号 + 装饰符号（含 ✂ ➰）
    "\U000024C2"              # Ⓜ
    "\U00002B00-\U00002BFF"   # 箭头等杂项
    "\U0001F000-\U0001F0FF"   # 麻将/扑克
    "\U0001F170-\U0001F251"   # 带圈字母/表意文字补充
    "\U0001F300-\U0001F5FF"   # 杂项符号与象形文字
    "\U0001F600-\U0001F64F"   # 表情
    "\U0001F680-\U0001F6FF"   # 交通与地图
    "\U0001F1E0-\U0001F1FF"   # 区域指示符（国旗）
    "\U0001F900-\U0001F9FF"   # 补充象形文字
    "\U0001FA00-\U0001FAFF"   # 扩展 A
    "]+"
)


def pcm_format(rate: int, channels: int = 1) -> dict:
    """裸 int16 PCM 的 format（唤醒词那条路：sounddevice 直接给的就是这个）"""
    return {
        "type": "pcm",
        "codec": "pcm_s16le",
        "rate": rate,
        "bits": 16,
        "channel": channels,
    }


def container_format(kind: str = "wav") -> dict:
    """带容器的音频文件（wav/webm/mp3/m4a/ogg...），服务端按头部自己嗅探"""
    return {"type": kind}


def build_body(audio: bytes, fmt: dict, language: str = "zh") -> dict:
    return {
        "audio": {
            "data": base64.b64encode(audio).decode(),
            "input": {
                "transcription": {
                    "model": ASR_MODEL,
                    "language": language,
                    "enable_itn": True,
                },
                "format": fmt,
            },
        }
    }


def parse_sse(raw: str) -> str:
    """从 SSE 流里取最终文本。

    正常情况下 transcript.text.done 带完整 text；万一流被截断只收到 delta，
    就把 delta 拼起来兜底，总比丢掉整句强。
    """
    deltas = []
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            ev = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "transcript.text.done":
            return (ev.get("text") or "").strip()
        if ev.get("type") == "transcript.text.delta":
            deltas.append(ev.get("delta") or "")
    return "".join(deltas).strip()


def _headers(key: str) -> dict:
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }


def _clean(text: str) -> str:
    return _EMOJI_RE.sub("", text).strip()


def transcribe(audio: bytes, fmt: dict, *, timeout: float = 20, log=print) -> str:
    """同步识别，失败返回空串。

    网络类错误（DNS/连接/超时）重试一次：唤醒词是一次性机会，一次抖动就丢掉
    整声呼唤（8/2 14:56 的 getaddrinfo failed）。HTTP 状态类错误（key 无效/
    欠费/参数错）不重试，重试只会重复计费。
    """
    key = get_key("step")
    if not key:
        return ""
    body = build_body(audio, fmt)
    # trust_env=False：stepfun 国内直连，走系统代理会 SSL handshake 超时（直连 1s / 代理 5~7s 且掉包）
    for attempt in (1, 2):
        try:
            with httpx.Client(timeout=httpx.Timeout(timeout, connect=10), trust_env=False) as client:
                resp = client.post(ASR_URL, headers=_headers(key), json=body)
            resp.raise_for_status()
            return _clean(parse_sse(resp.text))
        except httpx.HTTPStatusError as e:
            log(f"[Step ASR] HTTP {e.response.status_code}（不重试）: {e.response.text[:200]}")
            return ""
        except (httpx.TransportError, OSError) as e:
            log(f"[Step ASR] {type(e).__name__}: {e}（第 {attempt}/2 次）")
            if attempt == 1:
                time.sleep(0.6)
        except Exception as e:
            log(f"[Step ASR] {type(e).__name__}: {e}")
            return ""
    return ""


async def transcribe_async(audio: bytes, fmt: dict, *, timeout: float = 30) -> str:
    """异步识别，失败抛异常交给调用方处理（路由层要区分错误回吐给前端）"""
    key = get_key("step")
    if not key:
        raise RuntimeError("No step key")
    body = build_body(audio, fmt)
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10), trust_env=False) as client:
        resp = await client.post(ASR_URL, headers=_headers(key), json=body)
        resp.raise_for_status()
        return _clean(parse_sse(resp.text))
