"""哄睡音频囤货工具：整本书串行生成、断点续跑、可读文件名导出。

默认处理当前库里的全部书；也可以传 --book-id 只跑一本。
"""
import argparse
import io
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# 修复 Windows GBK 编码问题
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

_AUTH_FILE = Path(__file__).parent / "data" / "auth_secret.txt"
AUTH_TOKEN = _AUTH_FILE.read_text(encoding="utf-8").strip()
BASE = "http://localhost:8080"
DEFAULT_VOICE = "cixingnansheng"
HEADERS = {
    "Content-Type": "application/json",
    "X-Aion-Token": AUTH_TOKEN,
}


def api(method, path, data=None, timeout=120):
    req = urllib.request.Request(f"{BASE}{path}", method=method, headers=HEADERS)
    if data is not None:
        req.data = json.dumps(data, ensure_ascii=False).encode("utf-8")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(raw or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"error": body}


def poll_item(item_id, timeout=900):
    start = time.time()
    while time.time() - start < timeout:
        code, d = api("GET", f"/api/sleep/{item_id}/status")
        status = d.get("status")
        if status in ("ready", "failed"):
            return status, d
        detail = d.get("progress_detail") or status or "waiting"
        pct = d.get("progress_pct")
        if pct is not None:
            print(f"    {pct}% {detail}", flush=True)
        time.sleep(5)
    return "timeout", {}


def load_books(book_id=""):
    if book_id:
        code, d = api("GET", f"/api/books/{book_id}")
        if code != 200:
            raise RuntimeError(d.get("detail") or d.get("error") or f"book {book_id} not found")
        return [d]
    code, d = api("GET", "/api/books")
    if code != 200:
        raise RuntimeError(d.get("detail") or d.get("error") or "failed to load books")
    return d.get("books") or []


def load_chapters(book_id):
    code, d = api("GET", f"/api/books/{book_id}")
    if code != 200:
        raise RuntimeError(d.get("detail") or d.get("error") or f"failed to load chapters: {book_id}")
    return d.get("chapters") or []


def load_existing():
    code, lib = api("GET", "/api/sleep/library")
    existing = {}
    priority = {"": 0, "failed": 1, "generating": 2, "synthesizing": 2, "ready": 3}
    if code == 200:
        for it in lib.get("items", []):
            if it.get("book_id") and it.get("book_chapter", -1) >= 0:
                key = (it["book_id"], it.get("book_chapter"))
                status = it.get("status") or ""
                if priority.get(status, 0) >= priority.get(existing.get(key, ""), 0):
                    existing[key] = status
    return existing


def generate_chapter(book, chapter, voice, timeout):
    bid = book["book_id"]
    ch_idx = chapter["chapter_index"]
    print(f"[GEN] {book.get('title') or bid} 第 {ch_idx + 1} 章 {chapter.get('title') or ''}", flush=True)
    code, d = api("POST", "/api/sleep/generate", {
        "category": "reading",
        "prompt": "",
        "voice": voice,
        "book_id": bid,
        "chapter_index": ch_idx,
    })
    if code != 200:
        err = d.get("detail") or d.get("error") or str(d)
        print(f"  [FAIL] 创建失败: {err}", flush=True)
        return False
    item_id = d.get("id")
    print(f"  id={item_id} 等待合成...", flush=True)
    status, detail = poll_item(item_id, timeout)
    if status == "ready":
        print("  [OK] 完成", flush=True)
        return True
    err = detail.get("error") or status
    print(f"  [FAIL] {err}", flush=True)
    return False


def export_audio(book_id=""):
    code, d = api("POST", "/api/sleep/export", {"book_id": book_id})
    if code != 200:
        print(f"[EXPORT FAIL] {d.get('detail') or d.get('error') or d}", flush=True)
        return
    print(f"[EXPORT] {d.get('total', 0)} 个文件 -> {d.get('export_dir')}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book-id", default="", help="只处理某一本书")
    ap.add_argument("--voice", default=DEFAULT_VOICE, help="TTS voice/reference_id")
    ap.add_argument("--timeout", type=int, default=900, help="每章等待秒数")
    ap.add_argument("--export-only", action="store_true", help="只导出，不生成")
    ap.add_argument("--no-export", action="store_true", help="生成后不导出")
    args = ap.parse_args()

    if args.export_only:
        export_audio(args.book_id)
        return

    books = load_books(args.book_id)
    existing = load_existing()
    ok = fail = skip = empty = 0
    for book in books:
        bid = book["book_id"]
        for ch in load_chapters(bid):
            ch_idx = ch["chapter_index"]
            if not ch.get("char_count", 0):
                empty += 1
                continue
            status = existing.get((bid, ch_idx))
            if status in ("ready", "generating", "synthesizing"):
                skip += 1
                print(f"[SKIP] {book.get('title')} 第 {ch_idx + 1} 章 已{status}", flush=True)
                continue
            if generate_chapter(book, ch, args.voice, args.timeout):
                ok += 1
            else:
                fail += 1

    print("\n===== 完成 =====", flush=True)
    print(f"成功: {ok} / 失败: {fail} / 跳过: {skip} / 空章节: {empty}", flush=True)
    if not args.no_export:
        export_audio(args.book_id)


if __name__ == "__main__":
    main()
