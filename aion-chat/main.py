"""
Aion Chat — 入口文件
FastAPI app 创建、lifespan、静态文件挂载、路由注册
"""

import asyncio, json, logging, re
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

# 过滤高频轮询路径的 access log，避免淹没有用的日志
class _QuietCamFilter(logging.Filter):
    _noisy = ("/api/cam/frame", "/api/cam/status")
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(p in msg for p in self._noisy)

logging.getLogger("uvicorn.access").addFilter(_QuietCamFilter())

# 静默 Windows asyncio ProactorEventLoop 连接重置的噪音日志
logging.getLogger("asyncio").setLevel(logging.CRITICAL)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from config import BASE_DIR, PUBLIC_DIR, UPLOADS_DIR, SONGS_DIR, CODEX_UPLOADS_DIR, SCREENSHOTS_DIR, load_cam_config
from database import init_db, get_db
from ws import manager
from camera import cam
from voice import get_voice
from schedule import schedule_mgr

voice = get_voice()

from routes import chat, cam as cam_routes, files, settings, memories
from routes import voice as voice_routes
from routes import music as music_routes
from routes import schedule as schedule_routes
from routes import location as location_routes
from routes import heart_whispers as heart_whispers_routes
from routes import moments as moments_routes
from routes import diary as diary_routes
from routes import activity as activity_routes
from routes import book as book_routes
from routes import theater as theater_routes
from routes import date_theater as date_theater_routes
from routes import sleep as sleep_routes
from routes import todos as todos_routes
from routes import avatar as avatar_routes
from routes import ghost_forest as ghost_forest_routes
from routes import gift as gift_routes
from routes import fund as fund_routes
from routes import wallpaper as wallpaper_routes
from routes import playground as playground_routes
from routes import chatroom as chatroom_routes
from routes import doudizhu as doudizhu_routes
from routes import seeky as seeky_routes
from routes import wallet as wallet_routes
from routes import connor_wallet as connor_wallet_routes
from routes import health as health_routes
from routes import phone_screen as phone_screen_routes
from routes import search as search_routes
from routes import autonomy as autonomy_routes
from routes import persona_evolution as persona_evolution_routes
from routes import wishes as wishes_routes
from routes import xhs_lite as xhs_lite_routes
from routes import capabilities as capabilities_routes
from routes import wechat as wechat_routes
from activity import pc_tracker, pc_display_tracker
from memory import auto_digest
from chatroom import _connor_1v1_auto_digest_loop
from fund import fund_scheduler
from autonomy import idle_autonomy_mgr
from persona_evolution import main_ai_persona_evolution_loop, connor_persona_evolution_loop
from asset_manifest import get_client_asset_manifest
from home_assistant_events import ha_event_listener
from wechat_openclaw_runtime import openclaw_weixin_runtime


# ── 自动记忆总结定时任务 ──────────────────────────
async def _auto_digest_loop():
    """每 30 分钟检查一次，若用户已 30 分钟未发消息（Aion 私聊+群聊）则自动总结"""
    import aiosqlite, time as _time
    while True:
        await asyncio.sleep(30 * 60)  # 30 分钟
        try:
            # 检查最后一条用户消息的时间（Aion 私聊 + 群聊取最新的）
            latest_ts = 0
            async with get_db() as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute(
                    "SELECT created_at FROM messages WHERE role='user' ORDER BY created_at DESC LIMIT 1"
                )
                row = await cur.fetchone()
                if row:
                    latest_ts = max(latest_ts, row["created_at"])
                cur = await db.execute(
                    "SELECT m.created_at FROM chatroom_messages m "
                    "JOIN chatroom_rooms r ON r.id = m.room_id "
                    "WHERE m.sender='user' AND r.type='group' "
                    "ORDER BY m.created_at DESC LIMIT 1"
                )
                row = await cur.fetchone()
                if row:
                    latest_ts = max(latest_ts, row["created_at"])
            if latest_ts == 0:
                continue
            elapsed = _time.time() - latest_ts
            if elapsed < 30 * 60:
                print(f"[auto_digest] 用户 {elapsed/60:.0f} 分钟前仍在对话，跳过")
                continue
            print(f"[auto_digest] 用户已 {elapsed/60:.0f} 分钟未对话，开始自动总结")
            result = await auto_digest()
            print(f"[auto_digest] {result.get('message', '')}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[auto_digest] ❌ 异常: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    print(f"[Auth] 登录密码: {auth.get_secret()}（存于 data/auth_secret.txt，改成 off 可关闭鉴权）")
    # 哄睡孤儿状态回收：上次进程被杀时留在 generating/synthesizing 的条目改判 failed，
    # 否则它们会被合成端点永久挡住重试
    try:
        import bedtime
        await bedtime.reclaim_orphaned()
    except Exception as e:
        print(f"[Sleep] ❌ 孤儿状态清理异常: {e}")
    loop = asyncio.get_running_loop()
    # 各子系统启动互相独立：任何一个失败（摄像头被占、HA 离线、配置缺字段…）
    # 都不应拖死整个应用，聊天主链路必须先活着
    cam.set_event_loop(loop)
    try:
        cam_cfg = load_cam_config()
        if cam_cfg.get("monitor_enabled"):
            if cam_cfg.get("active_source") == "esp32":
                cam.open_esp32()
            else:
                cam.open_camera(cam_cfg.get("camera_index", 0))
            cam.start_monitoring()
    except Exception as e:
        print(f"[Camera] ❌ 启动异常: {e}")
    # 语音模块初始化
    voice.set_event_loop(loop)
    voice.set_ws_manager(manager)
    # 日程/闹铃模块初始化
    schedule_mgr.set_event_loop(loop)
    try:
        schedule_mgr.start()
    except Exception as e:
        print(f"[Schedule] ❌ 启动异常: {e}")
    # PC 活动采集
    pc_tracker.set_event_loop(loop)
    try:
        pc_tracker.start()
    except Exception as e:
        print(f"[PCActivity] ❌ 启动异常: {e}")
    try:
        pc_display_tracker.start()
    except Exception as e:
        print(f"[PCDisplay] ❌ 启动异常: {e}")
    # 基金监控定时任务
    fund_scheduler.set_event_loop(loop)
    try:
        fund_scheduler.start()
    except Exception as e:
        print(f"[Fund] ❌ 启动异常: {e}")
    # 自动记忆总结定时任务
    digest_task = asyncio.create_task(_auto_digest_loop())
    cr_digest_task = asyncio.create_task(_connor_1v1_auto_digest_loop())
    persona_evolution_task = asyncio.create_task(main_ai_persona_evolution_loop())
    connor_persona_evolution_task = asyncio.create_task(connor_persona_evolution_loop())
    try:
        idle_autonomy_mgr.start()
    except Exception as e:
        print(f"[Autonomy] ❌ 启动异常: {e}")
    try:
        ha_event_listener.start()
    except Exception as e:
        print(f"[HA] ❌ 启动异常: {e}")
    try:
        openclaw_weixin_runtime.start()
    except Exception as e:
        print(f"[Weixin] ❌ 启动异常: {e}")
    yield
    await openclaw_weixin_runtime.stop()
    await ha_event_listener.stop()
    idle_autonomy_mgr.stop()
    connor_persona_evolution_task.cancel()
    persona_evolution_task.cancel()
    cr_digest_task.cancel()
    digest_task.cancel()
    fund_scheduler.stop()
    pc_display_tracker.stop()
    pc_tracker.stop()
    schedule_mgr.stop()
    voice.stop()
    cam.close_camera()


app = FastAPI(lifespan=lifespan)

# Static files keep their existing URLs, so browsers must revalidate them. The
# Android app additionally uses /api/client-assets to share verified objects
# between LAN, Tailscale, and Cloudflare origins.
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

import auth

# 无需登录即可访问的路径（登录流程本身 + PWA 安装所需资源）
_AUTH_EXEMPT_PATHS = {"/login", "/api/login", "/sw.js", "/manifest.json", "/favicon.ico", "/api/client-assets"}
# AionApp 安卓端原生 OkHttp 请求（AionPushService/AionAccessibilityService）还不会带
# X-Aion-Token，这些端点暂时豁免；等 App 侧加上 token 头后应逐步收紧。
# 剩下的都是 <img src>/<audio src> 类媒体资源（浏览器同源会带 cookie，但 Range/SW
# 缓存行为与 fetch 不同，未逐个实测前不动）或网页端 + App 双用的接口。
_AUTH_EXEMPT_PREFIXES = (
    "/public/",
    "/api/location/",
    "/api/health/ring/",
    "/api/music/stream/",
    "/api/tts/audio/",
    "/api/theater/tts/audio/",
    "/api/gift/thumbnail/",
)
# 已收紧（2026-07-30）：
# - /api/phone-screen/、/api/activity/report、/api/cam/esp32/frame 移除：
#   网页端零引用（纯 App/硬件用途），且 cam_config 是 active_source=local、
#   esp32_cam_url 为空，ESP32 没在用。启用 APK 前需给 App 加 token。
# - /api/diaries/ 整前缀移除，换成下面的精确豁免：原先把日记增删改查全放行了，
#   而 App 侧（MediaCacheStore）只需要日记 TTS 音频。
# 精确豁免的完整路径（正则匹配，只放行 App 真正需要的那一个 GET/HEAD）
_AUTH_EXEMPT_PATTERNS = (
    re.compile(r"^/api/diaries/[^/]+/tts/audio$"),
)

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not auth.auth_enabled():
            return await call_next(request)
        path = request.url.path
        if path in _AUTH_EXEMPT_PATHS or path.startswith(_AUTH_EXEMPT_PREFIXES):
            return await call_next(request)
        if any(p.match(path) for p in _AUTH_EXEMPT_PATTERNS):
            return await call_next(request)
        if auth.check_request(request):
            return await call_next(request)
        client_ip = request.client.host if request.client else "?"
        print(f"[Auth] 401 {request.method} {path} from {client_ip}")
        if path.startswith("/api/"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return RedirectResponse("/login", status_code=302)

_LOCAL_PREFIXES = ("127.", "192.168.", "::1", "localhost")

class NoCacheStaticMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # 壁纸大文件只允许本地 IP 访问，远程设备不需要也避免占带宽
        if request.url.path.startswith("/public/wallpaper/"):
            client_ip = request.client.host if request.client else ""
            if not any(client_ip.startswith(p) for p in _LOCAL_PREFIXES):
                return Response("wallpaper only available on local network", status_code=403)
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "public, max-age=0, must-revalidate"
        return response

app.add_middleware(NoCacheStaticMiddleware)
app.add_middleware(AuthMiddleware)


# ── 登录 ──────────────────────────────────────────
@app.get("/login")
async def login_page():
    return FileResponse(BASE_DIR / "static" / "login.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.post("/api/login")
async def login(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    if str(body.get("password", "")).strip() == auth.get_secret():
        resp = JSONResponse({"ok": True})
        resp.set_cookie(
            auth.COOKIE_NAME, auth.cookie_value(),
            max_age=auth.COOKIE_MAX_AGE, httponly=True, samesite="lax",
        )
        return resp
    return JSONResponse({"ok": False, "error": "wrong password"}, status_code=401)

# 静态文件
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")
app.mount("/songs", StaticFiles(directory=str(SONGS_DIR)), name="songs")
app.mount("/cr-uploads", StaticFiles(directory=str(CODEX_UPLOADS_DIR)), name="cr-uploads")
app.mount("/public", StaticFiles(directory=str(PUBLIC_DIR)), name="public")
app.mount("/screenshots", StaticFiles(directory=str(SCREENSHOTS_DIR)), name="screenshots")
app.mount("/aion-pet", StaticFiles(directory=str(BASE_DIR.parent / "AionPet")), name="aion-pet")

# 路由
app.include_router(chat.router)
app.include_router(cam_routes.router)
app.include_router(files.router)
app.include_router(settings.router)
app.include_router(memories.router)
app.include_router(voice_routes.router)
app.include_router(music_routes.router)
app.include_router(schedule_routes.router)
app.include_router(location_routes.router)
app.include_router(heart_whispers_routes.router)
app.include_router(moments_routes.router)
app.include_router(diary_routes.router)
app.include_router(activity_routes.router)
app.include_router(book_routes.router)
app.include_router(theater_routes.router)
app.include_router(date_theater_routes.router)
app.include_router(sleep_routes.router)
app.include_router(todos_routes.router)
app.include_router(avatar_routes.router)
app.include_router(ghost_forest_routes.router)
app.include_router(gift_routes.router)
app.include_router(fund_routes.router)
app.include_router(wallpaper_routes.router)
app.include_router(playground_routes.router)
app.include_router(chatroom_routes.router)
app.include_router(doudizhu_routes.router)
app.include_router(seeky_routes.router)
app.include_router(wallet_routes.router)
app.include_router(connor_wallet_routes.router)
app.include_router(health_routes.router)
app.include_router(phone_screen_routes.router)
app.include_router(search_routes.router)
app.include_router(autonomy_routes.router)
app.include_router(persona_evolution_routes.router)
app.include_router(wishes_routes.router)
app.include_router(xhs_lite_routes.router)
app.include_router(capabilities_routes.router)
app.include_router(wechat_routes.router)


@app.get("/api/client-assets")
async def client_assets():
    return JSONResponse(
        get_client_asset_manifest(),
        headers={"Cache-Control": "no-store"},
    )


# 页面
@app.get("/")
async def home():
    return FileResponse(BASE_DIR / "static" / "home.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/chat")
async def chat_page():
    return FileResponse(BASE_DIR / "static" / "chat.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/settings")
async def settings_page():
    return FileResponse(BASE_DIR / "static" / "settings.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/capabilities")
async def capabilities_page():
    return FileResponse(BASE_DIR / "static" / "capabilities.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/worldbook")
async def worldbook_page():
    return FileResponse(BASE_DIR / "static" / "worldbook.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/sleep")
async def sleep_page():
    return FileResponse(BASE_DIR / "static" / "sleep.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/memory")
async def memory_page():
    return FileResponse(BASE_DIR / "static" / "memory.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/schedule")
async def schedule_page():
    return FileResponse(BASE_DIR / "static" / "schedule.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/camera")
async def camera_page():
    return FileResponse(BASE_DIR / "static" / "camera.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/monitor-logs")
async def monitor_logs_page():
    return FileResponse(BASE_DIR / "static" / "monitor-logs.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/location")
async def location_page():
    return FileResponse(BASE_DIR / "static" / "location.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/heart-whispers")
async def heart_whispers_page():
    return FileResponse(BASE_DIR / "static" / "heart-whispers.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/moments")
async def moments_page():
    return FileResponse(BASE_DIR / "static" / "moments.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/diary")
async def diary_page():
    return FileResponse(BASE_DIR / "static" / "diary.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/activity-logs")
async def activity_logs_page():
    return FileResponse(BASE_DIR / "static" / "activity-logs.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/reading")
async def reading_page():
    return FileResponse(BASE_DIR / "static" / "reading.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/theater")
async def theater_page():
    return FileResponse(BASE_DIR / "static" / "theater.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/date-theater")
async def date_theater_page():
    return FileResponse(BASE_DIR / "static" / "date_theater.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/ghost-forest")
async def ghost_forest_page():
    return FileResponse(BASE_DIR / "static" / "ghost-forest.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/gift")
async def gift_page():
    return FileResponse(BASE_DIR / "static" / "gift.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/fund")
async def fund_page():
    return FileResponse(BASE_DIR / "static" / "fund.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/wallpaper")
async def wallpaper_page():
    return FileResponse(BASE_DIR / "static" / "wallpaper.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/playground")
async def playground_page():
    return FileResponse(BASE_DIR / "static" / "playground.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/chatroom")
async def chatroom_page():
    return FileResponse(BASE_DIR / "static" / "chatroom.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/doudizhu")
async def doudizhu_page():
    return FileResponse(BASE_DIR / "static" / "doudizhu.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/seeky")
async def seeky_page():
    return FileResponse(BASE_DIR / "static" / "seeky.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/wishes")
async def wishes_page():
    return FileResponse(BASE_DIR / "static" / "wishes.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/xhs-lite")
async def xhs_lite_page():
    return FileResponse(BASE_DIR / "static" / "xhs-lite.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/xhs-lite-logs")
async def xhs_lite_logs_page():
    return FileResponse(BASE_DIR / "static" / "xhs-lite-logs.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/health")
async def health_page():
    return FileResponse(BASE_DIR / "static" / "health.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/pet")
async def pet_page():
    return FileResponse(BASE_DIR / "static" / "pet.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/music")
async def music_page():
    return FileResponse(BASE_DIR / "static" / "music.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/todos")
async def todos_page():
    return FileResponse(BASE_DIR / "static" / "todos.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

# PWA：Service Worker 必须从根路径提供，作用域才能覆盖所有页面
@app.get("/sw.js")
async def service_worker():
    return FileResponse(BASE_DIR / "static" / "sw.js", media_type="application/javascript")

@app.get("/manifest.json")
async def manifest():
    return FileResponse(BASE_DIR / "static" / "manifest.json", media_type="application/manifest+json")

# WebSocket
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # BaseHTTPMiddleware 不拦 WebSocket，所以这里必须自己校验：
    # ws.broadcast 会把聊天/朋友圈/日记内容推给所有连上的客户端，不校验等于公网旁听。
    # 浏览器握手自带 aion_auth cookie，网页端无需改动；非浏览器客户端用 ?token=。
    # 注意：拒绝要在 accept 之前 close，否则等于先接受了连接。
    if auth.auth_enabled() and not auth.check_request(ws):
        client_ip = ws.client.host if ws.client else "?"
        logging.getLogger("ws").warning("WS 鉴权失败，拒绝连接 from %s", client_ip)
        await ws.close(code=1008)  # 1008 = policy violation
        return
    await manager.connect(ws)
    try:
        while True:
            text = await ws.receive_text()
            try:
                msg = json.loads(text)
                if msg.get("type") == "ping":
                    await ws.send_text(json.dumps({"type": "pong"}))
                elif msg.get("type") == "tts_state":
                    try:
                        active_at = float(msg.get("active_at")) if msg.get("active_at") is not None else None
                    except (TypeError, ValueError):
                        active_at = None
                    manager.set_tts_state(
                        ws,
                        msg.get("enabled", False),
                        msg.get("voice", ""),
                        can_play=msg.get("can_play", True),
                        active_at=active_at,
                    )
                elif msg.get("type") == "register_client":
                    manager.register_client_id(ws, msg.get("client_id", ""))
                elif msg.get("type") == "pet_state":
                    manager.set_pet_state(ws, msg.get("enabled", False))
                elif msg.get("type") == "step_diag":
                    # 手机回传的步数传感器诊断 → 转发给所有浏览器客户端
                    await manager.broadcast(msg, exclude=ws)
            except (json.JSONDecodeError, Exception):
                pass
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logging.getLogger("ws").warning("WS endpoint error: %s", e)
    finally:
        manager.disconnect(ws)


if __name__ == "__main__":
    import uvicorn
    import sys
    if "--reload" in sys.argv:
        uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=True)
    else:
        uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
