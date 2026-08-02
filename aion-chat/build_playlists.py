"""用云盘里的歌自动建歌单（一本/一类一个歌单），先建公开再设为私密"""
import hashlib, io, json, re, sqlite3, time, sys
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).parent))
from pyncm.apis.login import LoginViaCookie
from pyncm.apis.cloud import GetCloudDriveInfo
from pyncm.apis.playlist import SetCreatePlaylist, SetManipulatePlaylistTracks
from pyncm import GetCurrentSession
from pyncm.utils.crypto import EapiEncrypt
from random import randrange
from requests import post

S = json.loads((Path(__file__).parent / "data" / "settings.json").read_text(encoding="utf-8"))
LoginViaCookie(MUSIC_U=S.get("netease_music_u", "").strip())
ses = GetCurrentSession()

def eapi_set_privacy(pl_id, val):
    """改歌单隐私: 0=公开 1=私密"""
    plain = {"id": str(pl_id), "privacy": str(val),
             "header": json.dumps({**ses.eapi_config, "requestId": str(randrange(20000000, 30000000))})}
    digest = EapiEncrypt("pyncm", json.dumps(plain))["params"]
    r = post("https://music.163.com/eapi/playlist/update/privacy", data=digest,
             headers={"User-Agent": ses.UA_DEFAULT, "Referer": "https://music.163.com"},
             cookies={**ses.eapi_config}, timeout=15)
    from pyncm.utils.crypto import EapiDecrypt
    try:
        txt = EapiDecrypt(r.content).decode()
        return json.loads(txt) if txt.strip() else {}
    except:
        return {}

# ── 1. 干净书名 ──
db = sqlite3.connect("data/chat.db")
db.row_factory = sqlite3.Row
clean_books = {}
for b in db.execute("SELECT book_id, title FROM books").fetchall():
    t = re.sub(r"[（(].*?elib\.cc.*?[）)]", "", b["title"]).strip(" （）() ")
    if t.startswith("被讨厌的勇气"):
        t = "被讨厌的勇气"
    clean_books[b["book_id"]] = t

CAT_NAME = {"asmr": "ASMR 助眠", "boyfriend": "男友哄睡",
            "fairytale": "睡前童话", "meditation": "冥想引导", "reading": "散文朗读"}

def group_for_folder(folder: str) -> str:
    if folder in CAT_NAME:
        return CAT_NAME[folder]
    if folder.startswith("青春咖啡馆"):
        return "青春咖啡馆"
    if folder.startswith("一个人的朝圣"):
        return "一个人的朝圣"
    for bid, bt in clean_books.items():
        if folder.startswith(bt[:6]) or bt.startswith(folder[:6]):
            return bt
    return folder

# ── 2. 本地 MD5 -> group ──
export = Path("data/sleep_export")
md5_to_group = {}
for mp3 in export.rglob("*.mp3"):
    data = mp3.read_bytes()
    md5 = hashlib.md5(data).hexdigest()
    rel = mp3.relative_to(export).parts
    if len(rel) >= 2:
        md5_to_group.setdefault(md5, group_for_folder(rel[0]))
    else:
        md5_to_group.setdefault(md5, "助眠场景")

# ── 3. 读云盘 ──
cloud = []
offset = 0
while True:
    resp = GetCloudDriveInfo(limit=100, offset=offset)
    data = resp.get("data", []) if isinstance(resp, dict) else []
    songs = data if isinstance(data, list) else []
    if not songs: break
    cloud.extend(songs)
    if len(songs) < 100: break
    offset += 100
print(f"云盘 {len(cloud)} 首歌", flush=True)

# ── 4. 分组 ──
from collections import OrderedDict
groups = OrderedDict()
unmatched = []
seen = set()
for s in cloud:
    pc = s.get("privateCloud") or {}
    md5 = pc.get("md5") or pc.get("md") or ""
    sid = str(s.get("songId"))
    if sid in seen: continue
    seen.add(sid)
    if md5 in md5_to_group:
        groups.setdefault(md5_to_group[md5], []).append(sid)
    else:
        unmatched.append((s.get("songName",""), md5[:8]))

print(f"未匹配 {len(unmatched)} 首，重复已去重", flush=True)

# ── 5. 建歌单 + 加歌 ──
results = []
for g, sids in groups.items():
    print(f"\n【{g}】{len(sids)} 首", flush=True)
    # 建公开歌单（privacy=0 一定成功）
    try:
        r = SetCreatePlaylist(g, privacy=False)
        pid = r.get("id")
        if pid is None:
            print(f"  ❌ 建歌单失败: {r}", flush=True)
            continue
        # 加歌
        added = 0
        for i in range(0, len(sids), 100):
            batch = sids[i:i+100]
            time.sleep(0.5)
            add = SetManipulatePlaylistTracks(batch, pid, op="add", imme=True, e_r=True)
            if add.get("code") in (200, None):
                added += len(batch)
            else:
                print(f"  ❌ 加歌: {add}", flush=True)
        print(f"  歌单 {pid} 加入 {added} 首", flush=True)
        # 设为私密
        time.sleep(0.3)
        priv = eapi_set_privacy(pid, "1")
        pcode = priv.get("code", "?") if isinstance(priv, dict) else "err"
        print(f"  设私密 -> code={pcode}", flush=True)
        time.sleep(2)  # 慢一点防风控
        results.append((g, pid, len(sids)))
    except Exception as e:
        print(f"  ❌ 异常: {e}", flush=True)

print(f"\n=== 完成 ===")
print(f"建了 {len(results)} 个歌单")
for g, pid, n in results:
    print(f"  {g}: {pid}（{n} 首）")
