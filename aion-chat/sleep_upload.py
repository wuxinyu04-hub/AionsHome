"""网易云云盘上传模块：合成完成后的后台自动上传。

解耦设计：合成完成即丢给后台线程上传，上传是否成功不影响「合成」状态。
失败只记日志（sleep_upload_log.jsonl），不报错、不重试循环。

用法：
    from sleep_upload import setup_sleep_upload, upload_finished_item
    setup_sleep_upload(music_u)                     # 应用启动时调用一次
    upload_finished_item(item_id, title, voice, ...)  # _synthesize_bg 成功后调用
"""
import hashlib, json, logging, sqlite3, time
from pathlib import Path
from datetime import datetime

from pyncm.apis.login import LoginViaCookie, GetCurrentLoginStatus
from pyncm.apis.cloud import (
    GetNosToken, SetUploadObject, GetCheckCloudUpload,
    SetUploadCloudInfo, SetPublishCloudResource,
)
from pyncm.apis.playlist import SetCreatePlaylist, SetManipulatePlaylistTracks
from pyncm.apis.user import GetUserPlaylists

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
SETTINGS_PATH = DATA_DIR / "settings.json"
# 独立台账：旧 CLI 工具 upload_to_netease_cloud.py 用 netease_uploaded.json，
# key 是目录路径；这里 key 是 item_id。共用一个文件会让两套 key 混在一起互相误判。
LEDGER_PATH = DATA_DIR / "sleep_netease_uploaded.json"
LOG_PATH = DATA_DIR / "sleep_upload_log.jsonl"
AUDIO_DIR = DATA_DIR / "sleep_tts_cache"

log = logging.getLogger("sleep_upload")

_CAT_NAME = {
    "asmr": "ASMR 助眠", "boyfriend": "男友哄睡", "fairytale": "睡前童话",
    "meditation": "冥想引导", "reading": "散文朗读",
}
_CACHED_LOGIN = False
# 手动上传运行态：item_id -> {status: running|done, ok, song_id, err}。
# 自动上传（合成完）不记这里，只有手动按钮触发的进内存，供前端轮询。
_manual: dict[str, dict] = {}


def _log_event(item_id: str, status: str, detail: str = "") -> None:
    """追加一条日志，便于排查。"""
    try:
        record = json.dumps(
            {"ts": datetime.now().isoformat(timespec="seconds"),
             "id": item_id, "status": status, "detail": detail},
            ensure_ascii=False)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(record + "\n")
    except Exception as e:
        log.debug("写上传日志失败: %s", e)


def setup_sleep_upload(music_u: str = "") -> None:
    """应用启动时调用：用 netease_music_u 登录网易云（全局一次）。"""
    global _CACHED_LOGIN
    if _CACHED_LOGIN:
        return
    mu = (music_u or "").strip()
    if not mu:
        # 没有配 Cookie：模块可用，但所有上传会直接失败并记日志
        log.info("sleep_upload 未配置 netease_music_u，上传将被跳过")
        _CACHED_LOGIN = True
        return
    try:
        LoginViaCookie(MUSIC_U=mu)
        _CACHED_LOGIN = True
        log.info("sleep_upload 登录网易云成功")
    except Exception as e:
        log.warning("sleep_upload 登录网易云失败: %s", e)
        _CACHED_LOGIN = True  # 不要反复登录


def _load_ledger() -> dict:
    try:
        return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_ledger(led: dict) -> None:
    try:
        LEDGER_PATH.write_text(json.dumps(led, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        log.warning("写上传台账失败: %s", e)


def _clean_book_title(t: str) -> str:
    """书名去掉 elib.cc 之类的下载站后缀，太长的截断。同步自 sleep_playlists.py"""
    import re
    t = re.sub(r"[（(][^（）()]*elib\.cc[^（）()]*[）)]", "", t or "")
    t = re.sub(r"[（(][^（）()]{12,}[）)]", "", t)
    t = t.strip(" ·（）()")
    if len(t) > 20:
        t = re.split(r"[：:—－\-]", t)[0].strip() or t[:20]
        t = t[:20]
    return t or "未命名"


def _get_playlist_name(item_id: str, category: str) -> str:
    """从 DB 推导歌单名，讲书条目按书分组，其余按分类。"""
    try:
        db = sqlite3.connect(DATA_DIR / "chat.db")
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT COALESCE(book_ref,'') br FROM sleep_items WHERE id=?", (item_id,)
        ).fetchone()
        if not row:
            return _CAT_NAME.get(category, "助眠精选")
        book_ref = row["br"]
        if book_ref:
            import json as j
            book_id = j.loads(book_ref).get("book_id")
            if book_id:
                br = db.execute("SELECT title FROM books WHERE book_id=?", (book_id,)).fetchone()
                if br:
                    db.close()
                    return _clean_book_title(br["title"])
        db.close()
    except Exception as e:
        log.debug("_get_playlist_name 查 DB 失败: %s", e)
    return _CAT_NAME.get(category, "助眠精选")


def _add_to_playlist(item_id: str, song_id: str, category: str) -> None:
    """把刚传的歌加到对应歌单（按书/分类归组）。失败不报错，只记日志。"""
    name = _get_playlist_name(item_id, category)
    uid = GetCurrentLoginStatus().get("profile", {}).get("userId")
    if not uid:
        raise RuntimeError("GetCurrentLoginStatus 未拿到 userId")

    # 找或建歌单
    existing = {}
    for p in GetUserPlaylists(uid, limit=200).get("playlist", []):
        existing.setdefault(p.get("name"), p.get("id"))
    pid = existing.get(name)
    if not pid:
        r = SetCreatePlaylist(name, privacy=False)
        pid = r.get("id")
        if not pid:
            raise RuntimeError(f"SetCreatePlaylist 失败: {str(r)[:110]}")
        time.sleep(0.5)

    # 查已有，避免重复加歌
    from pyncm.apis.playlist import GetPlaylistAllTracks
    try:
        have = {str(t.get("id")) for t in
                (GetPlaylistAllTracks(pid, limit=1000).get("songs") or [])}
    except Exception:
        have = set()
    if song_id in have:
        return  # 已在歌单里，跳过

    time.sleep(0.6)
    a = SetManipulatePlaylistTracks([song_id], pid, op="add", imme=True, e_r=True)
    if a.get("code") not in (200, None):
        raise RuntimeError(f"SetManipulatePlaylistTracks: {str(a)[:110]}")


def upload_finished_item(
    item_id: str,
    title: str = "",
    category: str = "",
    artist: str = "温叙远",
    album: str = "",
) -> dict:
    """
    上传合成完成的一项到网易云云盘。返回 {ok, song_id, err}。
    调用方应丢给 asyncio.to_thread / executor 执行，不要让它在合成主线程里阻塞。
    """
    # 台账防重复：按 item_id 去重（比按路径稳，同一本可能改标题）
    led = _load_ledger()
    if item_id in led:
        _log_event(item_id, "skip-duplicate", f"已传 songId={led.get(item_id)}")
        return {"ok": False, "song_id": led.get(item_id), "err": "already_uploaded"}

    if not _CACHED_LOGIN:
        _log_event(item_id, "skip-no-login", "未配置 netease_music_u")
        return {"ok": False, "song_id": "", "err": "not_logged_in"}

    mp3 = AUDIO_DIR / f"{item_id}.mp3"
    if not mp3.exists():
        _log_event(item_id, "skip-missing", "mp3 文件不存在")
        return {"ok": False, "song_id": "", "err": "file_not_found"}

    data = mp3.read_bytes()
    file_size = len(data)
    md5 = hashlib.md5(data).hexdigest()
    filename = mp3.name
    ext = "mp3"

    song = title or item_id
    song = song.replace(".", "·").replace("/", "·").replace("\n", " ")[:120]

    album = (album or _CAT_NAME.get(category, "") or "哄睡")[:60]

    _log_event(item_id, "start", f"{filename} {file_size // 1024}KB md5={md5[:8]}")
    try:
        token = GetNosToken(filename=filename, md5=md5, fileSize=str(file_size), ext=ext)
        if token.get("code") != 200:
            _log_event(item_id, "fail", f"GetNosToken: {str(token)[:120]}")
            return {"ok": False, "song_id": "", "err": "nos_token"}
        rid = token.get("result", {}).get("resourceId", "")
        okey = token.get("result", {}).get("objectKey", "")
        tok = token.get("result", {}).get("token", "")
        if not all([rid, okey, tok]):
            _log_event(item_id, "fail", "nos_token 缺字段")
            return {"ok": False, "song_id": "", "err": "nos_token"}

        up = SetUploadObject(stream=data, md5=md5, fileSize=str(file_size),
                             objectKey=okey, token=tok)
        if not (up.get("code") == 200 or "requestId" in up):
            _log_event(item_id, "fail", f"SetUploadObject: {str(up)[:120]}")
            return {"ok": False, "song_id": "", "err": "nos_upload"}

        check = GetCheckCloudUpload(md5=md5, ext=ext, length=file_size, bitrate=128)
        if check.get("code") != 200:
            _log_event(item_id, "fail", f"GetCheckCloudUpload: {str(check)[:120]}")
            return {"ok": False, "song_id": "", "err": "check"}

        info = SetUploadCloudInfo(resourceId=rid, songid=str(check.get("songId", "")),
                                  md5=md5, filename=filename, song=song,
                                  artist=artist, album=album, bitrate=128)
        if info.get("code") != 200:
            _log_event(item_id, "fail", f"SetUploadCloudInfo: {str(info)[:120]}")
            return {"ok": False, "song_id": "", "err": "cloud_info"}

        pub_id = str(info.get("songId") or info.get("songIdLong") or "")
        if not pub_id.isdigit():
            _log_event(item_id, "fail", f"cloud_info 未拿到 songId: {str(info)[:120]}")
            return {"ok": False, "song_id": "", "err": "no_songid"}

        max_wait = min(300, max(40, int(file_size / 1024 / 1024) * 12))
        pub = {}
        waited = 0
        while True:
            pub = SetPublishCloudResource(songid=pub_id)
            if pub.get("code") in (200, 201):
                break
            if waited >= max_wait:
                break
            time.sleep(6)
            waited += 6
        if pub.get("code") not in (200, 201):
            _log_event(item_id, "fail", f"publish {str(pub)[:120]}")
            return {"ok": False, "song_id": "", "err": "publish"}

        led[item_id] = pub_id
        _save_ledger(led)
        _log_event(item_id, "ok", f"songId={pub_id}")
        # 顺手归歌单：进云盘只能在「云盘」里翻，归了歌单才好找。
        # 失败不影响上传结果，隔阵子跑 sleep_playlists.py 能补齐。
        try:
            _add_to_playlist(item_id, pub_id, category)
        except Exception as e:
            _log_event(item_id, "playlist-fail", str(e)[:160])
        return {"ok": True, "song_id": pub_id, "err": ""}

    except Exception as e:
        _log_event(item_id, "fail", str(e)[:200])
        return {"ok": False, "song_id": "", "err": f"exception: {e}"}


def get_netease_status(item_id: str, category: str = "") -> dict:
    """查询一条的上传状态（播放页按钮展示用）。

    已传：ledger 有 song_id + 反算歌单名；未传看手动运行态（running/err）。
    不抛异常，找不到也返回 uploaded=False。
    """
    led = _load_ledger()
    song_id = str(led.get(item_id) or "")
    uploaded = bool(song_id)
    m = _manual.get(item_id) or {}
    return {
        "uploaded": uploaded,
        "song_id": song_id,
        "playlist_name": _get_playlist_name(item_id, category) if uploaded else "",
        "running": m.get("status") == "running",
        "err": m.get("err", ""),
    }


def start_manual_upload(item_id: str, title: str = "", category: str = "") -> dict:
    """手动触发上传（播放页按钮）。同步阻塞，调用方应丢 asyncio.to_thread。

    已传直接返回，不重复传；上传中返回 uploading。失败写 _manual done（含 err），
    不写台账，下次可重试。
    """
    led = _load_ledger()
    if item_id in led:
        _log_event(item_id, "skip-duplicate", f"手动触发但已传 songId={led.get(item_id)}")
        return {"ok": False, "song_id": led.get(item_id), "err": "already_uploaded"}
    if (_manual.get(item_id) or {}).get("status") == "running":
        return {"ok": False, "song_id": "", "err": "uploading"}
    _manual[item_id] = {"status": "running"}
    res = upload_finished_item(item_id, title, category)
    _manual[item_id] = {
        "status": "done",
        "ok": bool(res.get("ok")),
        "song_id": res.get("song_id", ""),
        "err": res.get("err", ""),
    }
    return res
