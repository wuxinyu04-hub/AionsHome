"""
阶跃 StepAudio 2.5 Realtime 全双工语音引擎（临时方案，默认关闭）。

与 voice.py 的 VoiceWakeup 外部接口完全一致（enabled / in_call / ai_speaking /
wake_word + start / stop / notify_ai_speaking / notify_cam_check_start /
set_event_loop / set_ws_manager），由 settings.json 的 voice_realtime_enabled
开关经 voice.py 底部 get_voice() 工厂选择。开关关 = 原 voice.py 管线逐字节不变。

工作方式：
- 唤醒阶段复用 VoiceWakeup：16k 麦克风 + WebRTC VAD + Step ASR 检测唤醒词"老公"。
- 唤醒后建一条 StepFun Realtime WebSocket：24k 麦克风流式 input_audio_buffer.append，
  服务端 VAD 断句自动触发推理，response.audio.delta（base64 PCM16）经 WS 广播回前端播放。
  全双工，AI 说话时可打断。
- 挂断：用户说"再见/拜拜/挂断..."（conversation.item.input_audio_transcription.completed
  事件带用户转写）或前端挂断按钮（通话中再次 start() = 挂断当前通话）。
- 大脑是 step 音频模型自己，不走项目聊天管线（丢记忆/上下文，人设靠 instructions 灌）。
"""

import json, base64, threading, asyncio, traceback

import sounddevice as sd
import websockets

from voice import VoiceWakeup, HANGUP_KEYWORDS, SAMPLE_RATE, CHANNELS, VAD_FRAME_SIZE, MAX_SILENCE_FRAMES
from config import get_key, get_voice_realtime_config

# 与 tts.py:385 _STEP_DAILY_INSTRUCTION 一致（主聊天 daily 风格），此处内联避免引入 tts 重依赖。
_DAILY_STYLE = "自然说话，年上温润的男性嗓音，像平时聊天，松弛不刻意。"


class RealtimeVoice(VoiceWakeup):
    """阶跃 StepAudio 2.5 Realtime 全双工语音引擎（默认关闭，可随时回退）。"""

    def __init__(self):
        super().__init__()
        self.realtime = True
        self._ws = None
        self._rt_loop: asyncio.AbstractEventLoop = None
        self._audio_queue: asyncio.Queue = None
        self._stream24 = None

    # ── 外部控制 ──────────────────────────────────

    def start(self, wake_word: str = "老公"):
        """开启监听；通话中再次 start()（前端挂断按钮路径）= 挂断当前通话回到待命。

        线程已在跑时也要更新 wake_word——前端改唤醒词走的是同一个 toggle 接口，
        早期版本在这里直接 return，新唤醒词被静默丢弃（改「哥哥」叫不应的根因）。
        """
        if self._thread and self._thread.is_alive():
            self.wake_word = (wake_word or "").strip() or self.wake_word
            if self.in_call:
                self._request_hangup()
            return
        self.wake_word = (wake_word or "").strip() or "老公"
        self.enabled = True
        self.in_call = False
        self.ai_speaking = False
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._main_loop, daemon=True)
        self._thread.start()
        self._broadcast_state("voice_state", {"enabled": True, "status": "calibrating"})

    def stop(self):
        """关闭语音监听，全部状态复位。"""
        self.enabled = False
        self._stop_evt.set()
        self._request_hangup()
        self.ai_speaking = False
        self._broadcast_state("voice_state", {"enabled": False, "status": "off"})

    def _request_hangup(self):
        """结束当前 realtime 通话（线程安全：在 rt 事件循环上调度 ws 关闭，unblock 收消息循环）。"""
        self.in_call = False
        if self._ws and self._rt_loop:
            try:
                asyncio.run_coroutine_threadsafe(self._ws.close(), self._rt_loop)
            except Exception:
                pass

    def notify_ai_speaking(self, speaking: bool):
        """realtime 全双工下无 TTS 播放，仅保留标志位供接口兼容。"""
        self.ai_speaking = speaking

    def notify_cam_check_start(self):
        pass

    # ── 主循环：唤醒词监听 + 通话 ──────────────────

    def _main_loop(self):
        stream16 = None
        try:
            stream16 = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=CHANNELS,
                dtype="int16", blocksize=VAD_FRAME_SIZE
            )
            stream16.start()
            self._stream = stream16
            print("[RealtimeVoice] 唤醒监听已启动（16k WebRTC VAD）")
            self._broadcast_state("voice_state", {"enabled": True, "status": "waiting", "wake_word": self.wake_word})

            while not self._stop_evt.is_set():
                audio = self._record(stream16, MAX_SILENCE_FRAMES)
                if audio is None or self._stop_evt.is_set():
                    continue
                text = self._asr(audio)
                if not text or self.wake_word not in text:
                    continue
                print(f"[RealtimeVoice] 唤醒词命中: {text}")
                self.in_call = True
                self.ai_speaking = False
                asyncio.run(self._realtime_call())
                self.in_call = False
                self.ai_speaking = False
                if not self._stop_evt.is_set():
                    print("[RealtimeVoice] 通话结束，回到唤醒监听")
                    self._broadcast_state("voice_state", {"enabled": True, "status": "waiting", "wake_word": self.wake_word})
        except Exception as e:
            print(f"[RealtimeVoice] Error: {e}")
            traceback.print_exc()
        finally:
            if stream16:
                try:
                    stream16.stop()
                    stream16.close()
                except Exception:
                    pass
            self._stream = None
            self.enabled = False
            self.in_call = False
            self._broadcast_state("voice_state", {"enabled": False, "status": "off"})

    # ── Realtime 通话 ─────────────────────────────

    @staticmethod
    def _greeting() -> str:
        return "宝宝，我在呢。想你了，陪你说会儿话吧。"

    @staticmethod
    def _build_instructions(cfg) -> str:
        from config import load_worldbook
        parts = []
        persona = (load_worldbook().get("ai_persona") or "").strip()
        if persona:
            parts.append(persona)
        parts.append(_DAILY_STYLE)
        parts.append("你是温叙远，我的男朋友。说话要温柔、简短，像枕边耳语，一轮一两句，不要长篇大论。")
        return "\n\n".join(parts)

    async def _realtime_call(self):
        cfg = get_voice_realtime_config()
        key = get_key("step")
        if not key:
            print("[RealtimeVoice] 无 step key，退出通话")
            return
        self._rt_loop = asyncio.get_running_loop()
        try:
            async with websockets.connect(
                cfg["ws_url"],
                additional_headers={"Authorization": f"Bearer {key}"},
                open_timeout=15,
            ) as ws:
                self._ws = ws
                await ws.send(json.dumps({
                    "type": "session.update",
                    "session": {
                        "modalities": ["text", "audio"],
                        "instructions": self._build_instructions(cfg),
                        "voice": cfg["voice"],
                        "input_audio_format": "pcm16",
                        "output_audio_format": "pcm16",
                        "turn_detection": {"type": "server_vad"},
                    },
                }))
                await self._wait_for(ws, {"session.updated"})
                self._broadcast_state("voice_state", {
                    "enabled": True, "status": "wakeup", "realtime": True,
                    "message": "唤醒成功！"
                })
                # 开场白：让温叙远先开口（官方文档写法：response.create 携带 session.instructions）
                try:
                    await ws.send(json.dumps({
                        "type": "response.create",
                        "session": {"instructions": "请你原样无修改地输出下面的话：" + self._greeting()},
                    }))
                except Exception as e:
                    print(f"[RealtimeVoice] 开场白发送失败(忽略): {e}")
                mic_task = asyncio.create_task(self._stream_mic(cfg))
                try:
                    await self._recv_loop(ws)
                finally:
                    mic_task.cancel()
                    try:
                        await mic_task
                    except asyncio.CancelledError:
                        pass
                self._ws = None
                self._rt_loop = None
        except websockets.WebSocketException as e:
            print(f"[RealtimeVoice] WS 异常: {type(e).__name__}: {e}")
            if self.in_call:
                self._broadcast_state("voice_state", {"enabled": True, "status": "hangup", "message": "通话连接断开"})
        except Exception as e:
            print(f"[RealtimeVoice] 通话异常: {e}")
            traceback.print_exc()
            if self.in_call:
                self._broadcast_state("voice_state", {"enabled": True, "status": "hangup", "message": "通话异常结束"})

    async def _wait_for(self, ws, types):
        """等指定类型的首个服务端事件（容忍 error / 未知事件）。"""
        for _ in range(20):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=8)
            except asyncio.TimeoutError:
                continue
            except websockets.WebSocketException:
                return
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            t = ev.get("type", "")
            if t in types:
                return
            if t == "error":
                print(f"[RealtimeVoice] session.update error: {ev.get('message')}")

    async def _stream_mic(self, cfg):
        """24k 麦克风流式送 input_audio_buffer.append（20ms/块，官方建议小步快送）。"""
        rate = cfg["sample_rate"]
        q = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _cb(indata, frames, time_info, status):
            try:
                loop.call_soon_threadsafe(q.put_nowait, indata.copy())
            except RuntimeError:
                pass

        def _open():
            return sd.InputStream(
                samplerate=rate, channels=CHANNELS, dtype="int16",
                blocksize=max(320, rate // 50), callback=_cb,
            )

        stream = await asyncio.to_thread(_open)
        try:
            stream.start()
            self._stream24 = stream
            while self.in_call and not self._stop_evt.is_set():
                try:
                    data = await asyncio.wait_for(q.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                b64 = base64.b64encode(data.tobytes()).decode("ascii")
                try:
                    await self._ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": b64}))
                except websockets.WebSocketException:
                    break
        finally:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
            self._stream24 = None

    async def _recv_loop(self, ws):
        """收消息循环：音频回传 + 状态广播 + 挂断检测。"""
        cfg = get_voice_realtime_config()
        ai_transcript = ""
        while self.in_call and not self._stop_evt.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=3)
            except asyncio.TimeoutError:
                continue
            except websockets.ConnectionClosed:
                if self.in_call:
                    print("[RealtimeVoice] WS 连接意外断开")
                    self._broadcast_state("voice_state", {"enabled": True, "status": "hangup", "message": "通话连接断开"})
                break
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            t = ev.get("type", "")
            if t == "response.audio.delta":
                self._broadcast_state("voice_audio_delta", {
                    "base64": ev.get("delta", ""),
                    "sample_rate": cfg["sample_rate"],
                })
            elif t == "response.audio_transcript.delta":
                ai_transcript += ev.get("delta", "")
                self._broadcast_state("voice_state", {"enabled": True, "status": "ai_speaking", "realtime": True})
            elif t == "response.done":
                ai_transcript = ""
                self._broadcast_state("voice_state", {"enabled": True, "status": "listening_cmd", "realtime": True, "message": "聆听中..."})
            elif t == "input_audio_buffer.speech_started":
                self._broadcast_state("voice_state", {"enabled": True, "status": "listening_cmd", "realtime": True, "message": "在听你说..."})
            elif t == "input_audio_buffer.speech_stopped":
                self._broadcast_state("voice_state", {"enabled": True, "status": "ai_thinking", "realtime": True, "message": "AI 思考中..."})
            elif t == "conversation.item.input_audio_transcription.completed":
                user_text = (ev.get("transcript") or "").strip()
                print(f"[RealtimeVoice] 用户说: {user_text}")
                if user_text and any(kw in user_text for kw in HANGUP_KEYWORDS):
                    print("[RealtimeVoice] 检测到挂断词")
                    self.in_call = False
                    break
            elif t == "error":
                print(f"[RealtimeVoice] error: {ev.get('message')}")


# 全局单例（get_voice() 工厂在 voice_realtime_enabled=True 时返回它）
voice = RealtimeVoice()
print("[Voice] 使用阶跃 Realtime 引擎（voice_realtime_enabled=True）")
