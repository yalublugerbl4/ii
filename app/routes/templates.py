import re
from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from .. import schemas
from ..auth import get_current_user, require_admin
from ..db import get_session
from ..models import Template
from ..services.kie import upload_file_stream

router = APIRouter(prefix="/templates", tags=["templates"])


def convert_yandex_disk_url(url: str) -> str:
    """Преобразует ссылку Яндекс Диска в прямую ссылку на файл"""
    if not url or not isinstance(url, str):
        return url
    
    # Паттерн для ссылок вида https://disk.yandex.ru/i/<id>
    pattern_i = r'https?://disk\.yandex\.ru/i/([a-zA-Z0-9_-]+)'
    match_i = re.search(pattern_i, url)
    if match_i:
        file_id = match_i.group(1)
        # Преобразуем в прямую ссылку на скачивание через getfile.dokpub.com
        # Это сервис, который конвертирует публичные ссылки Яндекс Диска в прямые ссылки
        return f"https://getfile.dokpub.com/yandex/get/{file_id}"
    
    # Паттерн для ссылок вида https://disk.yandex.ru/d/<id>
    pattern_d = r'https?://disk\.yandex\.ru/d/([a-zA-Z0-9_-]+)'
    match_d = re.search(pattern_d, url)
    if match_d:
        file_id = match_d.group(1)
        return f"https://getfile.dokpub.com/yandex/get/{file_id}"
    
    # Если это уже прямая ссылка или другой формат, возвращаем как есть
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

