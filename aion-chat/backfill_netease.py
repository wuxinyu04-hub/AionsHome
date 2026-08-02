"""把存量哄睡音频补传到网易云云盘（一次性脚本）。

自动上传是 2026-08-02 才接上的，之前合成的 151 条从没上过云。
串行跑 + 失败退避重试，台账已记的跳过，中断后重跑自动续传。

用法：../.venv/Scripts/python.exe backfill_netease.py
"""

import json
import sqlite3
import sys
import time
from pathlib import Path

from config import SETTINGS
import sleep_upload

DATA_DIR = Path(__file__).parent / "data"


def main() -> int:
    sleep_upload.setup_sleep_upload(SETTINGS.get("netease_music_u", ""))
    if not getattr(sleep_upload, "_CACHED_LOGIN", None):
        print("网易云未登录（netease_music_u 可能过期），中止")
        return 1

    db = sqlite3.connect(DATA_DIR / "chat.db")
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute(
        "SELECT id,title,COALESCE(category,'') category FROM sleep_items "
        "WHERE COALESCE(audio_path,'')<>'' ORDER BY created_at")]
    db.close()

    led = {}
    p = DATA_DIR / "sleep_netease_uploaded.json"
    if p.exists():
        led = json.loads(p.read_text(encoding="utf-8"))

    todo = [r for r in rows if r["id"] not in led
            and (DATA_DIR / "sleep_tts_cache" / f"{r['id']}.mp3").exists()]
    print(f"能听 {len(rows)} 条，已传 {len(led)} 条，本次待传 {len(todo)} 条", flush=True)

    ok = skip = fail = 0
    for i, r in enumerate(todo, 1):
        title = (r["title"] or "")[:40]
        for attempt in range(3):
            try:
                res = sleep_upload.upload_finished_item(
                    r["id"], r["title"], r["category"]) or {}
                err = res.get("err", "")
                if res.get("ok"):
                    ok += 1
                    print(f"[{i}/{len(todo)}] ✓ {title} songId={res.get('song_id')}", flush=True)
                    break
                if err in ("already_uploaded", "file_not_found"):
                    skip += 1
                    print(f"[{i}/{len(todo)}] - 跳过 {title}（{err}）", flush=True)
                    break
                if err == "not_logged_in":
                    print("登录失效，中止（重新配置 netease_music_u 后重跑即可续传）", flush=True)
                    return 1
                # 其余是网络/限流类，交给外层退避重试
                raise RuntimeError(err or str(res)[:120])
            except Exception as e:
                if attempt < 2:
                    time.sleep(5 * (attempt + 1))  # 5s / 10s 退避，网易云会限流
                    continue
                fail += 1
                print(f"[{i}/{len(todo)}] ✗ {title}: {e}", flush=True)
        time.sleep(2)  # 每条之间歇一下，别把限流打爆

    print(f"\n完成：成功 {ok}，跳过 {skip}，失败 {fail}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
