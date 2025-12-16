from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from .. import schemas
from ..auth import create_access_token, verify_telegram_init_data
from ..db import get_session
from ..models import Admin, User

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/me", response_model=schemas.UserOut)
async def get_me(
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Получить текущего пользователя с балансом"""
    result = await session.execute(select(User).where(User.tgid == user.tgid))
    db_user = result.scalars().first()
    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")
    return schemas.UserOut(tgid=db_user.tgid, balance=float(db_user.balance))


@router.post("/telegram", response_model=schemas.TelegramAuthResponse)
async def auth_telegram(
    body: schemas.TelegramAuthRequest, session: AsyncSession = Depends(get_session)
):
    try:
        data = verify_telegram_init_data(body.initData)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    user_data = data.get("user")
    if not user_data:
        raise HTTPException(status_code=400, detail="user missing in initData")
    try:
        tgid = int(user_data)
    except Exception:
        # Sometimes user is JSON string
        import json

        parsed = json.loads(user_data)
        tgid = int(parsed.get("id"))

    result = await session.execute(select(User).where(User.tgid == tgid))
    user = result.scalars().first()
    if not user:
        user = User(tgid=tgid)
        session.add(user)
        await session.commit()
    result = await session.execute(select(Admin).where(Admin.tgid == tgid))
    is_admin = result.scalars().first() is not None
    token = create_access_token({"tgid": tgid})
    await session.refresh(user)
    return schemas.TelegramAuthResponse(
        accessToken=token,
        user=schemas.UserOut(tgid=tgid, balance=float(user.balance)),
        isAdmin=is_admin,
    )

