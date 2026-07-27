"""
设置、世界书、模型列表、TTS 路由
"""

import json
import logging
import traceback

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, FileResponse
from pydantic import BaseModel, Field
from typing import Any, Dict, Optional

import httpx

from config import SETTINGS, save_settings, get_key, get_sentinel_config, load_worldbook, save_worldbook, load_chat_status, TTS_CACHE_DIR, TTS_CACHE_MAX_BYTES, THEATER_TTS_CACHE_DIR, normalize_custom_model_routes, refresh_custom_models, iter_visible_models, DATA_DIR
from tts import cleanup_tts_cache_dir
from ws import manager

router = APIRouter()

log = logging.getLogger("settings.fishaudio")

RELAY_MODEL_PROVIDERS = {"aipro", "custom_openai"}

# ── 密钥打码 ──────────────────────────────────────
# GET /api/settings 不再外发明文密钥：明文字段返回打码值，前端原样回传时
# PUT 端识别"打码回显"并保留原值，只有真正输入新密钥才会覆盖。

def _mask(k):
    if not k or len(k) < 8:
        return k
    return k[:4] + "*" * (len(k) - 8) + k[-4:]

def _is_masked_echo(field: str, value: str) -> bool:
    """回传值等于当前存储值的打码形式（且不等于存储值本身）→ 视为未修改"""
    cur = SETTINGS.get(field, "") or ""
    return bool(value) and value == _mask(cur) and value != cur

# ── 模型列表 ──────────────────────────────────────
@router.get("/api/models")
async def list_models():
    rows = [
        {
            "key": k,
            "provider": v["provider"],
            "custom": v.get("provider") == "custom_openai",
            "route_name": v.get("route_name", ""),
        }
        for k, v in iter_visible_models()
    ]
    return sorted(rows, key=lambda item: 1 if item["provider"] in RELAY_MODEL_PROVIDERS else 0)

# ── 设置 ──────────────────────────────────────────
class SettingsUpdate(BaseModel):
    gemini_key: Optional[str] = None
    siliconflow_key: Optional[str] = None
    gemini_free_key: Optional[str] = None
    aipro_key: Optional[str] = None
    senseaudio_key: Optional[str] = None
    minimax_key: Optional[str] = None
    fishaudio_key: Optional[str] = None
    step_key: Optional[str] = None
    tts_provider: Optional[str] = None
    tavily_api_key: Optional[str] = None
    netease_music_u: Optional[str] = None
    sentinel_base_url: Optional[str] = None
    sentinel_api_key: Optional[str] = None
    sentinel_model: Optional[str] = None
    embedding_base_url: Optional[str] = None
    embedding_api_key: Optional[str] = None
    embedding_model: Optional[str] = None
    luckin_mcp_enabled: Optional[bool] = None
    luckin_mcp_token: Optional[str] = None
    luckin_default_longitude: Optional[str] = None
    luckin_default_latitude: Optional[str] = None
    luckin_default_shop_keyword: Optional[str] = None
    custom_model_routes: Optional[list[Dict[str, Any]]] = None

class HomeLayoutUpdate(BaseModel):
    version: Optional[int] = 2
    positions: Dict[str, Any] = Field(default_factory=dict)

def _normalize_home_layout(payload: Any) -> Dict[str, Any]:
    positions = payload.get("positions", {}) if isinstance(payload, dict) else {}
    normalized: Dict[str, int] = {}
    if isinstance(positions, dict):
        for app_id, cell in positions.items():
            if not isinstance(app_id, str):
                continue
            try:
                cell_index = int(cell)
            except (TypeError, ValueError):
                continue
            if 0 <= cell_index <= 4095:
                normalized[app_id] = cell_index
    return {"version": 2, "positions": normalized}

@router.get("/api/home/layout")
async def get_home_layout():
    return _normalize_home_layout(SETTINGS.get("home_layout", {}))

@router.put("/api/home/layout")
async def update_home_layout(body: HomeLayoutUpdate):
    payload = body.model_dump() if hasattr(body, "model_dump") else body.dict()
    layout = _normalize_home_layout(payload)
    SETTINGS["home_layout"] = layout
    save_settings(SETTINGS)
    return {"ok": True, "layout": layout}

@router.get("/api/settings")
async def get_settings():
    mask = _mask
    routes_masked = [
        dict(r, api_key=mask(r.get("api_key", "")))
        for r in normalize_custom_model_routes(SETTINGS.get("custom_model_routes"))
    ]
    return {
        "gemini_key": mask(SETTINGS.get("gemini_key", "")),
        "siliconflow_key": mask(SETTINGS.get("siliconflow_key", "")),
        "gemini_free_key": mask(SETTINGS.get("gemini_free_key", "")),
        "aipro_key": mask(SETTINGS.get("aipro_key", "")),
        "senseaudio_key": mask(SETTINGS.get("senseaudio_key", "")),
        "minimax_key": mask(SETTINGS.get("minimax_key", "")),
        "fishaudio_key": mask(SETTINGS.get("fishaudio_key", "")),
        "step_key": mask(SETTINGS.get("step_key", "")),
        "tts_provider": SETTINGS.get("tts_provider", "siliconflow"),
        "tavily_api_key": mask(SETTINGS.get("tavily_api_key", "")),
        "netease_music_u": mask(SETTINGS.get("netease_music_u", "")),
        "sentinel_base_url": SETTINGS.get("sentinel_base_url", ""),
        "sentinel_api_key": mask(SETTINGS.get("sentinel_api_key", "")),
        "sentinel_model": SETTINGS.get("sentinel_model", ""),
        "embedding_base_url": SETTINGS.get("embedding_base_url", ""),
        "embedding_api_key": mask(SETTINGS.get("embedding_api_key", "")),
        "embedding_model": SETTINGS.get("embedding_model", ""),
        "luckin_mcp_enabled": SETTINGS.get("luckin_mcp_enabled", False),
        "luckin_mcp_token": mask(SETTINGS.get("luckin_mcp_token", "")),
        "luckin_default_longitude": SETTINGS.get("luckin_default_longitude", ""),
        "luckin_default_latitude": SETTINGS.get("luckin_default_latitude", ""),
        "luckin_default_shop_keyword": SETTINGS.get("luckin_default_shop_keyword", ""),
        "custom_model_routes": routes_masked,
        "gemini_key_masked": mask(SETTINGS.get("gemini_key", "")),
        "siliconflow_key_masked": mask(SETTINGS.get("siliconflow_key", "")),
        "gemini_free_key_masked": mask(SETTINGS.get("gemini_free_key", "")),
        "aipro_key_masked": mask(SETTINGS.get("aipro_key", "")),
        "senseaudio_key_masked": mask(SETTINGS.get("senseaudio_key", "")),
        "minimax_key_masked": mask(SETTINGS.get("minimax_key", "")),
        "fishaudio_key_masked": mask(SETTINGS.get("fishaudio_key", "")),
        "step_key_masked": mask(SETTINGS.get("step_key", "")),
        "tavily_api_key_masked": mask(SETTINGS.get("tavily_api_key", "")),
        "netease_music_u_masked": mask(SETTINGS.get("netease_music_u", "")),
        "sentinel_api_key_masked": mask(SETTINGS.get("sentinel_api_key", "")),
        "embedding_api_key_masked": mask(SETTINGS.get("embedding_api_key", "")),
    }

@router.put("/api/settings")
async def update_settings(body: SettingsUpdate):
    luckin_changed = False
    def set_secret(field, value):
        if value is not None and not _is_masked_echo(field, value):
            SETTINGS[field] = value
    set_secret("gemini_key", body.gemini_key)
    set_secret("siliconflow_key", body.siliconflow_key)
    set_secret("gemini_free_key", body.gemini_free_key)
    set_secret("aipro_key", body.aipro_key)
    set_secret("senseaudio_key", body.senseaudio_key)
    set_secret("minimax_key", body.minimax_key)
    set_secret("fishaudio_key", body.fishaudio_key)
    set_secret("step_key", body.step_key)
    if body.tts_provider is not None:
        SETTINGS["tts_provider"] = body.tts_provider
    set_secret("tavily_api_key", body.tavily_api_key)
    if body.sentinel_base_url is not None:
        SETTINGS["sentinel_base_url"] = body.sentinel_base_url
    set_secret("sentinel_api_key", body.sentinel_api_key)
    if body.sentinel_model is not None:
        SETTINGS["sentinel_model"] = body.sentinel_model
    if body.embedding_base_url is not None:
        SETTINGS["embedding_base_url"] = body.embedding_base_url
    set_secret("embedding_api_key", body.embedding_api_key)
    if body.embedding_model is not None:
        SETTINGS["embedding_model"] = body.embedding_model
    if body.luckin_mcp_enabled is not None:
        luckin_changed = luckin_changed or SETTINGS.get("luckin_mcp_enabled") != body.luckin_mcp_enabled
        SETTINGS["luckin_mcp_enabled"] = body.luckin_mcp_enabled
    if body.luckin_mcp_token is not None and not _is_masked_echo("luckin_mcp_token", body.luckin_mcp_token):
        luckin_changed = luckin_changed or SETTINGS.get("luckin_mcp_token", "") != body.luckin_mcp_token
        SETTINGS["luckin_mcp_token"] = body.luckin_mcp_token
    if body.luckin_default_longitude is not None:
        SETTINGS["luckin_default_longitude"] = body.luckin_default_longitude
    if body.luckin_default_latitude is not None:
        SETTINGS["luckin_default_latitude"] = body.luckin_default_latitude
    if body.luckin_default_shop_keyword is not None:
        SETTINGS["luckin_default_shop_keyword"] = body.luckin_default_shop_keyword
    if body.custom_model_routes is not None:
        new_routes = normalize_custom_model_routes(body.custom_model_routes)
        old_by_id = {
            r.get("id"): r
            for r in normalize_custom_model_routes(SETTINGS.get("custom_model_routes"))
        }
        for r in new_routes:
            old = old_by_id.get(r.get("id"))
            if old:
                old_key = old.get("api_key", "")
                if r.get("api_key") and r["api_key"] == _mask(old_key) and r["api_key"] != old_key:
                    r["api_key"] = old_key
        SETTINGS["custom_model_routes"] = new_routes
        refresh_custom_models()
    if body.netease_music_u is not None and not _is_masked_echo("netease_music_u", body.netease_music_u):
        old_mu = SETTINGS.get("netease_music_u", "")
        SETTINGS["netease_music_u"] = body.netease_music_u
        if body.netease_music_u != old_mu:
            # MUSIC_U 变更，重新登录 pyncm
            try:
                from music import reload_login
                reload_login()
            except Exception:
                pass
    save_settings(SETTINGS)
    if luckin_changed:
        try:
            from luckin import LUCKIN_SERVER_NAME
            from mcp_client import mcp_manager
            await mcp_manager.disconnect(LUCKIN_SERVER_NAME)
        except Exception:
            pass
    return {"ok": True}

# ── 温度设置 ──────────────────────────────────────
class TempUpdate(BaseModel):
    temperature: float

@router.put("/api/settings/temperature")
async def update_temperature(body: TempUpdate):
    SETTINGS["temperature"] = body.temperature
    save_settings(SETTINGS)
    return {"ok": True}

# ── 视频通话开关 ──────────────────────────────────
@router.get("/api/settings/video-call")
async def get_video_call_setting():
    return {"video_call_enabled": SETTINGS.get("video_call_enabled", True)}

class VideoCallToggle(BaseModel):
    enabled: bool

@router.put("/api/settings/video-call")
async def update_video_call_setting(body: VideoCallToggle):
    SETTINGS["video_call_enabled"] = body.enabled
    save_settings(SETTINGS)
    return {"ok": True, "video_call_enabled": body.enabled}

# ── AI 生图开关 ───────────────────────────────────
@router.get("/api/settings/image-gen")
async def get_image_gen_setting():
    return {"image_gen_enabled": SETTINGS.get("image_gen_enabled", False)}

class ImageGenToggle(BaseModel):
    enabled: bool

@router.put("/api/settings/image-gen")
async def update_image_gen_setting(body: ImageGenToggle):
    SETTINGS["image_gen_enabled"] = body.enabled
    save_settings(SETTINGS)
    return {"ok": True, "image_gen_enabled": body.enabled}

# ── CLI 工具调用开关（Gemini CLI / Antigravity CLI） ─────────────────
# ── AI song generation toggle ─────────────────────────────────
@router.get("/api/settings/song-gen")
async def get_song_gen_setting():
    return {"song_gen_enabled": SETTINGS.get("song_gen_enabled", False)}

class SongGenToggle(BaseModel):
    enabled: bool

@router.put("/api/settings/song-gen")
async def update_song_gen_setting(body: SongGenToggle):
    SETTINGS["song_gen_enabled"] = body.enabled
    save_settings(SETTINGS)
    return {"ok": True, "song_gen_enabled": body.enabled}

# ── 微信桥接设置 ─────────────────────────────────
class WeChatBridgeSettingsUpdate(BaseModel):
    enabled: Optional[bool] = None
    transport: Optional[str] = None
    webhook_url: Optional[str] = None
    webhook_token: Optional[str] = None
    inbound_token: Optional[str] = None
    openclaw_home: Optional[str] = None
    context_stale_seconds: Optional[int] = None


class WeChatBridgeBindingCreate(BaseModel):
    source_type: Optional[str] = None
    source_id: Optional[str] = None
    ttl_seconds: Optional[int] = None


@router.get("/api/settings/wechat-bridge")
async def get_wechat_bridge_setting():
    from wechat_bridge import public_wechat_bindings

    openclaw_accounts = []
    openclaw_status_error = ""
    try:
        from openclaw_weixin import summarize_accounts

        openclaw_accounts = summarize_accounts(SETTINGS.get("wechat_bridge_openclaw_home") or None)
    except Exception as exc:
        openclaw_status_error = str(exc)

    pending = SETTINGS.get("wechat_bridge_pending_bindings")
    if not isinstance(pending, dict):
        pending = {}
    return {
        "wechat_bridge_enabled": SETTINGS.get("wechat_bridge_enabled", False),
        "wechat_bridge_transport": SETTINGS.get("wechat_bridge_transport", "webhook"),
        "wechat_bridge_webhook_url": SETTINGS.get("wechat_bridge_webhook_url", ""),
        "wechat_bridge_webhook_token": SETTINGS.get("wechat_bridge_webhook_token", ""),
        "wechat_bridge_inbound_token": SETTINGS.get("wechat_bridge_inbound_token", ""),
        "wechat_bridge_openclaw_home": SETTINGS.get("wechat_bridge_openclaw_home", ""),
        "wechat_bridge_context_stale_seconds": SETTINGS.get("wechat_bridge_context_stale_seconds", 15 * 60),
        "wechat_bridge_last_send": SETTINGS.get("wechat_bridge_last_send"),
        "openclaw_accounts": openclaw_accounts,
        "openclaw_status_error": openclaw_status_error,
        "bindings": public_wechat_bindings(settings=SETTINGS),
        "pending_bindings": list(pending.values()),
    }


@router.put("/api/settings/wechat-bridge")
async def update_wechat_bridge_setting(body: WeChatBridgeSettingsUpdate):
    if body.enabled is not None:
        SETTINGS["wechat_bridge_enabled"] = bool(body.enabled)
    if body.transport is not None:
        transport = body.transport.strip().lower()
        if transport not in ("webhook", "openclaw"):
            raise HTTPException(status_code=400, detail="transport must be webhook or openclaw")
        SETTINGS["wechat_bridge_transport"] = transport
    if body.webhook_url is not None:
        SETTINGS["wechat_bridge_webhook_url"] = body.webhook_url.strip()
    if body.webhook_token is not None:
        SETTINGS["wechat_bridge_webhook_token"] = body.webhook_token.strip()
    if body.inbound_token is not None:
        SETTINGS["wechat_bridge_inbound_token"] = body.inbound_token.strip()
    if body.openclaw_home is not None:
        SETTINGS["wechat_bridge_openclaw_home"] = body.openclaw_home.strip()
    if body.context_stale_seconds is not None:
        SETTINGS["wechat_bridge_context_stale_seconds"] = max(60, int(body.context_stale_seconds))
    save_settings(SETTINGS)
    return {
        "ok": True,
        "wechat_bridge_enabled": SETTINGS.get("wechat_bridge_enabled", False),
        "wechat_bridge_transport": SETTINGS.get("wechat_bridge_transport", "webhook"),
        "wechat_bridge_webhook_url": SETTINGS.get("wechat_bridge_webhook_url", ""),
    }


@router.post("/api/settings/wechat-bridge/bindings")
async def create_wechat_bridge_binding(body: WeChatBridgeBindingCreate):
    from wechat_bridge import create_wechat_pending_binding, get_recorded_wechat_route

    route = get_recorded_wechat_route()
    source_type = (body.source_type or route.get("source_type") or "").strip()
    source_id = (body.source_id or route.get("source_id") or "").strip()
    if not source_type or not source_id:
        raise HTTPException(status_code=400, detail="source_type and source_id are required when no recent WeChat route exists")

    SETTINGS["wechat_bridge_enabled"] = True
    SETTINGS["wechat_bridge_transport"] = "openclaw"
    pending = create_wechat_pending_binding(
        source_type=source_type,
        source_id=source_id,
        ttl_seconds=body.ttl_seconds or 10 * 60,
        settings=SETTINGS,
    )
    save_settings(SETTINGS)
    return {
        "ok": True,
        "code": pending["code"],
        "source_type": pending["source_type"],
        "source_id": pending["source_id"],
        "expires_at": pending["expires_at"],
        "instruction": f"Send this in WeChat: bind {pending['code']}",
    }

@router.get("/api/settings/gemini-cli-tools")
async def get_gemini_cli_tools_setting():
    return {"gemini_cli_tools_enabled": SETTINGS.get("gemini_cli_tools_enabled", False)}

class GeminiCliToolsToggle(BaseModel):
    enabled: bool

@router.put("/api/settings/gemini-cli-tools")
async def update_gemini_cli_tools_setting(body: GeminiCliToolsToggle):
    SETTINGS["gemini_cli_tools_enabled"] = body.enabled
    save_settings(SETTINGS)
    return {"ok": True, "gemini_cli_tools_enabled": body.enabled}

# ── 桌宠开关 ──────────────────────────────────────
@router.get("/api/settings/pet")
async def get_pet_setting():
    return {"pet_enabled": SETTINGS.get("pet_enabled", False)}

class PetToggle(BaseModel):
    enabled: bool

@router.put("/api/settings/pet")
async def update_pet_setting(body: PetToggle):
    SETTINGS["pet_enabled"] = body.enabled
    save_settings(SETTINGS)
    return {"ok": True, "pet_enabled": body.enabled}

# ── 健康数据分享开关 ──────────────────────────────
@router.get("/api/settings/health-share")
async def get_health_share_setting():
    return {"health_share_enabled": SETTINGS.get("health_share_enabled", False)}

class HealthShareToggle(BaseModel):
    enabled: bool

@router.put("/api/settings/health-share")
async def update_health_share_setting(body: HealthShareToggle):
    SETTINGS["health_share_enabled"] = body.enabled
    save_settings(SETTINGS)
    await manager.broadcast({
        "type": "health_share_changed",
        "data": {"health_share_enabled": body.enabled},
    })
    await manager.broadcast({
        "type": "capability_config_changed",
        "data": {"key": "health_context", "enabled": body.enabled},
    })
    return {"ok": True, "health_share_enabled": body.enabled}

# ── 世界书 ────────────────────────────────────────
class WorldBookUpdate(BaseModel):
    ai_persona: str = ""
    user_persona: str = ""
    system_prompt: str = ""
    system_prompt_enabled: bool = True
    ai_name: str = "AI"
    user_name: str = "你"
    persona_schema_version: int = 1
    ai_persona_sections: Dict[str, str] = Field(default_factory=dict)
    user_persona_sections: Dict[str, str] = Field(default_factory=dict)
    creative_rules: str = ""
    persona_section_locks: Dict[str, Any] = Field(default_factory=dict)
    persona_evolution_enabled: bool = False

@router.get("/api/worldbook")
async def get_worldbook():
    return load_worldbook()

@router.put("/api/worldbook")
async def update_worldbook(body: WorldBookUpdate):
    current = load_worldbook()
    payload = body.model_dump() if hasattr(body, "model_dump") else body.dict()
    current.update(payload)
    save_worldbook(current)
    return {"ok": True}

# ── 聊天状态 ──────────────────────────────────────
@router.get("/api/chat_status")
async def get_chat_status_api():
    return load_chat_status()

# ── TTS 语音合成 ──────────────────────────────────
class TTSRequest(BaseModel):
    text: str
    voice: str = ""
    msg_id: Optional[str] = None

@router.post("/api/tts")
async def tts_synthesize(body: TTSRequest):
    key = get_key("siliconflow")
    if not key:
        return Response(content=json.dumps({"error": "未配置硅基流动 API Key"}), status_code=400, media_type="application/json")
    if not body.text.strip():
        return Response(content=json.dumps({"error": "文本不能为空"}), status_code=400, media_type="application/json")
    if not body.voice:
        return Response(content=json.dumps({"error": "未选择语音"}), status_code=400, media_type="application/json")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.siliconflow.cn/v1/audio/speech",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": "FunAudioLLM/CosyVoice2-0.5B",
                    "input": body.text.strip(),
                    "voice": body.voice,
                    "response_format": "mp3",
                    "speed": 1.0,
                    "gain": 0
                }
            )
        if resp.status_code != 200:
            return Response(content=json.dumps({"error": f"TTS API 错误: {resp.status_code}"}), status_code=502, media_type="application/json")
        audio_data = resp.content
        # 如果提供了 msg_id，将音频缓存到服务器
        if body.msg_id:
            import re
            safe_id = re.sub(r'[^a-zA-Z0-9_\-]', '', body.msg_id)
            if safe_id:
                cache_path = TTS_CACHE_DIR / f"{safe_id}.mp3"
                cache_path.write_bytes(audio_data)
                cleanup_tts_cache_dir(TTS_CACHE_DIR, TTS_CACHE_MAX_BYTES, skip={cache_path})
        return Response(content=audio_data, media_type="audio/mpeg")
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=500, media_type="application/json")

@router.head("/api/tts/audio/{msg_id}")
@router.get("/api/tts/audio/{msg_id}")
async def tts_audio(msg_id: str):
    import re
    safe_id = re.sub(r'[^a-zA-Z0-9_\-]', '', msg_id)
    if not safe_id:
        return Response(status_code=404)
    cache_path = TTS_CACHE_DIR / f"{safe_id}.mp3"
    if not cache_path.exists():
        return Response(status_code=404)
    return FileResponse(cache_path, media_type="audio/mpeg", filename=f"{safe_id}.mp3")

@router.head("/api/theater/tts/audio/{msg_id}")
@router.get("/api/theater/tts/audio/{msg_id}")
async def theater_tts_audio(msg_id: str):
    import re
    safe_id = re.sub(r'[^a-zA-Z0-9_\-]', '', msg_id)
    if not safe_id:
        return Response(status_code=404)
    cache_path = THEATER_TTS_CACHE_DIR / f"{safe_id}.mp3"
    if not cache_path.exists():
        return Response(status_code=404)
    return FileResponse(cache_path, media_type="audio/mpeg", filename=f"{safe_id}.mp3")

# CosyVoice2 系统预置音色。硅基的 /v1/audio/voice/list 只返回用户克隆音色，
# 不含系统音色——账号没克隆过时返回空，导致前端音色下拉没选项、TTS 无法触发。
# 这里作为兜底，列表为空时补上这些免费可用的系统音色（实测 alex/claire 等可直接合成）。
_COSYVOICE2_SYSTEM_VOICES = [
    {"uri": "FunAudioLLM/CosyVoice2-0.5B:alex", "name": "Alex（男）"},
    {"uri": "FunAudioLLM/CosyVoice2-0.5B:benjamin", "name": "Benjamin（男）"},
    {"uri": "FunAudioLLM/CosyVoice2-0.5B:charles", "name": "Charles（男）"},
    {"uri": "FunAudioLLM/CosyVoice2-0.5B:claire", "name": "Claire（女）"},
    {"uri": "FunAudioLLM/CosyVoice2-0.5B:david", "name": "David（男）"},
    {"uri": "FunAudioLLM/CosyVoice2-0.5B:diana", "name": "Diana（女）"},
]

# SenseAudio Free 版（实名后默认）可调用的普通音色。当 /v1/get_voice 接口失败或返回空时兜底。
_SENSEAUDIO_FREE_VOICES = [
    {"uri": "child_0001_a", "customName": "可爱萌娃（开心）"},
    {"uri": "child_0001_b", "customName": "可爱萌娃（平稳）"},
    {"uri": "male_0004_a", "customName": "儒雅道长（平稳）"},
    {"uri": "male_0018_a", "customName": "沙哑青年（深情）"},
]


# SenseAudio Free 版（实名后默认）可直接调用的 voice_id 集合（来自官方文档音色列表页）。
# 其他音色可能需要 Plus/Pro 等更高套餐，调了会报权限错误。
_SENSEAUDIO_FREE_VOICE_IDS = {"child_0001_a", "child_0001_b", "male_0004_a", "male_0018_a"}


# MiniMax 系统音色 —— 男友陪伴向精选（8 个中文男声）
# 砍到只保留中文男声，所有显示名都是中文，下拉一目了然不再翻不到。
# 后续想扩展（克隆声/新增声）再补。
_MINIMAX_SYSTEM_VOICES = [
    {"uri": "junlang_nanyou", "customName": "⭐ 俊朗男友（名字就是男友）"},
    {"uri": "male-qn-jingying", "customName": "精英青年（磁性主流）"},
    {"uri": "male-qn-qingse", "customName": "青涩青年（清朗少年）"},
    {"uri": "male-qn-daxuesheng", "customName": "青年大学生（校园感）"},
    {"uri": "male-qn-badao", "customName": "霸道青年（强气男友）"},
    {"uri": "Chinese (Mandarin)_Gentleman", "customName": "温润男声（温柔绅士）"},
    {"uri": "Chinese (Mandarin)_Pure-hearted_Boy", "customName": "清澈邻家弟弟"},
    {"uri": "Chinese (Mandarin)_Unrestrained_Young_Man", "customName": "不羁青年（洒脱外向）"},
    {"uri": "Chinese (Mandarin)_Stubborn_Friend", "customName": "嘴硬竹马（傲娇竹马）"},
    {"uri": "lengdan_xiongzhang", "customName": "冷淡学长（冷都男友）"},
]


async def _list_senseaudio_voices(key: str) -> dict:
    """调 SenseAudio /v1/get_voice 拉音色列表；失败/空时回退 Free 版音色。

    返回三类可用音色：
    - 沙哑青年 male_0018_a：system 中唯一保留的免费男声（儒雅道长太老、萌娃是儿童声，按用户要求都不进下拉）
    - voice_generation：用户用提示词生成的音色（温叙远低沉/男友等）
    - voice_cloning：用户上传音频复刻的音色（当前 0，未来自动出现）

    注意：必须传 voice_type="all" 才能同时拿到这三类；传 "system" 时 generation/cloning 都是空数组。
    """
    try:
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            resp = await client.post(
                "https://api.senseaudio.cn/v1/get_voice",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"voice_type": "all"},
            )
        if resp.status_code != 200:
            return {"voices": _SENSEAUDIO_FREE_VOICES, "note": f"获取音色列表失败({resp.status_code})，已回退免费音色"}
        data = resp.json()
        system = data.get("system_voice") or []
        generation = data.get("voice_generation") or []
        cloning = data.get("voice_cloning") or []

        voices = []
        # 1. system：只保留沙哑青年 male_0018_a（用户指定唯一系统音色）
        for it in system:
            if not isinstance(it, dict):
                continue
            vid = it.get("voice_id") or ""
            if vid == "male_0018_a":
                voices.append({"uri": vid, "customName": it.get("voice_name") or "沙哑青年"})
                break

        # 2. voice_generation：用户用提示词生成的全部音色
        for it in generation:
            if not isinstance(it, dict):
                continue
            vid = it.get("voice_id") or ""
            if not vid:
                continue
            voices.append({"uri": vid, "customName": it.get("voice_name") or vid})

        # 3. voice_cloning：用户复刻的全部音色（当前 0，未来自动出现）
        for it in cloning:
            if not isinstance(it, dict):
                continue
            vid = it.get("voice_id") or ""
            if not vid:
                continue
            voices.append({"uri": vid, "customName": it.get("voice_name") or vid})

        if not voices:
            voices = _SENSEAUDIO_FREE_VOICES
        return {"voices": voices}
    except Exception as e:
        return {"voices": _SENSEAUDIO_FREE_VOICES, "note": f"音色列表请求异常：{e}，已回退免费音色"}


async def _list_minimax_voices(key: str) -> dict:
    """返回 MiniMax 男友向精选音色列表（固定 8 个中文男声）。

    不再去 /v1/get_voice 拉全量 —— 那个接口返回 327 个混合多语种，下拉翻不完，
    而且 voice_name 可能是英文。本项目的男友向场景只需要中文男声，硬编码 curated 列表
    体验更好；后续想扩展（克隆声/新增系统声）再合并即可。
    """
    # 顺手用一下 key 做个连通性检测，避免 key 失效时没有任何反馈
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            await client.post(
                "https://api.minimax.io/v1/get_voice",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"voice_type": "system"},
            )
    except Exception:
        pass  # 探测失败不影响，返回 curated 列表即可
    return {"voices": _MINIMAX_SYSTEM_VOICES}


# Fish Audio 默认声（不传 reference_id 时 Fish Audio 用的内置声）
# 永远放第一位，下拉第一项就是它，零配置可用。
_FISHAUDIO_DEFAULT_VOICE = {"uri": "", "customName": "⭐ Fish Audio 默认声（无 reference_id）"}


# Fish Audio 精选男友向中文男声（社区公共声）。
# 用户偏好（2026-07-15 确认）：磁性 + 性感 + 男友向（deep/sexy/intimate/seductive/breathy）。
# 乙游男主（恋与深空秦彻/黎深、未定事件簿莫弈、光与夜之恋齐司礼等）可用；明星真人克隆声仍排除。
# 当前 18 条：5 磁性性感 + 8 乙游男主 + 5 温柔亲密向。用户说「都加上」；良莠后续可在设置页 UI 删（json）或改这里。
# 后续临时加声走设置页「粘贴 fish.audio 链接」（存 data/fishaudio_user_voices.json，免改代码/重启）。
_FISHAUDIO_CURATED_VOICES = [
    # ── 磁性性感男友向 ──
    {"uri": "a56e22a0ec34498da51cdb396f5fcb18", "customName": "该隐（柔软亲密·浪漫气声·ASMR，热门）"},
    {"uri": "073ff47193eb4f179da0d62e250bfd82", "customName": "该隐·深沉版（低沉平滑·气声戏剧·诱惑）"},
    {"uri": "1244770105124a68abcfb9d028db2185", "customName": "⭐ 男1·磁性ASMR（低沉沙哑·贴麦呢喃·诱惑，用户首推）"},
    {"uri": "05349c9fdee04de981fb5c09b0e0379a", "customName": "柔情男声（温柔磁性·深情亲密·戏剧感）"},
    {"uri": "ee0a2c89d5c04288a695e741a748b8a9", "customName": "⭐ 磁性轻语男声（沉稳磁性·亲密玩味·轻语）"},
    # ── 乙游男主 ──
    {"uri": "7cb18d1a283247f9a2c3771384690f98", "customName": "秦彻（恋与深空·深沉成熟·冷静沉稳）"},
    {"uri": "e9327e13d3d54983934894283651c6a9", "customName": "黎深（恋与深空·深沉戏剧·角色向）"},
    {"uri": "8da2976486f1435f9aa5b6b07c1708c6", "customName": "莫弈（未定事件簿·冷静理性·沉稳）"},
    {"uri": "ca5f2f7d465e4c658f846da77daf718d", "customName": "齐司礼（光与夜之恋·温润亲密·优雅）"},
    {"uri": "da8d001ca1424649bce8b945b460d81b", "customName": "易遇5（世界之外·深沉平滑·亲密）"},
    {"uri": "f98b76e7bad04f078366c05d56307eb8", "customName": "沈星回（恋与深空·深沉温暖·亲密）"},
    {"uri": "38548717ab4340a79db111cca6bbba6a", "customName": "易遇（世界之外·温柔气声·亲密治愈）"},
    {"uri": "92eda74224f34a81912f0e0e36ff5c13", "customName": "夏以昼（恋与深空·温柔温暖·亲密·微撩）"},
    # ── 温柔亲密向 ──
    {"uri": "530a14adc5bf4189a86ca6af52e234b5", "customName": "男主配音（温柔温暖·浪漫·亲密）"},
    {"uri": "204900525e1243cc9a616c82c8c02636", "customName": "男4·温柔（温柔诗意·亲密·沉稳）"},
    {"uri": "cce8c195deca474ab5796c1afda9a509", "customName": "温柔轻语（温柔气声·抚慰·亲密）"},
    {"uri": "97ee352e8f864eebba97ba4dffa642ee", "customName": "深情男声（深情·戏剧·亲密）"},
    {"uri": "952757cbdedc4dcf90d727e5eef4e7dc", "customName": "温言（深沉共振·温柔·亲密）"},
]


# 用户自加的 Fish Audio 音色（在设置页粘贴 fish.audio/m/<id> 链接加入，存 json，免改代码/重启）
_FISHAUDIO_USER_VOICES_PATH = DATA_DIR / "fishaudio_user_voices.json"


def _load_fishaudio_user_voices() -> list[dict]:
    """读用户自加音色列表；文件不存在/损坏返回 []。"""
    try:
        if not _FISHAUDIO_USER_VOICES_PATH.exists():
            return []
        data = json.loads(_FISHAUDIO_USER_VOICES_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [v for v in data if isinstance(v, dict) and v.get("uri") and v.get("customName")]
    except Exception:
        log.exception("读 fishaudio_user_voices.json 失败")
    return []


def _save_fishaudio_user_voices(voices: list[dict]) -> None:
    _FISHAUDIO_USER_VOICES_PATH.write_text(
        json.dumps(voices, ensure_ascii=False, indent=2), encoding="utf-8"
    )


async def _list_fishaudio_voices(key: str) -> dict:
    """返回 Fish Audio 精选男友向中文男声 + 用户自己的克隆声。

    精选列表是硬编码的社区公共声（用户确认 18 条，见 _FISHAUDIO_CURATED_VOICES），
    不再去拉热门榜--热门榜里充斥游戏向/说教向/中年声，不符合项目男友陪伴定位。
    用户在 fish.audio 平台创建的克隆声会追加在后面（self=true 拉取）。
    """
    try:
        return await _list_fishaudio_voices_impl(key)
    except Exception as e:
        log.exception("FishAudio _list_fishaudio_voices 崩溃")
        return {"voices": [_FISHAUDIO_DEFAULT_VOICE], "error": f"{type(e).__name__}: {e}"}


async def _list_fishaudio_voices_impl(key: str) -> dict:
    # 1. 默认声 + 精选男友向中文男声（硬编码，不依赖网络）
    voices: list[dict] = [_FISHAUDIO_DEFAULT_VOICE] + list(_FISHAUDIO_CURATED_VOICES)
    notes: list[str] = []

    # 1.5 用户自加音色（粘贴 fish.audio 链接加入，存 json，免改代码/重启）
    for v in _load_fishaudio_user_voices():
        uri = v.get("uri") or ""
        if uri and not any(x["uri"] == uri for x in voices):
            voices.append(v)

    # 2. 用户自己的克隆声（self=true 拉取，失败静默跳过）
    seen_ids: set[str] = {v["uri"] for v in voices}
    try:
        async with httpx.AsyncClient(timeout=15, trust_env=True) as client:
            resp = await client.get(
                "https://api.fish.audio/model?page_size=20&page_number=1&self=true&type=tts&sort_by=task_count",
                headers={"Authorization": f"Bearer {key}", "model": "s2.1-pro-free"},
            )
        if resp.status_code == 200:
            data = resp.json()
            items = data.get("items") if isinstance(data, dict) else data
            if isinstance(items, list):
                private: list[dict] = []
                for it in items:
                    if not isinstance(it, dict) or it.get("state", "trained") != "trained":
                        continue
                    ref_id = it.get("_id") or it.get("id") or it.get("model_id") or ""
                    if not ref_id or ref_id in seen_ids:
                        continue
                    seen_ids.add(ref_id)
                    name = it.get("title") or it.get("name") or ref_id
                    private.append({"uri": ref_id, "customName": f"👤 {name}"})
                if private:
                    voices.append({"uri": "__divider__", "customName": "── 我的克隆声 ──"})
                    voices.extend(private)
        else:
            notes.append(f"克隆声列表拉取失败({resp.status_code})")
    except Exception as e:
        # 克隆声拉取失败不影响精选列表
        notes.append(f"克隆声列表异常：{type(e).__name__}: {e}")

    return {"voices": voices, **({"note": "; ".join(notes)} if notes else {})}


# 阶跃星辰 Step TTS 精选男友向中文男声（step-tts-2 支持全部 36 音色，这里只列中文男声）。
_STEP_DEFAULT_VOICE = {"uri": "", "customName": "⭐ Step 默认声（不指定 voice_label）"}
_STEP_CURATED_VOICES = [
    {"uri": "cixingnansheng",     "customName": "⭐ 磁性男声（低沉磁性·男友向·推荐首选）"},
    {"uri": "shenchennanyin",     "customName": "深沉男音（低沉稳重·年上爹系）"},
    {"uri": "wenrougongzi",       "customName": "温柔公子（温润绅士·优雅亲密）"},
    {"uri": "ruyananshi",         "customName": "儒雅男士（沉稳优雅·知性）"},
    {"uri": "wenrounansheng",     "customName": "温柔男声（柔和关怀·温暖）"},
    {"uri": "zixinnansheng",      "customName": "自信男声（坚定自信·阳光）"},
    {"uri": "zhengpaiqingnian",   "customName": "正派青年（正直阳光·少年感）"},
    {"uri": "boyinnansheng",      "customName": "播音男声（清晰标准· audiobook）"},
    {"uri": "yuanqinansheng",     "customName": "元气男声（活力四射·开朗）"},
    {"uri": "qingniandaxuesheng", "customName": "青年大学生（青春校园·邻家）"},
    {"uri": "shuangkuainansheng", "customName": "爽快男声（直爽坦诚·接地气）"},
    {"uri": "vibrant-youth",      "customName": "Vibrant Youth（活力青年·动感）"},
    {"uri": "soft-spoken-gentleman", "customName": "Soft-spoken Gentleman（温和绅士·轻声细语）"},
    {"uri": "magnetic-voiced-male",  "customName": "Magnetic-voiced Male（磁性嗓音·深情）"},
]


async def _list_step_voices(key: str) -> dict:
    """返回 Step TTS 精选中文男声。Step 暂不支持用户克隆声 API（只读精选列表）。"""
    voices = [_STEP_DEFAULT_VOICE] + list(_STEP_CURATED_VOICES)
    return {"voices": voices}


# Edge TTS 精选中文音色（微软 Azure 神经语音，免费无需 key）
# 直接硬编码 curated 列表，避免去拉全量几百个多语种音色污染下拉。
# 注意：edge-tts 7.x 实际只有 6 个 zh-CN 音色，其他音色 ID 会 NoAudioReceived 错误。
# 想扩展（克隆声 / 多语种）再补。
_EDGE_SYSTEM_VOICES = [
    {"uri": "zh-CN-XiaoxiaoNeural", "customName": "⭐ 晓晓（女·温暖亲切，免费中文女声天花板）"},
    {"uri": "zh-CN-XiaoyiNeural", "customName": "晓伊（女·活泼俏皮）"},
    {"uri": "zh-CN-YunxiNeural", "customName": "云希（男·阳光亲切）"},
    {"uri": "zh-CN-YunjianNeural", "customName": "云健（男·运动感）"},
    {"uri": "zh-CN-YunxiaNeural", "customName": "云夏（男·可爱系）"},
    {"uri": "zh-CN-YunyangNeural", "customName": "云扬（男·新闻播报）"},
    {"uri": "zh-TW-YunJheNeural", "customName": "⭐ 云哲（男·成熟自然，台湾男声伴侣推荐）"},
]


@router.get("/api/tts/voices")
async def tts_voice_list():
    from config import get_tts_provider
    provider = get_tts_provider()
    if provider == "senseaudio":
        key = get_key("senseaudio")
        if not key:
            return {"voices": [], "error": "未配置 SenseAudio API Key", "provider": provider}
        result = await _list_senseaudio_voices(key)
        result["provider"] = provider
        return result
    if provider == "minimax":
        key = get_key("minimax")
        if not key:
            return {"voices": [], "error": "未配置 MiniMax API Key", "provider": provider}
        result = await _list_minimax_voices(key)
        result["provider"] = provider
        return result
    if provider == "edge":
        return {"voices": _EDGE_SYSTEM_VOICES, "provider": provider}
    if provider == "fishaudio":
        key = get_key("fishaudio")
        if not key:
            return {"voices": [], "error": "未配置 FishAudio API Key", "provider": provider}
        result = await _list_fishaudio_voices(key)
        result["provider"] = provider
        return result
    if provider == "step":
        key = get_key("step")
        if not key:
            return {"voices": [], "error": "未配置 Step API Key", "provider": provider}
        result = await _list_step_voices(key)
        result["provider"] = provider
        return result
    # 默认硅基流动
    key = get_key("siliconflow")
    if not key:
        return {"voices": [], "error": "未配置硅基流动 API Key", "provider": provider}
    try:
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            resp = await client.get(
                "https://api.siliconflow.cn/v1/audio/voice/list",
                headers={"Authorization": f"Bearer {key}"}
            )
        if resp.status_code != 200:
            return {"voices": [], "error": "获取音色列表失败", "provider": provider}
        data = resp.json()
        voices = data.get("result") or data.get("voices") or data.get("data") or []
        if not voices:
            voices = _COSYVOICE2_SYSTEM_VOICES
        return {"voices": voices, "provider": provider}
    except Exception as e:
        return {"voices": [], "error": str(e), "provider": provider}


# ── Fish Audio 用户自加音色（在设置页粘贴 fish.audio/m/<id> 链接加入，存 json，免改代码/重启）──
def _parse_fishaudio_id(s: str) -> str:
    """从 URL 或纯 ID 提取 reference_id。支持 https://fish.audio/m/<id> / 纯 id / 带尾部路径。"""
    s = (s or "").strip()
    if not s:
        return ""
    if "/m/" in s:
        s = s.split("/m/", 1)[1]
    s = s.split("?", 1)[0].split("#", 1)[0].strip("/")
    return s.split("/", 1)[0]


@router.get("/api/tts/fishaudio/voice")
async def fishaudio_user_voice_list():
    from config import get_tts_provider
    return {"voices": _load_fishaudio_user_voices(), "enabled": get_tts_provider() == "fishaudio"}


class FishAudioVoiceIn(BaseModel):
    link: str  # fish.audio/m/<id> 链接 或 纯 id
    name: Optional[str] = None  # 可选自定义显示名；不传则拉 fish.audio 的 title


@router.post("/api/tts/fishaudio/voice")
async def fishaudio_user_voice_add(body: FishAudioVoiceIn):
    ref_id = _parse_fishaudio_id(body.link)
    if not ref_id or len(ref_id) < 8:
        raise HTTPException(400, "无法解析链接，请粘完整 https://fish.audio/m/<id>")
    voices = _load_fishaudio_user_voices()
    exists = any(v["uri"] == ref_id for v in voices) or any(v["uri"] == ref_id for v in _FISHAUDIO_CURATED_VOICES)
    if exists:
        raise HTTPException(409, "这个音色已经在列表里了")
    title = body.name or ref_id
    if not body.name:
        try:
            async with httpx.AsyncClient(timeout=15, trust_env=True) as client:
                resp = await client.get(
                    f"https://api.fish.audio/model/{ref_id}",
                    headers={"model": "s2.1-pro-free"},
                )
            if resp.status_code == 200:
                t = (resp.json() or {}).get("title") or ""
                if t:
                    title = t
        except Exception as e:
            log.warning("拉 fishaudio 模型 title 失败 %s: %s", ref_id, e)
    entry = {"uri": ref_id, "customName": title}
    voices.append(entry)
    _save_fishaudio_user_voices(voices)
    log.info("fishaudio 加用户音色: %s (%s)", ref_id, title)
    return {"ok": True, "voice": entry, "voices": voices}


@router.delete("/api/tts/fishaudio/voice/{ref_id}")
async def fishaudio_user_voice_delete(ref_id: str):
    voices = _load_fishaudio_user_voices()
    new = [v for v in voices if v.get("uri") != ref_id]
    if len(new) == len(voices):
        raise HTTPException(404, "没找到（可能是精选默认，不能从这里删）")
    _save_fishaudio_user_voices(new)
    return {"ok": True, "voices": new}
