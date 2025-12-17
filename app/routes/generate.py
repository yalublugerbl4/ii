from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from .. import schemas
from ..auth import get_current_user
from ..db import get_session
from ..models import Generation, Template, User
from ..services.kie import (
    KieError,
    build_payload_for_model,
    create_gpt4o_task,
    create_task,
    extract_result_url,
    poll_task,
    upload_file_stream,
)
from ..settings import settings

router = APIRouter(prefix="/generate", tags=["generate"])

# Стоимость генерации по моделям (в монетах)
MODEL_PRICES = {
    "google/nano-banana-edit": 5.0,
    "google/nano-banana": 5.0,
    "google/pro-image-to-image": 10.0,
    "flux2/pro-image-to-image": 15.0,
    "flux2/pro-text-to-image": 15.0,
    "flux2/flex-image-to-image": 12.0,
    "flux2/flex-text-to-image": 12.0,
    "seedream/4.5-text-to-image": 10.0,
    "seedream/4.5-edit": 10.0,
    "gpt4o-image": 12.0,
}


def get_generation_price(model: str) -> float:
    """Возвращает стоимость генерации для модели"""
    return MODEL_PRICES.get(model, 10.0)


@router.get("/models", response_model=list[schemas.ModelInfo])
async def list_models():
    models = [
        schemas.ModelInfo(
            id="google/nano-banana-edit",
            title="NanoBanana",
            description="Быстрая модель для редактирования и создания изображений",
            supports_output_format=True,
        ),
        schemas.ModelInfo(
            id="google/pro-image-to-image",
            title="🔥 NanoBanana PRO",
            description="Новая улучшенная модель с более качественным пониманием запроса",
            supports_resolution=True,
            supports_output_format=True,
            default_output_format="png",
        ),
        schemas.ModelInfo(
            id="seedream/4.5-text-to-image",
            title="Seedream 4.5",
            description="Новейшая модель Seedream 4.5",
            supports_output_format=True,
        ),
        schemas.ModelInfo(
            id="gpt4o-image",
            title="GPT-4o",
            description="Новейшая модель от OpenAI для генерации изображений",
            supports_output_format=True,
        ),
        schemas.ModelInfo(
            id="flux2/pro-text-to-image",
            title="Flux 2 Pro",
            description="Мощная модель Flux 2 Pro для генерации из текста",
            supports_output_format=True,
        ),
        schemas.ModelInfo(
            id="flux2/flex-text-to-image",
            title="Flux 2 Flex",
            description="Гибкая модель Flux 2 Flex для генерации из текста",
            supports_output_format=True,
        ),
    ]
    return models


@router.post("/image")
async def generate_image(
    prompt: str,
    model: str,
    aspect_ratio: Optional[str] = "auto",
    resolution: Optional[str] = None,
    output_format: str = "png",
    template_id: Optional[str] = None,
    files: List[UploadFile] = File(default_factory=list),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    template = None
    if template_id:
        result = await session.execute(select(Template).where(Template.id == template_id))
        template = result.scalars().first()
        if not template:
            raise HTTPException(status_code=404, detail="Template not found")
        if template.default_prompt and not prompt:
            prompt = template.default_prompt
    # Проверка баланса
    price = get_generation_price(model)
    result = await session.execute(select(User).where(User.tgid == user.tgid))
    db_user = result.scalars().first()
    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")
    if float(db_user.balance) < price:
        raise HTTPException(
            status_code=402, detail=f"Insufficient balance. Required: {price}, available: {float(db_user.balance)}"
        )
    
    image_urls: list[str] = []
    for file in files:
        url = await upload_file_stream(file)
        image_urls.append(url)
    try:
        payload, is_gpt4o = await build_payload_for_model(
            model=model,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            output_format=output_format,
            image_urls=image_urls,
        )
        if settings.kie_callback_url:
            # callBackUrl добавляется на верхний уровень payload, не в input
            payload["callBackUrl"] = settings.kie_callback_url
        
        if is_gpt4o:
            task_id = await create_gpt4o_task(payload)
        else:
            task_id = await create_task(payload)
    except KieError as exc:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"KIE error: {exc}")
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Unexpected error: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal error: {str(exc)}")
    
    # Списание баланса после успешного создания задачи
    db_user.balance = float(db_user.balance) - price
    gen = Generation(
        tgid=user.tgid,
        template_id=template.id if template else None,
        model=model,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        output_format=output_format,
        prompt=prompt,
        status="queued",
        kie_task_id=task_id,
    )
    session.add(gen)
    await session.commit()
    await session.refresh(db_user)
    await session.refresh(gen)
    return {"generation_id": str(gen.id), "task_id": task_id, "status": gen.status}


@router.post("/poll/{generation_id}", response_model=schemas.GenerationOut)
async def poll_generation(
    generation_id: str,
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(Generation).where(Generation.id == generation_id, Generation.tgid == user.tgid)
    )
    gen = result.scalars().first()
    if not gen:
        raise HTTPException(status_code=404, detail="Generation not found")
    if not gen.kie_task_id:
        raise HTTPException(status_code=400, detail="No task id")
    
    is_gpt4o = gen.model == "gpt4o-image"
    data = await poll_task(gen.kie_task_id, is_gpt4o=is_gpt4o)
    
    status = (data.get("data") or {}).get("status") or data.get("status")
    if status:
        gen.status = str(status).lower()
    url = extract_result_url(data)
    if url:
        gen.result_url = url
        gen.status = "done"
    await session.commit()
    await session.refresh(gen)
    return gen

