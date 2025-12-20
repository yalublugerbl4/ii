import logging
from fastapi import APIRouter, Depends, HTTPException, UploadFile, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from io import BytesIO

from .. import schemas
from ..auth import get_current_user, require_admin
from ..db import get_session
from ..models import Template
from ..services.kie import upload_file_stream

router = APIRouter(prefix="/templates", tags=["templates"])
logger = logging.getLogger(__name__)


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
    """Загрузить превью изображение в базу данных PostgreSQL"""
    try:
        # Читаем содержимое файла
        content = await file.read()
        
        # Определяем content type
        content_type = file.content_type or 'image/jpeg'
        if not content_type.startswith('image/'):
            # Пытаемся определить по расширению
            ext = file.filename.split('.')[-1].lower() if '.' in file.filename else ''
            if ext == 'png':
                content_type = 'image/png'
            elif ext == 'gif':
                content_type = 'image/gif'
            elif ext == 'webp':
                content_type = 'image/webp'
            else:
                content_type = 'image/jpeg'
        
        # Сохраняем в базу данных
        # Возвращаем URL для использования в шаблоне
        # URL будет указывать на эндпоинт /templates/{id}/preview
        # Но пока что возвращаем placeholder, так как нужен template_id
        # В реальности это будет использоваться при создании/обновлении шаблона
        
        logger.info(f"File uploaded to database, size: {len(content)} bytes, content_type: {content_type}")
        
        # Возвращаем данные для сохранения в шаблоне
        import base64
        return {
            "data": base64.b64encode(content).decode('utf-8'),
            "content_type": content_type,
            "size": len(content)
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
    """Загрузить изображение по URL и вернуть base64 для сохранения в базу данных"""
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
            content_type = response.headers.get('content-type', '')
            if not content_type.startswith('image/'):
                raise HTTPException(status_code=400, detail="URL does not point to an image")
            
            # Кодируем в base64 для сохранения в базу
            image_data = base64.b64encode(response.content).decode('utf-8')
            
            logger.info(f"Successfully downloaded image from URL, size: {len(response.content)} bytes")
            return {
                "data": image_data,
                "content_type": content_type,
                "size": len(response.content)
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

