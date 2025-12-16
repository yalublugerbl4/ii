import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional

import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from .db import get_session
from .models import Admin, User
from .settings import settings

auth_scheme = HTTPBearer(auto_error=False)


def _parse_init_data(init_data: str) -> Dict[str, str]:
    pairs = init_data.split("&")
    data: Dict[str, str] = {}
    for p in pairs:
        if "=" not in p:
            continue
        k, v = p.split("=", 1)
        data[k] = v
    return data


def verify_telegram_init_data(init_data: str) -> Dict[str, Any]:
    data = _parse_init_data(init_data)
    received_hash = data.pop("hash", None)
    if not received_hash:
        raise ValueError("hash missing")
    check_list = [f"{k}={v}" for k, v in sorted(data.items())]
    data_check_string = "\n".join(check_list)
    secret_key = hmac.new(
        b"WebAppData", settings.bot_token.encode(), hashlib.sha256
    ).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed_hash, received_hash):
        raise ValueError("initData hash mismatch")
    return data


def create_access_token(payload: Dict[str, Any]) -> str:
    exp = int(time.time()) + settings.jwt_expires_seconds
    to_encode = {**payload, "exp": exp}
    return jwt.encode(to_encode, settings.jwt_secret, algorithm="HS256")


async def get_current_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> User:
    """Получить текущего пользователя из Telegram initData"""
    import logging
    logger = logging.getLogger(__name__)
    
    # Пробуем получить заголовок разными способами
    init_data = (
        request.headers.get("x-telegram-initdata") or
        request.headers.get("X-Telegram-Initdata") or
        request.headers.get("X-Telegram-InitData")
    )
    
    # Логируем все заголовки для отладки
    all_headers = dict(request.headers)
    logger.info(f"All headers: {list(all_headers.keys())}")
    logger.info(f"x-telegram-initdata header present: {bool(init_data)}")
    
    if not init_data:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing x-telegram-initdata header")
    
    try:
        data = verify_telegram_init_data(init_data)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid initData: {str(exc)}")
    
    user_data = data.get("user")
    if not user_data:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="user missing in initData")
    
    try:
        tgid = int(user_data)
    except Exception:
        # Sometimes user is JSON string
        parsed = json.loads(user_data)
        tgid = int(parsed.get("id"))
    
    result = await session.execute(select(User).where(User.tgid == tgid))
    user = result.scalars().first()
    if not user:
        # Создаем пользователя, если его нет
        user = User(tgid=tgid)
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user


async def require_admin(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> User:
    result = await session.execute(select(Admin).where(Admin.tgid == user.tgid))
    admin = result.scalars().first()
    if not admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user

