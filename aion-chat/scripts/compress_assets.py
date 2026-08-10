# -*- coding: utf-8 -*-
"""一次性资源压缩：图标缩到长边 128px PNG(保透明)，背景缩到长边 1920 JPEG q78。
原地覆盖同名文件；覆盖前先备份原文件到 public/_orig_backup/。
跑法:  python aion-chat/scripts/compress_assets.py
"""
import sys
from pathlib import Path
from PIL import Image

PUBLIC = Path(__file__).resolve().parent.parent.parent / "public"
BACKUP = PUBLIC / "_orig_backup"

ICON_MAX = 128          # 图标长边
BG_MAX = 1920           # 背景长边
JPEG_Q = 78

# 要压缩的文件名（相对 public/）
ICON_PATTERNS = ["funIcon_*.png", "music-icon.png", "sleep-icon.png"]
BG_FILES = ["BackGround.jpg", "BackGroundN.jpg"]


def collect_files():
    files = []
    for pat in ICON_PATTERNS:
        files.extend(sorted(PUBLIC.glob(pat)))
    for name in BG_FILES:
        f = PUBLIC / name
        if f.exists():
            files.append(f)
    return files


def backup(f: Path):
    BACKUP.mkdir(parents=True, exist_ok=True)
    dst = BACKUP / f.name
    if dst.exists():
        return  # 已备份过，不覆盖备份
    dst.write_bytes(f.read_bytes())


def compress_icon(f: Path):
    """PNG：缩到长边 128，转 8bit palette 量化，保留透明（二值 alpha）。
    卡通图标硬边透明，量化到 256 色 + 单透明索引，体积远小于 RGBA。"""
    img = Image.open(f).convert("RGBA")
    w, h = img.size
    scale = min(1.0, ICON_MAX / max(w, h))
    if scale < 1.0:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                         Image.LANCZOS)
    r, g, b, a = img.split()
    # 二值化 alpha：>32 视为不透明，其余完全透明
    a = a.point(lambda v: 255 if v > 32 else 0, mode="L")
    # 透明像素在 RGB 上填一个固定色（magenta），便于量化后定位透明索引
    rgb = Image.merge("RGBA", (r, g, b, a)).convert("RGB")
    rgb.paste((255, 0, 255), (0, 0), Image.eval(a, lambda v: 0 if v else 255))
    p = rgb.quantize(colors=255, method=Image.MEDIANCUT, dither=0)
    # 找到 magenta 的索引设为透明
    magenta_idx = None
    pal = p.getpalette()  # [r,g,b, r,g,b, ...]
    for i in range(len(pal) // 3):
        rr, gg, bb = pal[i * 3], pal[i * 3 + 1], pal[i * 3 + 2]
        if rr == 255 and gg == 0 and bb == 255:
            magenta_idx = i
            break
    if magenta_idx is not None:
        p.info["transparency"] = magenta_idx
    p.save(f, format="PNG", optimize=True, transparency=magenta_idx)


def compress_bg(f: Path):
    """JPEG 背景缩到长边 1920 q78。"""
    img = Image.open(f).convert("RGB")
    w, h = img.size
    scale = min(1.0, BG_MAX / max(w, h))
    if scale < 1.0:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                         Image.LANCZOS)
    img.save(f, format="JPEG", quality=JPEG_Q, optimize=True, progressive=True)


def human(n):
    for u in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}GB"


def main():
    files = collect_files()
    if not files:
        print("没有找到待压缩资源，检查 PUBLIC 路径:", PUBLIC)
        sys.exit(1)
    bgs = set(BG_FILES)
    total_before = 0
    total_after = 0
    for f in files:
        before = f.stat().st_size
        backup(f)
        if f.name in bgs:
            compress_bg(f)
        else:
            compress_icon(f)
        after = f.stat().st_size
        total_before += before
        total_after += after
        print(f"{f.name:40s} {human(before):>10s} -> {human(after):>10s}  "
              f"({100*after/before:.0f}%)")
    print("-" * 70)
    print(f"合计 {len(files)} 个文件：{human(total_before)} -> {human(total_after)}"
          f"  压缩到 {100*total_after/total_before:.0f}%")
    print(f"原文件备份于：{BACKUP}")


if __name__ == "__main__":
    main()
