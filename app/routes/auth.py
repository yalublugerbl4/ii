from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request
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
    request: Request,
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    r_tgid: Optional[int] = Query(None, description="Telegram ID пригласившего"),  # Реферальный tgid из query параметра
):
    """Получить текущего пользователя с балансом"""
    # Пробуем получить start_param из initData (для параметра startapp)
    start_param = None
    try:
        init_data = request.headers.get("x-telegram-initdata") or request.headers.get("X-Telegram-InitData") or request.headers.get("X-Telegram-Initdata")
        if init_data:
            import urllib.parse
            if '%' in init_data:
                init_data = urllib.parse.unquote(init_data)
            pairs = init_data.split("&")
            for p in pairs:
                if "=" in p:
                    k, v = p.split("=", 1)
                    if k == "start_param":
                        start_param = urllib.parse.unquote(v)
                        break
    except Exception:
        pass
    
    # Обрабатываем start_param (формат: r_tgid_123456789)
    if start_param and start_param.startswith("r_tgid_"):
        try:
            tgid_str = start_param.replace("r_tgid_", "")
            r_tgid_from_start = int(tgid_str)
            if not r_tgid or r_tgid_from_start != r_tgid:
                r_tgid = r_tgid_from_start
        except (ValueError, AttributeError):
            pass
    
    result = await session.execute(select(User).where(User.tgid == user.tgid))
    db_user = result.scalars().first()
    if not db_user:
        # Если пользователь не найден, создаем его
        # Обрабатываем реферальный tgid, если он передан
        referred_by_tgid = None
        if r_tgid:
            # Проверяем, что пригласивший существует и это не сам пользователь
            if r_tgid != user.tgid:
                result = await session.execute(select(User).where(User.tgid == r_tgid))
                referrer = result.scalars().first()
                if referrer:  # Если пригласивший существует
                    referred_by_tgid = r_tgid
        
        db_user = User(tgid=user.tgid, referred_by=referred_by_tgid)
        session.add(db_user)
        await session.commit()
        await session.refresh(db_user)
    else:
        # Если пользователь уже существует, но у него нет referred_by и передан r_tgid
        # (для случая, когда пользователь уже был создан, но реферал еще не установлен)
        if not db_user.referred_by and r_tgid and r_tgid != user.tgid:
            result = await session.execute(select(User).where(User.tgid == r_tgid))
            referrer = result.scalars().first()
            if referrer:
                db_user.referred_by = r_tgid
                await session.commit()
                await session.refresh(db_user)
    
    return schemas.UserOut(
        tgid=db_user.tgid, 
        balance=float(db_user.balance),
        email=db_user.email
    )


@router.get("/referral-link")
async def get_referral_link(
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Получить реферальную ссылку пользователя"""
    result = await session.execute(select(User).where(User.tgid == user.tgid))
    db_user = result.scalars().first()
    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Формируем реферальную ссылку для открытия Mini App
    from ..settings import settings
    bot_username = getattr(settings, 'bot_username', None)
    
    if bot_username:
        # Используем Direct Link для открытия Mini App с параметром startapp
        # Формат: https://t.me/bot_username/direct_link_name?startapp=r_tgid_123456789
        # Direct Link обеспечивает появление диалога разрешения при первом открытии
        direct_link_name = getattr(settings, 'direct_link_name', 'app')
        referral_link = f"https://t.me/{bot_username}/{direct_link_name}?startapp=r_tgid_{db_user.tgid}"
    else:
        # Fallback: используем frontend_url если bot_username не указан
        frontend_url = getattr(settings, 'frontend_url', 'https://iiapp-66742.web.app')
        referral_link = f"{frontend_url}?r_tgid={db_user.tgid}"
    
    return {"referral_link": referral_link}


@router.get("/mini-app-link")
async def get_mini_app_link(
    page: Optional[str] = Query(None, description="Страница для открытия (например: generator_image)"),
    model: Optional[str] = Query(None, description="Модель для выбора (например: nano-banana-pro)"),
    _: None = Depends(require_admin),
):
    """Получить ссылку для открытия Mini App на конкретной странице (только для админов)
    
    Возвращает два типа ссылок:
    1. web_app_url - для использования в кнопках web_app (прямой URL Mini App)
    2. bot_link - для использования в обычных ссылках бота (через startapp)
    """
    from ..settings import settings
    bot_username = getattr(settings, 'bot_username', None)
    frontend_url = getattr(settings, 'frontend_url', 'https://iiapp-66742.web.app')
    
    # Формируем прямой URL Mini App для кнопки web_app
    if page and model:
        # Формат: /generator/image?model=nano-banana-pro
        web_app_url = f"{frontend_url}/{page.replace('_', '/')}?model={model}"
    elif page:
        web_app_url = f"{frontend_url}/{page.replace('_', '/')}"
    else:
        web_app_url = frontend_url
    
    # Формируем ссылку на бота для обычных ссылок (через startapp)
    bot_link = None
    if bot_username:
        direct_link_name = getattr(settings, 'direct_link_name', 'app')
        if page and model:
            startapp_param = f"{page}_{model}"
        elif page:
            startapp_param = page
        else:
            startapp_param = ""
        
        if startapp_param:
            bot_link = f"https://t.me/{bot_username}/{direct_link_name}?startapp={startapp_param}"
        else:
            bot_link = f"https://t.me/{bot_username}/{direct_link_name}"
    
    return {
        "web_app_url": web_app_url,  # Для кнопок web_app в Telegram
        "bot_link": bot_link,  # Для обычных ссылок через бота
        "page": page,
        "model": model
    }


@router.get("/all-mini-app-links")
async def get_all_mini_app_links(
    _: None = Depends(require_admin),
):
    """Получить ссылки для всех доступных нейросетей (только для админов)
    
    Возвращает ссылки для всех моделей генерации изображений и видео,
    а также для других функций (удаление фона, улучшение качества).
    """
    from ..settings import settings
    bot_username = getattr(settings, 'bot_username', None)
    frontend_url = getattr(settings, 'frontend_url', 'https://iiapp-66742.web.app')
    direct_link_name = getattr(settings, 'direct_link_name', 'app')
    
    links = []
    
    # Модели для генерации изображений
    image_models = [
        {"id": "google/nano-banana-edit", "name": "NanoBanana", "page": "generator/image"},
        {"id": "nano-banana-pro", "name": "NanoBanana PRO", "page": "generator/image"},
        {"id": "seedream/4.5-text-to-image", "name": "Seedream 4.5", "page": "generator/image"},
    ]
    
    for model in image_models:
        web_app_url = f"{frontend_url}/{model['page'].replace('_', '/')}?model={model['id']}"
        bot_link = None
        if bot_username:
            startapp_param = f"{model['page']}_{model['id']}"
            bot_link = f"https://t.me/{bot_username}/{direct_link_name}?startapp={startapp_param}"
        
        links.append({
            "type": "image",
            "model_id": model['id'],
            "model_name": model['name'],
            "web_app_url": web_app_url,
            "bot_link": bot_link,
            "startapp_param": f"{model['page']}_{model['id']}" if bot_username else None
        })
    
    # Модели для генерации видео
    video_models = [
        {"id": "grok-imagine/text-to-video", "name": "Grok Imagine", "page": "generator/video"},
        {"id": "veo3", "name": "Veo 3.1 Quality", "page": "generator/video"},
        {"id": "veo3_fast", "name": "Veo 3.1 Fast", "page": "generator/video"},
    ]
    
    for model in video_models:
        web_app_url = f"{frontend_url}/{model['page'].replace('_', '/')}?model={model['id']}"
        bot_link = None
        if bot_username:
            startapp_param = f"{model['page']}_{model['id']}"
            bot_link = f"https://t.me/{bot_username}/{direct_link_name}?startapp={startapp_param}"
        
        links.append({
            "type": "video",
            "model_id": model['id'],
            "model_name": model['name'],
            "web_app_url": web_app_url,
            "bot_link": bot_link,
            "startapp_param": f"{model['page']}_{model['id']}" if bot_username else None
        })
    
    # Другие функции (без модели)
    other_features = [
        {"name": "Генерация музыки", "page": "generator/music", "startapp": "generator_music"},
        {"name": "Удаление фона", "page": "generator/remove-bg", "startapp": "generator_remove-bg"},
        {"name": "Улучшение качества", "page": "generator/upscale", "startapp": "generator_upscale"},
    ]
    
    for feature in other_features:
        web_app_url = f"{frontend_url}/{feature['page']}"
        bot_link = None
        if bot_username:
            startapp_param = feature['startapp']
            bot_link = f"https://t.me/{bot_username}/{direct_link_name}?startapp={startapp_param}"
        
        links.append({
            "type": "feature",
            "model_id": None,
            "model_name": feature['name'],
            "web_app_url": web_app_url,
            "bot_link": bot_link,
            "startapp_param": feature['startapp'] if bot_username else None
        })
    
    return {
        "links": links,
        "total": len(links),
        "frontend_url": frontend_url,
        "bot_username": bot_username
    }


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
    body: schemas.TelegramAuthRequest, 
    session: AsyncSession = Depends(get_session),
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
        # Создаем пользователя без реферала (реферал будет установлен при первом запросе /auth/me с r_tgid)
        user = User(tgid=tgid, referred_by=None)
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

