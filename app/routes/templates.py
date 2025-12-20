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
    template_data = payload.dict()
    
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
    """Загрузить превью изображение в папку public/uploads"""
    import uuid
    from pathlib import Path
    
    try:
        # Создаем папку public/uploads если её нет
        upload_dir = Path("public/uploads")
        upload_dir.mkdir(parents=True, exist_ok=True)
        
        # Генерируем уникальное имя файла
        file_ext = file.filename.split('.')[-1] if '.' in file.filename else 'jpg'
        file_name = f"{uuid.uuid4()}.{file_ext}"
        file_path = upload_dir / file_name
        
        # Сохраняем файл
        content = await file.read()
        with open(file_path, "wb") as f:
            f.write(content)
        
        # Возвращаем URL для доступа через бэкенд
        # Используем относительный путь, бэкенд будет раздавать статику
        url = f"/uploads/{file_name}"
        logger.info(f"File uploaded to public/uploads: {file_name}, URL: {url}")
        return {"url": url}
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
    """Загрузить изображение по URL и сохранить в папку public/uploads"""
    import os
    import uuid
    import httpx
    from pathlib import Path
    
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
        # Загружаем файл по URL
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            
            # Проверяем, что это изображение
            content_type = response.headers.get('content-type', '')
            if not content_type.startswith('image/'):
                raise HTTPException(status_code=400, detail="URL does not point to an image")
            
            # Определяем расширение
            ext = 'jpg'
            if 'png' in content_type:
                ext = 'png'
            elif 'gif' in content_type:
                ext = 'gif'
            elif 'webp' in content_type:
                ext = 'webp'
            
            # Создаем папку public/uploads если её нет
            upload_dir = Path("public/uploads")
            upload_dir.mkdir(parents=True, exist_ok=True)
            
            # Генерируем уникальное имя файла
            file_name = f"{uuid.uuid4()}.{ext}"
            file_path = upload_dir / file_name
            
            # Сохраняем файл
            with open(file_path, "wb") as f:
                f.write(response.content)
            
            # Возвращаем URL для доступа через бэкенд
            # Используем относительный путь, бэкенд будет раздавать статику
            final_url = f"/uploads/{file_name}"
            logger.info(f"Successfully uploaded to public/uploads: {file_name}, URL: {final_url}")
            return {"url": final_url}
            
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

