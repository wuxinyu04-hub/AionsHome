"""
语音唤醒路由：开关控制 + 状态查询 + AI说话通知 + 远程ASR
"""

from fastapi import APIRouter, UploadFile, File
from pydantic import BaseModel
from typing import Optional
import logging

from voice import get_voice
from config import get_voice_wake_word, save_voice_wake_word
import step_asr

logger = logging.getLogger("voice_routes")

router = APIRouter()

# 引擎按 settings.json 的 voice_realtime_enabled 选择（默认原半双工 VoiceWakeup）
voice = get_voice()
# 唤醒词落盘在 settings.json，进程起来先恢复，否则重启后回落默认「老公」跟前端对不上
voice.wake_word = get_voice_wake_word()


class VoiceToggle(BaseModel):
    enabled: bool
    wake_word: Optional[str] = None


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
    """开关语音监听。带 wake_word 时同步落盘，重启后自动恢复。

    前端改唤醒词也走这个接口（enabled=true 重发），所以 start() 必须能在
    监听线程已存活的情况下更新 wake_word——见 voice.py / voice_realtime.py 的 start()。
    """
    word = save_voice_wake_word(body.wake_word) if body.wake_word else get_voice_wake_word()
    if body.enabled:
        voice.start(word)
    else:
        voice.stop()
        voice.wake_word = word
    return {"ok": True, "enabled": voice.enabled, "wake_word": voice.wake_word}


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


@router.post("/api/voice/remote-asr")
async def remote_asr(file: UploadFile = File(...)):
    """远程 ASR：接收手机端录音，调 Step ASR 返回文本"""
    content = await file.read()
    logger.info("RemoteASR received %d bytes filename=%s", len(content), file.filename)
    try:
        text = await step_asr.transcribe_async(content, step_asr.container_format("wav"), timeout=20)
        logger.info("RemoteASR result=%r", text)
        return {"text": text}
    except Exception as e:
        # 异常细节只进日志，不回吐给前端（可能含内部 URL/状态）
        logger.warning("RemoteASR failed: %s: %s", type(e).__name__, e)
        return {"text": "", "error": "识别服务异常，稍后再试"}


@router.post("/api/voice/transcribe")
async def transcribe_voice_message(file: UploadFile = File(...)):
    """语音消息转写：接收上传的音频文件，调 Step ASR 返回文本"""
    content = await file.read()
    mime = file.content_type or "audio/webm"
    ext = file.filename.rsplit(".", 1)[-1].lower() if file.filename and "." in file.filename else "webm"
    logger.info("VoiceTranscribe received %d bytes mime=%s ext=%s", len(content), mime, ext)
    try:
        # 浏览器录的多是 webm/mp4，容器名交给服务端嗅探（实测 wav/webm/mp3/m4a/ogg 都收）
        text = await step_asr.transcribe_async(content, step_asr.container_format(ext), timeout=30)
        logger.info("VoiceTranscribe result=%r", text)
        return {"text": text}
    except Exception as e:
        # 异常细节只进日志，不回吐给前端
        logger.warning("VoiceTranscribe failed: %s: %s", type(e).__name__, e)
        return {"text": "", "error": "识别服务异常，稍后再试"}
