import hashlib
import hmac
import time

from fastapi import Depends, HTTPException, Request
from itsdangerous import URLSafeTimedSerializer

from app.config import settings
from app.database import get_user, touch_user, upsert_user

COOKIE_NAME = "sp_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30


def verify_telegram_auth(data, bot_token):
    data = dict(data)
    check_hash = data.pop("hash", None)
    data.pop("next", None)
    if not check_hash:
        return False
    auth_date = data.get("auth_date")
    if auth_date:
        try:
            if abs(int(time.time()) - int(auth_date)) > 7 * 86400:
                return False
        except (TypeError, ValueError):
            return False
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hashlib.sha256(bot_token.encode("utf-8")).digest()
    h = hmac.new(secret, data_check_string.encode("utf-8"), hashlib.sha256)
    return hmac.compare_digest(h.hexdigest(), check_hash)


def make_session(telegram_id, full_name=None):
    serializer = URLSafeTimedSerializer(settings.SESSION_SECRET)
    return serializer.dumps({"tg_id": telegram_id, "name": full_name or ""})


def load_session(token):
    serializer = URLSafeTimedSerializer(settings.SESSION_SECRET)
    try:
        return serializer.loads(token, max_age=SESSION_MAX_AGE)
    except Exception:
        return None


async def get_current_user(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    payload = load_session(token)
    if not payload or not payload.get("tg_id"):
        return None
    try:
        tg_id = int(payload["tg_id"])
    except (TypeError, ValueError):
        return None
    user = get_user(tg_id)
    if user is None:
        upsert_user(tg_id, payload.get("name"))
        user = get_user(tg_id)
    else:
        touch_user(tg_id)
    return user


async def get_admin_user(request: Request, user=Depends(get_current_user)):
    if user is None or not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user
