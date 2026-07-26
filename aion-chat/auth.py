"""
极简共享密钥鉴权

- 密钥存在 data/auth_secret.txt（首次启动自动生成，启动日志会打印）
- 浏览器：访问任意页面会被带到 /login，输入一次密码后种长期 cookie
- 非浏览器客户端：请求头 X-Aion-Token: <密钥> 或 URL 参数 ?token=<密钥>
- 想临时关闭鉴权：把 data/auth_secret.txt 内容改成 off
"""
import hashlib
import secrets as _secrets

from config import DATA_DIR

AUTH_SECRET_FILE = DATA_DIR / "auth_secret.txt"
COOKIE_NAME = "aion_auth"
COOKIE_MAX_AGE = 365 * 24 * 3600

_cached_secret = None


def get_secret() -> str:
    global _cached_secret
    if _cached_secret:
        return _cached_secret
    try:
        s = AUTH_SECRET_FILE.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        s = ""
    if not s:
        s = _secrets.token_urlsafe(9)
        AUTH_SECRET_FILE.write_text(s, encoding="utf-8")
    _cached_secret = s
    return s


def auth_enabled() -> bool:
    return get_secret().lower() != "off"


def cookie_value() -> str:
    return hashlib.sha256(get_secret().encode("utf-8")).hexdigest()


def is_authed_cookies(cookies) -> bool:
    return cookies.get(COOKIE_NAME, "") == cookie_value()


def check_request(request) -> bool:
    """HTTP 请求鉴权：cookie / X-Aion-Token 头 / ?token= 任一命中即通过"""
    if is_authed_cookies(request.cookies):
        return True
    token = request.headers.get("x-aion-token") or request.query_params.get("token")
    return bool(token) and token == get_secret()
