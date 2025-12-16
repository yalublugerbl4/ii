import hashlib
import hmac
import time
from typing import Any, Dict

import jwt
from fastapi import Depends, HTTPException, status
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
    creds: HTTPAuthorizationCredentials = Depends(auth_scheme),
    session: AsyncSession = Depends(get_session),
) -> User:
    if creds is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Auth required")
    token = creds.credentials
    try:
        decoded = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    tgid = decoded.get("tgid")
    if tgid is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    result = await session.execute(select(User).where(User.tgid == tgid))
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


async def require_admin(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> User:
    result = await session.execute(select(Admin).where(Admin.tgid == user.tgid))
    admin = result.scalars().first()
    if not admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user

