import re
import logging
from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from .. import schemas
from ..auth import get_current_user, require_admin
from ..db import get_session
from ..models import Template
from ..services.kie import upload_file_stream
from ..services.firebase_storage import upload_to_firebase_storage, upload_from_url_to_firebase_storage

router = APIRouter(prefix="/templates", tags=["templates"])
logger = logging.getLogger(__name__)


async def get_yandex_disk_direct_url(public_url: str) -> str:
    """Получает прямую ссылку на файл через API Яндекс Диска"""
    import httpx
    import urllib.parse
    
    try:
        # URL-кодируем оригинальную ссылку (без safe символов, чтобы закодировать все)
        encoded_url = urllib.parse.quote(public_url, safe='')
        api_url = f"https://cloud-api.yandex.net/v1/disk/public/resources/download?public_key={encoded_url}"
        
        logger.info(f"Requesting direct URL from Yandex Disk API for: {public_url}")
        logger.info(f"API URL: {api_url}")
        
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(api_url)
            response.raise_for_status()
            data = response.json()
            
            # API возвращает JSON с полем 'href' - это прямая ссылка на скачивание
            direct_url = data.get('href')
            if direct_url:
                logger.info(f"Got direct URL from Yandex Disk API: {direct_url}")
                return direct_url
            else:
                logger.warning(f"Yandex Disk API response missing 'href': {data}")
                return public_url
    except Exception as e:
        logger.error(f"Failed to get direct URL from Yandex Disk API: {e}", exc_info=True)
        return public_url


def convert_yandex_disk_url(url: str) -> str:
    """Преобразует ссылку Яндекс Диска в прямую ссылку на файл (синхронная версия для сохранения)"""
    if not url or not isinstance(url, str):
        return url
    
    # Проверяем, является ли это ссылкой Яндекс Диска
    if 'disk.yandex.ru/i/' in url or 'disk.yandex.ru/d/' in url:
        # Для синхронной функции просто возвращаем оригинальную ссылку
        # Реальное преобразование будет в асинхронной функции загрузки
        logger.info(f"Yandex Disk link detected: {url}")
        return url
    
    return url


@router.get("", response_model=list[schemas.TemplateOut])
async def list_templates(session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Template).order_by(Template.created_at.desc()))
    return result.scalars().all()


@router.get("/{template_id}", response_model=schemas.TemplateOut)
async def get_template(template_id: str, session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Template).where(Template.id == template_id))
    tpl = result.scalars().first()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    return tpl


@router.post("", response_model=schemas.TemplateOut)
async def create_template(
    payload: schemas.TemplateCreate,
    _: None = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    # Преобразуем ссылку Яндекс Диска в прямую ссылку, если нужно
    template_data = payload.dict()
    if template_data.get('preview_image_url'):
        template_data['preview_image_url'] = convert_yandex_disk_url(template_data['preview_image_url'])
    
    tpl = Template(**template_data)
    session.add(tpl)
    await session.commit()
    await session.refresh(tpl)
    return tpl


@router.put("/{template_id}", response_model=schemas.TemplateOut)
async def update_template(
    template_id: str,
    payload: schemas.TemplateUpdate,
    _: None = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(Template).where(Template.id == template_id))
    tpl = result.scalars().first()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    
    template_data = payload.dict()
    # Преобразуем ссылку Яндекс Диска в прямую ссылку, если нужно
    if template_data.get('preview_image_url'):
        template_data['preview_image_url'] = convert_yandex_disk_url(template_data['preview_image_url'])
    
    for k, v in template_data.items():
        setattr(tpl, k, v)
    session.add(tpl)
    await session.commit()
    await session.refresh(tpl)
    return tpl


@router.delete("/{template_id}")
async def delete_template(
    template_id: str,
    _: None = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(Template).where(Template.id == template_id))
    tpl = result.scalars().first()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    await session.delete(tpl)
    await session.commit()
    return {"ok": True}


@router.post("/upload/preview")
async def upload_preview(
    file: UploadFile,
    _: None = Depends(require_admin),
):
    """Загрузить превью изображение в Firebase Storage"""
    try:
        url = await upload_to_firebase_storage(file, folder="template-previews")
        return {"url": url}
    except Exception as e:
        logger.error(f"Failed to upload to Firebase Storage: {e}", exc_info=True)
        # Fallback на KIE, если Firebase не настроен
        try:
            path = await upload_file_stream(file)
            if path.startswith("http"):
                url = path
            else:
                from ..settings import settings
                url = f"{settings.kie_file_upload_base}/{path.lstrip('/')}"
            return {"url": url}
        except Exception as fallback_error:
            logger.error(f"Fallback to KIE also failed: {fallback_error}")
            raise HTTPException(status_code=500, detail=f"Failed to upload file: {str(e)}")


class PreviewUrlRequest(BaseModel):
    url: str


@router.post("/upload/preview-from-url")
async def upload_preview_from_url(
    request: PreviewUrlRequest,
    _: None = Depends(require_admin),
):
    """Загрузить изображение по URL и сохранить в Firebase Storage"""
    url = request.url
    logger.info(f"Uploading preview from URL: {url}")
    
    # Если это ссылка Яндекс Диска, получаем прямую ссылку через API
    if 'disk.yandex.ru/i/' in url or 'disk.yandex.ru/d/' in url:
        logger.info(f"Yandex Disk link detected, getting direct URL via API: {url}")
        try:
            direct_url = await get_yandex_disk_direct_url(url)
            if direct_url and direct_url != url:
                url = direct_url
                logger.info(f"Using direct URL from Yandex Disk API: {direct_url}")
        except Exception as e:
            logger.warning(f"Error getting direct URL from Yandex Disk API: {e}, using original URL")
    
    try:
        # Загружаем в Firebase Storage
        final_url = await upload_from_url_to_firebase_storage(url, folder="template-previews")
        logger.info(f"Successfully uploaded to Firebase Storage: {final_url}")
        return {"url": final_url}
    except Exception as e:
        logger.error(f"Failed to upload to Firebase Storage: {e}", exc_info=True)
        raise HTTPException(
            status_code=400, 
            detail=f"Не удалось загрузить изображение: {str(e)}"
        )

