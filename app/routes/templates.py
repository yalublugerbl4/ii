import logging
from typing import Tuple
from fastapi import APIRouter, Depends, HTTPException, UploadFile, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from io import BytesIO
from PIL import Image

from .. import schemas
from ..auth import get_current_user, require_admin
from ..db import get_session
from ..models import Template
from ..services.kie import upload_file_stream

router = APIRouter(prefix="/templates", tags=["templates"])
logger = logging.getLogger(__name__)


def optimize_image(image_data: bytes, max_width: int = 1200, max_height: int = 1800, quality: int = 85) -> Tuple[bytes, str]:
    """
    Оптимизирует изображение: изменяет размер, сжимает и конвертирует в JPEG.
    
    Args:
        image_data: Байты исходного изображения
        max_width: Максимальная ширина (по умолчанию 1200px для карточек 2:3)
        max_height: Максимальная высота (по умолчанию 1800px для карточек 2:3)
        quality: Качество JPEG (0-100, по умолчанию 85)
    
    Returns:
        tuple: (оптимизированные байты, content_type)
    """
    try:
        # Открываем изображение
        img = Image.open(BytesIO(image_data))
        
        # Конвертируем RGBA в RGB если нужно (для JPEG)
        if img.mode in ('RGBA', 'LA', 'P'):
            # Создаем белый фон для прозрачных изображений
            background = Image.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'P':
                img = img.convert('RGBA')
            background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
            img = background
        elif img.mode != 'RGB':
            img = img.convert('RGB')
        
        # Получаем текущие размеры
        width, height = img.size
        
        # Вычисляем новые размеры с сохранением пропорций
        if width > max_width or height > max_height:
            ratio = min(max_width / width, max_height / height)
            new_width = int(width * ratio)
            new_height = int(height * ratio)
            img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
            logger.info(f"Resized image from {width}x{height} to {new_width}x{new_height}")
        
        # Сохраняем в JPEG с оптимизацией
        output = BytesIO()
        img.save(output, format='JPEG', quality=quality, optimize=True, progressive=True)
        output.seek(0)
        
        optimized_data = output.getvalue()
        original_size = len(image_data)
        optimized_size = len(optimized_data)
        compression_ratio = (1 - optimized_size / original_size) * 100
        
        logger.info(f"Optimized image: {original_size} bytes -> {optimized_size} bytes ({compression_ratio:.1f}% reduction)")
        
        return optimized_data, 'image/jpeg'
        
    except Exception as e:
        logger.error(f"Failed to optimize image: {e}", exc_info=True)
        # Если оптимизация не удалась, возвращаем оригинал
        return image_data, 'image/jpeg'


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


@router.get("/{template_id}/preview")
async def get_template_preview(
    template_id: str,
    session: AsyncSession = Depends(get_session),
):
    """Получить превью изображение шаблона из базы данных с кэшированием"""
    result = await session.execute(select(Template).where(Template.id == template_id))
    tpl = result.scalars().first()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    
    # Если изображение хранится в базе данных
    if tpl.preview_image_data:
        content_type = tpl.preview_image_content_type or 'image/jpeg'
        
        # Создаем поток из байтов
        image_stream = BytesIO(tpl.preview_image_data)
        
        # Возвращаем изображение с кэшированием на 1 год
        return StreamingResponse(
            image_stream,
            media_type=content_type,
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",  # 1 год кэширования
                "ETag": f'"{template_id}"',  # Для валидации кэша
            }
        )
    
    # Если изображение по URL, редиректим (или можно вернуть 404)
    if tpl.preview_image_url:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=tpl.preview_image_url)
    
    raise HTTPException(status_code=404, detail="Preview image not found")


@router.post("", response_model=schemas.TemplateOut)
async def create_template(
    payload: schemas.TemplateCreate,
    _: None = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    import base64
    
    template_data = payload.dict()
    
    # Если есть preview_image_data (base64), декодируем и сохраняем
    preview_image_data_str = template_data.pop('preview_image_data', None)
    preview_image_content_type = template_data.pop('preview_image_content_type', None)
    
    # Создаем объект Template без preview_image_data
    tpl = Template(**template_data)
    
    # Если есть base64 данные, декодируем их в байты
    if preview_image_data_str:
        try:
            # Декодируем base64 строку в байты
            tpl.preview_image_data = base64.b64decode(preview_image_data_str)
            tpl.preview_image_content_type = preview_image_content_type or 'image/jpeg'
            logger.info(f"Decoded preview_image_data, size: {len(tpl.preview_image_data)} bytes")
        except Exception as e:
            logger.error(f"Failed to decode preview_image_data: {e}", exc_info=True)
            # Не сохраняем данные, если декодирование не удалось
    
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
    import base64
    
    result = await session.execute(select(Template).where(Template.id == template_id))
    tpl = result.scalars().first()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    
    template_data = payload.dict()
    
    # Если есть preview_image_data (base64), декодируем и сохраняем
    preview_image_data_str = template_data.pop('preview_image_data', None)
    preview_image_content_type = template_data.pop('preview_image_content_type', None)
    
    if preview_image_data_str:
        try:
            # Декодируем base64 строку в байты
            tpl.preview_image_data = base64.b64decode(preview_image_data_str)
            tpl.preview_image_content_type = preview_image_content_type or 'image/jpeg'
            logger.info(f"Decoded preview_image_data for update, size: {len(tpl.preview_image_data)} bytes")
        except Exception as e:
            logger.error(f"Failed to decode preview_image_data: {e}", exc_info=True)
            # Не обновляем данные, если декодирование не удалось
    elif preview_image_data_str is None and 'preview_image_url' in template_data:
        # Если preview_image_data не передан, но есть URL, очищаем данные из базы
        tpl.preview_image_data = None
        tpl.preview_image_content_type = None
    
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
    """Загрузить превью изображение в базу данных PostgreSQL с оптимизацией"""
    try:
        # Читаем содержимое файла
        original_content = await file.read()
        
        # Оптимизируем изображение
        optimized_content, content_type = optimize_image(original_content)
        
        logger.info(f"File uploaded and optimized: {len(original_content)} bytes -> {len(optimized_content)} bytes, content_type: {content_type}")
        
        # Возвращаем данные для сохранения в шаблоне
        import base64
        return {
            "data": base64.b64encode(optimized_content).decode('utf-8'),
            "content_type": content_type,
            "size": len(optimized_content)
        }
    except Exception as e:
        logger.error(f"Failed to upload file: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to upload file: {str(e)}")


class PreviewUrlRequest(BaseModel):
    url: str


@router.post("/upload/preview-from-url")
async def upload_preview_from_url(
    request: PreviewUrlRequest,
    _: None = Depends(require_admin),
):
    """Загрузить изображение по URL и вернуть base64 для сохранения в базу данных с оптимизацией"""
    import httpx
    import base64
    
    url = request.url
    logger.info(f"Uploading preview from URL: {url}")
    
    try:
        # Загружаем файл по URL
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            
            # Проверяем, что это изображение
            original_content_type = response.headers.get('content-type', '')
            if not original_content_type.startswith('image/'):
                raise HTTPException(status_code=400, detail="URL does not point to an image")
            
            # Оптимизируем изображение
            original_content = response.content
            optimized_content, content_type = optimize_image(original_content)
            
            # Кодируем в base64 для сохранения в базу
            image_data = base64.b64encode(optimized_content).decode('utf-8')
            
            logger.info(f"Successfully downloaded and optimized image from URL: {len(original_content)} bytes -> {len(optimized_content)} bytes")
            return {
                "data": image_data,
                "content_type": content_type,
                "size": len(optimized_content)
            }
            
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error when downloading from URL: {e.response.status_code}")
        raise HTTPException(
            status_code=400, 
            detail=f"Не удалось загрузить изображение: HTTP {e.response.status_code}"
        )
    except Exception as e:
        logger.error(f"Failed to upload from URL: {e}", exc_info=True)
        raise HTTPException(
            status_code=400, 
            detail=f"Не удалось загрузить изображение: {str(e)}"
        )

