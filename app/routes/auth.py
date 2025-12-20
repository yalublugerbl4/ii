from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from .. import schemas
from ..auth import create_access_token, get_current_user, require_admin, verify_telegram_init_data
from ..db import get_session
from ..models import Admin, User

router = APIRouter(prefix="/auth", tags=["auth"])


class UpdateEmailRequest(BaseModel):
    email: Optional[EmailStr] = None


class AddAdminRequest(BaseModel):
    tgid: int


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
    return schemas.UserOut(
        tgid=db_user.tgid, 
        balance=float(db_user.balance),
        email=db_user.email
    )


@router.get("/check-admin")
async def check_admin(
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Проверить, является ли пользователь админом"""
    result = await session.execute(select(Admin).where(Admin.tgid == user.tgid))
    is_admin = result.scalars().first() is not None
    return {"isAdmin": is_admin}


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
        user=schemas.UserOut(tgid=tgid, balance=float(user.balance), email=user.email),
        isAdmin=is_admin,
    )


@router.put("/email")
async def update_email(
    body: UpdateEmailRequest,
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Обновить email пользователя для получения чеков"""
    result = await session.execute(select(User).where(User.tgid == user.tgid))
    db_user = result.scalars().first()
    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")
    
    db_user.email = body.email
    await session.commit()
    await session.refresh(db_user)
    
    return {"email": db_user.email}


@router.post("/add-admin")
async def add_admin(
    body: AddAdminRequest,
    admin_user=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Добавить нового админа (только для существующих админов)"""
    # Проверяем, существует ли пользователь
    result = await session.execute(select(User).where(User.tgid == body.tgid))
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Проверяем, не является ли уже админом
    result = await session.execute(select(Admin).where(Admin.tgid == body.tgid))
    existing_admin = result.scalars().first()
    if existing_admin:
        raise HTTPException(status_code=400, detail="User is already an admin")
    
    # Добавляем админа
    new_admin = Admin(tgid=body.tgid)
    session.add(new_admin)
    await session.commit()
    
    return {"message": "Admin added successfully", "tgid": body.tgid}

