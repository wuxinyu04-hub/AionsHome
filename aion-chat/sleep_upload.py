"""网易云云盘上传模块：合成完成后的后台自动上传。

解耦设计：合成完成即丢给后台线程上传，上传是否成功不影响「合成」状态。
失败只记日志（sleep_upload_log.jsonl），不报错、不重试循环。

用法：
    from sleep_upload import setup_sleep_upload, upload_finished_item
    setup_sleep_upload(music_u)                     # 应用启动时调用一次
    upload_finished_item(item_id, title, voice, ...)  # _synthesize_bg 成功后调用
"""
import hashlib, json, logging, time
from pathlib import Path
from datetime import datetime

from pyncm.apis.login import LoginViaCookie
from pyncm.apis.cloud import (
    GetNosToken, SetUploadObject, GetCheckCloudUpload,
    SetUploadCloudInfo, SetPublishCloudResource,
)

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
SETTINGS_PATH = DATA_DIR / "settings.json"
LEDGER_PATH = DATA_DIR / "netease_uploaded.json"
LOG_PATH = DATA_DIR / "sleep_upload_log.jsonl"
AUDIO_DIR = DATA_DIR / "sleep_tts_cache"

log = logging.getLogger("sleep_upload")

_CAT_NAME = {
    "asmr": "ASMR 助眠", "boyfriend": "男友哄睡", "fairytale": "睡前童话",
    "meditation": "冥想引导", "reading": "散文朗读",
}
_CACHED_LOGIN = False


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
        return {"ok": True, "song_id": pub_id, "err": ""}

    except Exception as e:
        _log_event(item_id, "fail", str(e)[:200])
        return {"ok": False, "song_id": "", "err": f"exception: {e}"}
