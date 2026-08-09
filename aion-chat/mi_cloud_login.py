"""小米运动健康小号扫码登录脚本。

生成二维码 → 用手机【系统相机】扫（小米运动健康 App 的扫一扫不支持）
→ 确认登录 → token 存到 aion-chat/data/mi_cloud_token.json

用法：
    python mi_cloud_login.py
二维码会写到 aion-chat/public/mi_cloud_qr.png（手机浏览器打开同网段 URL 即可扫），
并把登录 URL 一起打印出来。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
PUBLIC_DIR = Path(__file__).parent.parent / "public"
QR_PATH = PUBLIC_DIR / "mi_cloud_qr.png"
TOKEN_PATH = DATA_DIR / "mi_cloud_token.json"


async def main() -> None:
    try:
        from mi_fitness import XiaomiAuth
    except ImportError:
        print("缺少 mi-fitness：先 `pip install mi-fitness`")
        sys.exit(1)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)

    # 每轮二维码 ticket 只有 ~5 分钟生命。用户操作慢会赶不上，
    # 所以循环刷新：过期就重新生成，直到登录成功为止。
    URL_PATH = DATA_DIR / "mi_cloud_login_url.txt"

    async def on_qr(qr_image_url: str, login_url: str) -> None:
        print("=" * 50)
        print("登录 URL：", login_url)
        print("=" * 50, flush=True)
        URL_PATH.write_text(login_url, encoding="utf-8")
        # 下载二维码图片到 public/ 供手机浏览器直接打开
        import httpx

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(qr_image_url)
                QR_PATH.write_bytes(resp.content)
                print(f"\n二维码图片已存到 public/mi_cloud_qr.png", flush=True)
        except Exception as e:
            print(f"[warn] 下载二维码失败，但可以直接用上面的 URL：{e}", flush=True)

    async with XiaomiAuth() as auth:
        attempt = 0
        while True:
            attempt += 1
            print(f"\n[第 {attempt} 次] 已生成新登录链接，5 分钟内有效，请尽快打开确认…", flush=True)
            try:
                token = await auth.login_qr(qr_callback=on_qr)
                break
            except Exception as e:
                if "超时" in str(e) or "timeout" in str(e).lower():
                    print(f"[第 {attempt} 次] 链接过期，自动刷新…", flush=True)
                    continue
                raise
        auth.save_token(str(TOKEN_PATH))
        # 避免 Windows GBK 控制台对 emoji 抛 UnicodeEncodeError
        print("\n[OK] 登录成功！token 已保存到", TOKEN_PATH, flush=True)
        print("   用户 UID：", token.user_id, flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已取消")
