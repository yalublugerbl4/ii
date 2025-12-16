from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from .. import schemas
from ..auth import get_current_user
from ..db import get_session
from ..models import Generation, Template
from ..services.kie import KieError, build_payload_for_model, create_task, extract_result_url, poll_task, upload_file_stream
from ..settings import settings

router = APIRouter(prefix="/generate", tags=["generate"])


@router.get("/models", response_model=list[schemas.ModelInfo])
async def list_models():
    models = [
        schemas.ModelInfo(
            id="nanobanana",
            title="NanoBanana",
            description="Быстрая модель для редактирования и создания изображений",
            supports_output_format=True,
        ),
        schemas.ModelInfo(
            id="nanobanana_pro",
            title="NanoBanana PRO",
            description="Улучшенная модель с более качественным пониманием запроса",
            supports_resolution=True,
            supports_output_format=True,
            default_output_format="png",
        ),
        schemas.ModelInfo(
            id="seedream4",
            title="Seedream 4.0",
            description="Высококачественная генерация изображений",
        ),
        schemas.ModelInfo(
            id="seedream4.5",
            title="Seedream 4.5",
            description="Новейшая модель Seedream 4.5",
        ),
        schemas.ModelInfo(
            id="gpt-4o",
            title="GPT-4o",
            description="Новейшая модель от OpenAI для генерации изображений",
        ),
        schemas.ModelInfo(
            id="flux2",
            title="Flux 2",
            description="Мощная модель Flux 2 с поддержкой Pro и Flex режимов",
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
    image_urls: list[str] = []
    for file in files:
        url = await upload_file_stream(file)
        image_urls.append(url)
    try:
        payload = await build_payload_for_model(
            model=model,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            output_format=output_format,
            image_urls=image_urls,
        )
        if settings.kie_callback_url:
            payload["callBackUrl"] = settings.kie_callback_url
        task_id = await create_task(payload)
    except KieError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
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
    data = await poll_task(gen.kie_task_id)
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

