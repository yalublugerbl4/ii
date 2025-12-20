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

router = APIRouter(prefix="/templates", tags=["templates"])
logger = logging.getLogger(__name__)


def convert_yandex_disk_url(url: str) -> str:
    """Преобразует ссылку Яндекс Диска в прямую ссылку на файл"""
    if not url or not isinstance(url, str):
        return url
    
    original_url = url
    
    # Паттерн для ссылок вида https://disk.yandex.ru/i/<id>
    pattern_i = r'https?://disk\.yandex\.ru/i/([a-zA-Z0-9_-]+)'
    match_i = re.search(pattern_i, url)
    if match_i:
        file_id = match_i.group(1)
        logger.info(f"Converting Yandex Disk link: {url} -> file_id: {file_id}")
        # Пробуем несколько вариантов преобразования
        # Вариант 1: через getfile.dokpub.com
        converted_url = f"https://getfile.dokpub.com/yandex/get/{file_id}"
        logger.info(f"Converted to: {converted_url}")
        return converted_url
    
    # Паттерн для ссылок вида https://disk.yandex.ru/d/<id>
    pattern_d = r'https?://disk\.yandex\.ru/d/([a-zA-Z0-9_-]+)'
    match_d = re.search(pattern_d, url)
    if match_d:
        file_id = match_d.group(1)
        logger.info(f"Converting Yandex Disk link: {url} -> file_id: {file_id}")
        converted_url = f"https://getfile.dokpub.com/yandex/get/{file_id}"
        logger.info(f"Converted to: {converted_url}")
        return converted_url
    
    # Если это уже прямая ссылка или другой формат, возвращаем как есть
    logger.info(f"Yandex Disk link not matched, returning original: {url}")
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
    path = await upload_file_stream(file)
    # KIE возвращает полный URL или относительный путь
    if path.startswith("http"):
        url = path
    else:
        from ..settings import settings
        url = f"{settings.kie_file_upload_base}/{path.lstrip('/')}"
    return {"url": url}


class PreviewUrlRequest(BaseModel):
    url: str


@router.post("/upload/preview-from-url")
async def upload_preview_from_url(
    request: PreviewUrlRequest,
    _: None = Depends(require_admin),
):
    """Загрузить изображение по URL (например, с Яндекс Диска) и сохранить на KIE"""
    import httpx
    from ..settings import settings
    from io import BytesIO
    
    url = request.url
    logger.info(f"Uploading preview from URL: {url}")
    
    # Преобразуем ссылку Яндекс Диска, если нужно
    converted_url = convert_yandex_disk_url(url)
    logger.info(f"Converted URL: {converted_url}")
    
    try:
        # Загружаем изображение по URL
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(converted_url)
            response.raise_for_status()
            
            # Проверяем, что это изображение
            content_type = response.headers.get('content-type', '')
            if not content_type.startswith('image/'):
                # Если не изображение, пробуем оригинальную ссылку
                logger.warning(f"Converted URL returned non-image content type: {content_type}, trying original URL")
                response = await client.get(url, follow_redirects=True)
                response.raise_for_status()
                content_type = response.headers.get('content-type', '')
                if not content_type.startswith('image/'):
                    raise HTTPException(status_code=400, detail="URL does not point to an image")
            
            # Создаем временный файл из ответа
            file_content = response.content
            file_obj = BytesIO(file_content)
            
            # Определяем расширение файла
            ext = 'jpg'
            if 'png' in content_type:
                ext = 'png'
            elif 'gif' in content_type:
                ext = 'gif'
            elif 'webp' in content_type:
                ext = 'webp'
            
            # Создаем UploadFile из содержимого
            from fastapi import UploadFile
            upload_file = UploadFile(
                filename=f"preview.{ext}",
                file=file_obj
            )
            
            # Загружаем на KIE
            path = await upload_file_stream(upload_file)
            
            # KIE возвращает полный URL или относительный путь
            if path.startswith("http"):
                final_url = path
            else:
                final_url = f"{settings.kie_file_upload_base}/{path.lstrip('/')}"
            
            logger.info(f"Successfully uploaded to KIE: {final_url}")
            return {"url": final_url}
            
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error when uploading from URL: {e.response.status_code} - {e.response.text}")
        raise HTTPException(status_code=400, detail=f"Failed to download image from URL: HTTP {e.response.status_code}")
    except Exception as e:
        logger.error(f"Failed to upload preview from URL: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"Failed to upload image from URL: {str(e)}")

