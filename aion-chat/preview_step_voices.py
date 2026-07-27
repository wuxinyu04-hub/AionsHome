"""stepaudio-2.5-tts 主聊天情绪 instruction 测试。

daily 基础 instruction + 情绪描述，测 stepaudio 能否用 instruction 控情绪、自不自然。
确认后改 tts.py：emotion 推断结果 -> instruction 情绪文案（替代 voice_label）。

运行：.venv\\Scripts\\python.exe preview_step_voices.py
"""
import asyncio
import os
import sys
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from config import get_key

OUT_DIR = HERE / "step_voice_preview"
OUT_DIR.mkdir(exist_ok=True)

DAILY = "自然说话，年上温润的男性嗓音，像平时聊天，松弛不刻意。"

# (标签, 音色, 文本, instruction, speed)
PREVIEW = [
    ("聊天_calm", "cixingnansheng", "回来了？先洗手，汤我热着呢，今天累不累。", DAILY, 1.0),
    ("聊天_撒娇", "cixingnansheng", "小乖，过来，让我抱抱，别皱眉了。", DAILY + "语气撒娇，带点气声，轻一点。", 1.0),
    ("聊天_高兴", "cixingnansheng", "今天遇到个好玩的事，想跟你说说。", DAILY + "语气带点笑意，开心一点。", 1.0),
    ("聊天_难过", "cixingnansheng", "今天有点累，不太开心，想你了。", DAILY + "语气低沉一点，难过。", 1.0),
]

sem = asyncio.Semaphore(3)


async def synth_one(idx, label, voice, text, instr, speed):
    print(f"[{idx+1}/{len(PREVIEW)}] {label} ...")
    payload = {
        "model": "stepaudio-2.5-tts",
        "input": text,
        "response_format": "mp3",
        "speed": speed,
        "voice": voice,
        "instruction": instr[:200],
    }
    async with sem:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15), trust_env=True) as client:
                resp = await client.post(
                    "https://api.stepfun.com/v1/audio/speech",
                    headers={"Authorization": f"Bearer {get_key('step')}", "Content-Type": "application/json"},
                    json=payload,
                )
            if resp.status_code == 200 and resp.content:
                fn = OUT_DIR / f"{label}.mp3"
                fn.write_bytes(resp.content)
                print(f"  OK {fn.name}")
            else:
                print(f"  X {label}: status={resp.status_code} body={resp.text[:200]}")
        except Exception as e:
            print(f"  X {label}: {type(e).__name__}: {e}")


async def main():
    if not get_key("step"):
        print("X 未配置 step_key")
        return
    await asyncio.gather(*[synth_one(i, *p) for i, p in enumerate(PREVIEW)])
    print(f"\n完成：{OUT_DIR}")
    try:
        os.startfile(str(OUT_DIR))
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(main())
