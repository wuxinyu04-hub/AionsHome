"""精准创建并建满 10 个独立歌单（带频次控制与重试）"""
import hashlib, io, json, sqlite3, time, sys
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).parent))

from pyncm.apis.login import LoginViaCookie
from pyncm.apis.cloud import GetCloudDriveInfo
from pyncm.apis.playlist import SetCreatePlaylist, SetManipulatePlaylistTracks, GetPlaylistInfo

S = json.load(open("data/settings.json", encoding="utf-8"))
LoginViaCookie(MUSIC_U=S.get("netease_music_u", "").strip())

db = sqlite3.connect("data/chat.db")
db.row_factory = sqlite3.Row

books = {b["book_id"]: b["title"] for b in db.execute("SELECT book_id, title FROM books").fetchall()}

def clean_title(t):
    if "青春咖啡馆" in t: return "青春咖啡馆"
    if "被讨厌的勇气" in t: return "被讨厌的勇气"
    if "故宫" in t: return "故宫的古物之美"
    if "朝圣" in t: return "一个人的朝圣"
    if "城与城" in t: return "城与城"
    return t

cat_names = {
    "asmr": "ASMR 助眠",
    "fairytale": "睡前童话",
    "meditation": "冥想引导",
    "boyfriend": "男友哄睡",
    "reading": "散文朗读"
}

md5_to_playlist = {}

items = db.execute("""
    SELECT id, category, title, book_ref, voice, audio_path
    FROM sleep_items
    WHERE status='ready' AND audio_path != ''
""").fetchall()

for item in items:
    ap = item["audio_path"]
    src = Path("data") / ap
    if not src.exists(): continue
    md5 = hashlib.md5(src.read_bytes()).hexdigest()

    b_ref = item["book_ref"] or ""
    b_id = ""
    if b_ref:
        try:
            b_id = json.loads(b_ref).get("book_id", "")
        except: pass

    cat = item["category"] or "sleep"
    bt = books.get(b_id, "")

    if bt:
        target_playlist = clean_title(bt)
    else:
        target_playlist = cat_names.get(cat, "其他场景")

    md5_to_playlist[md5] = target_playlist

for mp3 in Path("data/sleep_export").rglob("*.mp3"):
    md5 = hashlib.md5(mp3.read_bytes()).hexdigest()
    if md5 not in md5_to_playlist:
        rel = mp3.relative_to(Path("data/sleep_export")).parts
        if len(rel) >= 2:
            folder = rel[0]
            if folder in cat_names:
                md5_to_playlist[md5] = cat_names[folder]
            else:
                md5_to_playlist[md5] = clean_title(folder)

cloud_songs = []
offset = 0
while True:
    resp = GetCloudDriveInfo(limit=100, offset=offset)
    data = resp.get("data", []) if isinstance(resp, dict) else []
    songs = data if isinstance(data, list) else []
    if not songs: break
    cloud_songs.extend(songs)
    if len(songs) < 100: break
    offset += 100

playlist_songs = {}
for s in cloud_songs:
    sid = str(s.get("songId"))
    pc = s.get("privateCloud") or {}
    md5 = pc.get("md5") or pc.get("md") or ""

    target_pl = md5_to_playlist.get(md5)
    if target_pl:
        playlist_songs.setdefault(target_pl, []).append(sid)
    elif "青春咖啡馆" in s.get("songName", ""):
        playlist_songs.setdefault("青春咖啡馆", []).append(sid)

def create_playlist_with_retry(name):
    for attempt in range(10):
        res = SetCreatePlaylist(name, privacy=False)
        if res.get("code") == 200 and res.get("id"):
            return res.get("id")
        print(f"  [Wait 10s] 创建歌单 '{name}' 触发风控/频控 (code: {res.get('code')})...")
        time.sleep(10)
    return None

new_playlists = []
for pl_name, sids in sorted(playlist_songs.items()):
    unique_sids = list(dict.fromkeys(sids))
    print(f"\n[Playlist] {pl_name} ({len(unique_sids)} tracks)")

    pid = create_playlist_with_retry(pl_name)
    if not pid:
        print(f"  ❌ 彻底失败")
        continue
    print(f"  ✅ 成功创建歌单 ID: {pid}")

    for i in range(0, len(unique_sids), 50):
        batch = unique_sids[i:i+50]
        add_res = SetManipulatePlaylistTracks(batch, pid, op="add", imme=True, e_r=True)
        print(f"  Added batch {i//50 + 1}: {len(batch)} songs (code: {add_res.get('code')})")
        time.sleep(1)
    new_playlists.append((pl_name, pid, len(unique_sids)))
    time.sleep(5)

print("\n===== 歌单创建与填充完成 =====")
for name, pid, count in new_playlists:
    print(f"{name:15s} | ID: {pid} | {count:2d} 首 | 链接: https://music.163.com/#/playlist?id={pid}")
