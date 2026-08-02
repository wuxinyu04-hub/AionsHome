"""把云盘里的哄睡音频按书/分类归进网易云歌单。

跟 build_playlists.py 的区别：那个靠 data/sleep_export/ 算 md5 匹配，
这个直接用文件名里的 item_id 反查 DB —— 自动上传的文件名就是 <item_id>.mp3，
比算 154 个文件的 md5 又快又准，也不依赖 export 目录还在不在。

已存在的同名歌单直接复用（之前建过一批 0 首的空歌单），不重复建。

用法：../.venv/Scripts/python.exe sleep_playlists.py
"""

import json
import re
import sqlite3
import sys
import time
from collections import OrderedDict
from pathlib import Path

from config import SETTINGS
import sleep_upload

from pyncm.apis.cloud import GetCloudDriveInfo
from pyncm.apis.login import GetCurrentLoginStatus
from pyncm.apis.playlist import (GetPlaylistAllTracks, SetCreatePlaylist,
                                 SetManipulatePlaylistTracks)
from pyncm.apis.user import GetUserPlaylists

DATA_DIR = Path(__file__).parent / "data"
CAT_NAME = {"asmr": "ASMR 助眠", "boyfriend": "男友哄睡",
            "fairytale": "睡前童话", "meditation": "冥想引导",
            "reading": "散文朗读"}


def clean_book_title(t: str) -> str:
    """书名去掉 elib.cc 之类的下载站后缀，太长的截断。"""
    t = re.sub(r"[（(][^（）()]*elib\.cc[^（）()]*[）)]", "", t or "")
    t = re.sub(r"[（(][^（）()]{12,}[）)]", "", t)
    t = t.strip(" ·（）()")
    # 网易云歌单名有字数上限，超了报 400「标题字数异常」；按标点截到主书名
    if len(t) > 20:
        t = re.split(r"[：:—－\-]", t)[0].strip() or t[:20]
        t = t[:20]
    return t or "未命名"


def load_local() -> dict:
    """item_id -> 歌单名。讲书条目按书分组，其余按分类。"""
    db = sqlite3.connect(DATA_DIR / "chat.db")
    db.row_factory = sqlite3.Row
    books = {r["book_id"]: clean_book_title(r["title"])
             for r in db.execute("SELECT book_id,title FROM books")}
    out = {}
    for r in db.execute("SELECT id,COALESCE(category,'') c,COALESCE(book_ref,'') br "
                        "FROM sleep_items WHERE COALESCE(audio_path,'')<>''"):
        group = ""
        if r["br"]:
            try:
                group = books.get(json.loads(r["br"]).get("book_id"), "")
            except Exception:
                group = ""
        out[r["id"]] = group or CAT_NAME.get(r["c"], "助眠精选")
    db.close()
    return out


def fetch_cloud() -> list:
    all_items, off = [], 0
    while True:
        r = GetCloudDriveInfo(limit=200, offset=off)
        d = r.get("data") or []
        all_items += d
        off += len(d)
        if not d or off >= r.get("count", 0):
            break
        time.sleep(0.3)
    return all_items


def main() -> int:
    sleep_upload.setup_sleep_upload(SETTINGS.get("netease_music_u", ""))
    if not getattr(sleep_upload, "_CACHED_LOGIN", None):
        print("网易云未登录，中止")
        return 1

    local = load_local()
    cloud = fetch_cloud()
    print(f"本地能听 {len(local)} 条，云盘 {len(cloud)} 首", flush=True)

    groups = OrderedDict()
    matched = 0
    for s in cloud:
        fn = s.get("fileName") or ""
        item_id = fn[:-4] if fn.lower().endswith(".mp3") else fn
        g = local.get(item_id)
        if not g:
            continue
        matched += 1
        groups.setdefault(g, []).append(str(s.get("songId")))
    print(f"按 item_id 匹配上 {matched} 首，分 {len(groups)} 组\n", flush=True)
    if not groups:
        print("一首都没匹配上 —— 云盘里可能还没有自动上传的文件")
        return 1

    uid = GetCurrentLoginStatus().get("profile", {}).get("userId")
    existing = {}
    for p in GetUserPlaylists(uid, limit=200).get("playlist", []):
        existing.setdefault(p.get("name"), p.get("id"))

    for name, sids in groups.items():
        pid = existing.get(name)
        if pid:
            print(f"【{name}】{len(sids)} 首 -> 复用已有歌单 {pid}", flush=True)
        else:
            r = SetCreatePlaylist(name, privacy=False)
            pid = r.get("id")
            if not pid:
                print(f"【{name}】❌ 建歌单失败: {str(r)[:110]}", flush=True)
                continue
            print(f"【{name}】{len(sids)} 首 -> 新建歌单 {pid}", flush=True)
            time.sleep(0.5)

        # 歌单里已有的跳过，避免 op=add 重复报错
        try:
            have = {str(t.get("id")) for t in
                    (GetPlaylistAllTracks(pid, limit=1000).get("songs") or [])}
        except Exception:
            have = set()
        todo = [s for s in sids if s not in have]
        if not todo:
            print("   已是最新，跳过", flush=True)
            continue

        added = 0
        for i in range(0, len(todo), 100):
            batch = todo[i:i + 100]
            time.sleep(0.6)
            try:
                a = SetManipulatePlaylistTracks(batch, pid, op="add", imme=True, e_r=True)
                if a.get("code") in (200, None):
                    added += len(batch)
                else:
                    print(f"   ❌ 加歌: {str(a)[:110]}", flush=True)
            except Exception as e:
                print(f"   ❌ 加歌异常: {e}", flush=True)
        print(f"   加入 {added} 首", flush=True)

    print("\n完成。歌单默认公开，需要私密可在客户端改。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
