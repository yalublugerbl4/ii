from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from .. import schemas
from ..auth import get_current_user, require_admin
from ..db import get_session
from ..models import Template
from ..services.kie import upload_file_stream

router = APIRouter(prefix="/templates", tags=["templates"])


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
    tpl = Template(**payload.dict())
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
    for k, v in payload.dict().items():
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

