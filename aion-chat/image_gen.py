"""
AI 生图模块：Gemini gemini-3.1-flash-lite-image 生成图片
支持 SELFIE（带参考图）和 DRAW（纯文本）两种模式
"""

import base64, re, time
from pathlib import Path

import httpx

from config import get_key, get_leesai_keys, UPLOADS_DIR, PUBLIC_DIR
from ai_providers import _make_http_client, _openai_chat_completions_url

# LeesAiHub：OpenAI 兼容生图站，gpt-image-2，一个 key 约 30 张额度
LEESAI_BASE_URL = "https://leesapihome.ccwu.cc/v1"
LEESAI_IMAGE_MODEL = "gpt-image-2"

# 参考图位置（用于 SELFIE 模式）
REFERENCE_IMAGE_PATH = PUBLIC_DIR / "生图锚点.jpg"
SECONDARY_REFERENCE_IMAGE_PATH = PUBLIC_DIR / "2号机生图锚点.jpg"
IMAGE_GEN_MODEL = "gemini-3.1-flash-lite-image"
IMAGE_GEN_TIMEOUT = 120  # 生图超时秒数


def _selfie_reference_path(source_identity: str = "") -> Path:
    """Return the SELFIE anchor by stable internal actor identity, not display name."""
    if str(source_identity or "").strip().lower() == "connor":
        return SECONDARY_REFERENCE_IMAGE_PATH
    return REFERENCE_IMAGE_PATH


async def generate_image(prompt: str, is_selfie: bool = False, source_identity: str = "") -> str | None:
    """
    调用 Gemini 生图模型生成图片，保存到 uploads 目录，返回文件名。
    is_selfie=True 时自动附带参考图。
    失败返回 None。
    """
    api_key = get_key("gemini")
    if not api_key:
        print("[image_gen] 没有 Gemini API Key，无法生图")
        return None

    # 构建请求内容
    parts = [{"text": prompt}]

    # SELFIE 模式：附带参考图
    if is_selfie:
        reference_image_path = _selfie_reference_path(source_identity)
        if reference_image_path.exists():
            ref_bytes = reference_image_path.read_bytes()
            ref_b64 = base64.b64encode(ref_bytes).decode("utf-8")
            parts.append({
                "inlineData": {
                    "mimeType": "image/jpeg",
                    "data": ref_b64
                }
            })
            print(f"[image_gen] SELFIE 模式，已附带参考图: {reference_image_path}")
        else:
            print(f"[image_gen] 参考图不存在: {reference_image_path}，降级为 DRAW 模式")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{IMAGE_GEN_MODEL}:generateContent?key={api_key}"

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "responseModalities": ["IMAGE", "TEXT"],
        },
        "safetySettings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ]
    }

    try:
        async with _make_http_client(url, timeout=IMAGE_GEN_TIMEOUT) as client:
            print(f"[image_gen] 开始生图... prompt: {prompt[:80]}")
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()

            # 解析响应，提取图片
            candidates = data.get("candidates", [])
            if not candidates:
                error_msg = data.get("error", {}).get("message", "未知错误")
                print(f"[image_gen] API 返回空 candidates: {error_msg}")
                return None

            content_parts = candidates[0].get("content", {}).get("parts", [])
            image_data = None
            mime_type = "image/png"

            for part in content_parts:
                inline = part.get("inlineData")
                if inline and inline.get("mimeType", "").startswith("image/"):
                    image_data = inline["data"]
                    mime_type = inline["mimeType"]
                    break

            if not image_data:
                print("[image_gen] 响应中未找到图片数据")
                return None

            # 确定文件扩展名
            ext = "png"
            if "jpeg" in mime_type or "jpg" in mime_type:
                ext = "jpg"
            elif "webp" in mime_type:
                ext = "webp"

            # 保存图片
            filename = f"img_gen_{int(time.time() * 1000)}.{ext}"
            filepath = UPLOADS_DIR / filename
            filepath.write_bytes(base64.b64decode(image_data))
            print(f"[image_gen] 图片已保存: {filepath}")
            return filename

    except httpx.HTTPStatusError as e:
        error_body = e.response.text[:500] if e.response else ""
        print(f"[image_gen] API 请求失败 ({e.response.status_code}): {error_body}")
        return None
    except Exception as e:
        print(f"[image_gen] 生图异常: {type(e).__name__}: {e!r}")
        return None


CPA_IMAGE_MODEL = "gemini-3.1-flash-image"


async def generate_image_custom_route(prompt: str) -> str | None:
    """通过自定义 OpenAI 兼容路由生图（如 CLI Proxy API 本地代理，走 CLI 授权不吃 API 配额）。

    遍历设置里的 custom_model_routes，对每个 base_url 试 gemini-3.1-flash-image；
    CPA 的响应把图放在 message.images[0].image_url.url（data:image/...;base64,...）。
    不支持该模型的路由（如火山）会报错，直接跳到下一条。
    """
    from config import SETTINGS
    for route in SETTINGS.get("custom_model_routes") or []:
        url = _openai_chat_completions_url(route.get("base_url") or "")
        if not url:
            continue
        name = route.get("name") or url
        try:
            # trust_env=False：本地路由不能被系统代理劫持
            async with httpx.AsyncClient(timeout=IMAGE_GEN_TIMEOUT, trust_env=False) as client:
                resp = await client.post(url, headers={"Authorization": f"Bearer {route.get('api_key') or ''}"},
                                         json={"model": CPA_IMAGE_MODEL,
                                               "messages": [{"role": "user", "content": prompt}],
                                               "stream": False})
            if resp.status_code != 200:
                print(f"[image_gen] 路由 {name} 生图失败 ({resp.status_code})，试下一条")
                continue
            msg = (resp.json().get("choices") or [{}])[0].get("message") or {}
            images = msg.get("images") or []
            data_url = (images[0].get("image_url") or {}).get("url", "") if images else ""
            m = re.match(r"data:image/(\w+);base64,(.+)$", data_url, re.DOTALL)
            if not m:
                print(f"[image_gen] 路由 {name} 响应里没有图，试下一条")
                continue
            ext = "jpg" if m.group(1) in ("jpeg", "jpg") else m.group(1)
            filename = f"img_gen_{int(time.time() * 1000)}.{ext}"
            (UPLOADS_DIR / filename).write_bytes(base64.b64decode(m.group(2)))
            print(f"[image_gen] 路由 {name} 生图成功: {filename}")
            return filename
        except Exception as e:
            print(f"[image_gen] 路由 {name} 生图异常: {type(e).__name__}: {e!r}")
    return None


async def generate_image_siliconflow(prompt: str, image_size: str = "1024x1024") -> str | None:
    """硅基流动 Kolors 生图（免费额度档）。保存到 uploads 目录，返回文件名；失败返回 None。

    用作 Gemini 生图的兜底：Gemini free tier 生图配额为 0（全模型 429），
    Kolors 中文 prompt 友好，插画质感也合适。
    """
    api_key = get_key("siliconflow")
    if not api_key:
        print("[image_gen] 没有硅基流动 API Key，跳过 Kolors 生图")
        return None
    try:
        async with httpx.AsyncClient(timeout=IMAGE_GEN_TIMEOUT, trust_env=True) as client:
            print(f"[image_gen] Kolors 生图... prompt: {prompt[:80]}")
            resp = await client.post(
                "https://api.siliconflow.cn/v1/images/generations",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": "Kwai-Kolors/Kolors",
                    "prompt": prompt,
                    "image_size": image_size,
                    "batch_size": 1,
                    "num_inference_steps": 25,
                },
            )
            if resp.status_code != 200:
                print(f"[image_gen] Kolors 请求失败 ({resp.status_code}): {resp.text[:300]}")
                return None
            images = resp.json().get("images") or []
            url = (images[0] or {}).get("url") if images else None
            if not url:
                print("[image_gen] Kolors 响应中没有图片 URL")
                return None
            img = await client.get(url)
            img.raise_for_status()
            filename = f"img_gen_{int(time.time() * 1000)}.png"
            (UPLOADS_DIR / filename).write_bytes(img.content)
            print(f"[image_gen] Kolors 图片已保存: {filename}")
            return filename
    except Exception as e:
        print(f"[image_gen] Kolors 生图异常: {type(e).__name__}: {e!r}")
        return None


async def generate_image_leesai(prompt: str) -> str | None:
    """LeesAiHub 生图（OpenAI 兼容 /v1/images/generations，gpt-image-2）。

    按 settings 里 leesai_keys 顺序轮换：当前 key 额度耗尽/失败就试下一个。
    响应为 OpenAI 格式 data[].b64_json（PNG）。失败返回 None。
    """
    keys = get_leesai_keys()
    if not keys:
        print("[image_gen] 没配 LeesAiHub key，跳过 gpt-image-2 生图")
        return None
    for idx, api_key in enumerate(keys):
        try:
            async with httpx.AsyncClient(timeout=IMAGE_GEN_TIMEOUT, trust_env=True) as client:
                print(f"[image_gen] LeesAiHub 生图 (key{idx + 1}/{len(keys)})... prompt: {prompt[:80]}")
                resp = await client.post(
                    f"{LEESAI_BASE_URL}/images/generations",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={"model": LEESAI_IMAGE_MODEL, "prompt": prompt, "size": "1024x1024", "n": 1},
                )
            if resp.status_code != 200:
                body = resp.text[:200]
                print(f"[image_gen] LeesAiHub key{idx + 1} 失败 ({resp.status_code}): {body}，试下一个")
                continue
            data = resp.json()
            b64 = (data.get("data") or [{}])[0].get("b64_json") or ""
            if not b64:
                print(f"[image_gen] LeesAiHub key{idx + 1} 响应里没有图，试下一个")
                continue
            filename = f"img_gen_{int(time.time() * 1000)}.png"
            (UPLOADS_DIR / filename).write_bytes(base64.b64decode(b64))
            print(f"[image_gen] LeesAiHub key{idx + 1} 生图成功: {filename}")
            return filename
        except Exception as e:
            print(f"[image_gen] LeesAiHub key{idx + 1} 生图异常: {type(e).__name__}: {e!r}")
    return None
