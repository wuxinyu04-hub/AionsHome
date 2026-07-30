"""
语音唤醒路由：开关控制 + 状态查询 + AI说话通知 + 远程ASR
"""

from fastapi import APIRouter, UploadFile, File
from pydantic import BaseModel
from typing import Optional
import httpx
import logging
import re

logger = logging.getLogger("voice_routes")

_EMOJI_RE = re.compile(
    "[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U000024C2-\U000024FF"
    "\U0001F170-\U0001F251"
    "\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF"
    "\U00002600-\U000026FF\U0000FE00-\U0000FE0F\U0000200D]+"
)

from voice import voice
from config import get_key

router = APIRouter()


class VoiceToggle(BaseModel):
    enabled: bool
    wake_word: str = "老公"


class AISpeakingNotify(BaseModel):
    speaking: bool


@router.get("/api/voice/status")
async def voice_status():
    return {
        "enabled": voice.enabled,
        "in_call": voice.in_call,
        "ai_speaking": voice.ai_speaking,
        "wake_word": voice.wake_word,
    }


@router.post("/api/voice/toggle")
async def voice_toggle(body: VoiceToggle):
    if body.enabled:
        voice.start(body.wake_word)
    else:
        voice.stop()
    return {"ok": True, "enabled": voice.enabled}


@router.post("/api/voice/ai-speaking")
async def voice_ai_speaking(body: AISpeakingNotify):
    """前端通知：AI TTS 播放状态"""
    voice.notify_ai_speaking(body.speaking)
    return {"ok": True}


@router.post("/api/voice/cam-check-start")
async def voice_cam_check_start():
    """前端通知：AI 触发了 CAM_CHECK"""
    voice.notify_cam_check_start()
    return {"ok": True}


ASR_URL = "https://api.stepfun.com/v1/audio/transcriptions"
ASR_MODEL = "stepaudio-2.5-asr"


@router.post("/api/voice/remote-asr")
async def remote_asr(file: UploadFile = File(...)):
    """远程 ASR：接收手机端录音，调 Step ASR 返回文本"""
    key = get_key("step")
    if not key:
        return {"text": "", "error": "No step key"}
    content = await file.read()
    logger.info("RemoteASR received %d bytes filename=%s", len(content), file.filename)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                ASR_URL,
                headers={"Authorization": f"Bearer {key}"},
                files={"file": ("audio.wav", content, "audio/wav")},
                data={"model": ASR_MODEL, "response_format": "json"},
                timeout=15,
            )
            resp.raise_for_status()
            result = resp.json()
            raw_text = result.get("text", "").strip()
            text = _EMOJI_RE.sub("", raw_text).strip()
            logger.info("RemoteASR result=%r", text)
            return {"text": text}
    except Exception as e:
        # 异常细节只进日志，不回吐给前端（可能含内部 URL/状态）
        logger.warning("RemoteASR failed: %s: %s", type(e).__name__, e)
        return {"text": "", "error": "识别服务异常，稍后再试"}


@router.post("/api/voice/transcribe")
async def transcribe_voice_message(file: UploadFile = File(...)):
    """语音消息转写：接收上传的音频文件，调 Step ASR 返回文本"""
    key = get_key("step")
    if not key:
        return {"text": "", "error": "No step key"}
    content = await file.read()
    mime = file.content_type or "audio/webm"
    ext = file.filename.rsplit(".", 1)[-1] if file.filename and "." in file.filename else "webm"
    logger.info("VoiceTranscribe received %d bytes mime=%s ext=%s", len(content), mime, ext)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                ASR_URL,
                headers={"Authorization": f"Bearer {key}"},
                files={"file": (f"voice.{ext}", content, mime)},
                data={"model": ASR_MODEL, "response_format": "json"},
                timeout=30,
            )
            resp.raise_for_status()
            result = resp.json()
            raw_text = result.get("text", "").strip()
            text = _EMOJI_RE.sub("", raw_text).strip()
            logger.info("VoiceTranscribe result=%r", text)
            return {"text": text}
    except Exception as e:
        # 异常细节只进日志，不回吐给前端
        logger.warning("VoiceTranscribe failed: %s: %s", type(e).__name__, e)
        return {"text": "", "error": "识别服务异常，稍后再试"}
