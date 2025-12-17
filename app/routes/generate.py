import json
from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
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
    "nano-banana-pro": 10.0,  # Как в bot.txt
    "google/pro-image-to-image": 10.0,  # Алиас для обратной совместимости
    "flux2/pro-image-to-image": 15.0,
    "flux2/pro-text-to-image": 15.0,
    "flux2/flex-image-to-image": 12.0,
    "flux2/flex-text-to-image": 12.0,
    "bytedance/seedream-v4-text-to-image": 10.0,  # Как в bot.txt
    "bytedance/seedream-v4-edit": 10.0,  # Как в bot.txt
    "seedream/4.5-text-to-image": 10.0,
    "seedream/4.5-edit": 10.0,
    "gpt4o-image": 12.0,
}


def get_generation_price(model: str) -> float:
    """Возвращает стоимость генерации для модели"""
    return MODEL_PRICES.get(model, 10.0)


@router.get("/models", response_model=list[schemas.ModelInfo])
async def list_models():
    # Модели точно как в bot.txt и документации
    models = [
        schemas.ModelInfo(
            id="google/nano-banana-edit",
            title="NanoBanana",
            description="Быстрая модель для редактирования и создания изображений",
            supports_output_format=True,
        ),
        schemas.ModelInfo(
            id="nano-banana-pro",
            title="🔥 NanoBanana PRO",
            description="Новая улучшенная модель с более качественным пониманием запроса",
            supports_resolution=True,
            supports_output_format=True,
            default_output_format="png",
        ),
        schemas.ModelInfo(
            id="bytedance/seedream-v4-text-to-image",
            title="Seedream 4.0",
            description="Высококачественная генерация изображений",
            supports_output_format=True,
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
    request: Request,
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    import logging
    logger = logging.getLogger(__name__)
    
    # Получаем все данные из form напрямую
    form = await request.form()
    logger.info(f"Form keys from request.form(): {list(form.keys())}")
    
    # Извлекаем текстовые поля
    prompt = form.get("prompt", "")
    model = form.get("model", "google/nano-banana-edit")
    aspect_ratio = form.get("aspect_ratio", "auto")
    resolution = form.get("resolution") or None
    output_format = form.get("output_format", "png")
    template_id = form.get("template_id") or None
    
    # Получаем файлы из form
    files_list: List[UploadFile] = []
    files_values = form.getlist("files")
    logger.info(f"Files from form.getlist('files'): {len(files_values)} values")
    
    for idx, value in enumerate(files_values):
        logger.info(f"Value {idx} type: {type(value).__name__}, value: {value}")
        if isinstance(value, UploadFile):
            files_list.append(value)
            logger.info(f"Added UploadFile {idx}: filename={value.filename}, content_type={value.content_type}")
        else:
            logger.warning(f"Value {idx} is not UploadFile: {type(value)}")
    
    logger.info(f"generate_image called: model={model}, prompt_length={len(prompt)}, files_count={len(files_list)}")
    
    # Логируем информацию о файлах
    if files_list:
        for idx, file in enumerate(files_list):
            logger.info(f"File {idx}: filename={file.filename}, content_type={file.content_type}")
    else:
        logger.warning("No files received in request!")
    
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
    if files_list:
        for idx, file in enumerate(files_list):
            logger.info(f"Uploading file {idx}: {file.filename}")
            try:
                url = await upload_file_stream(file)
                image_urls.append(url)
                logger.info(f"File {idx} uploaded successfully: {url}")
            except Exception as e:
                logger.error(f"Failed to upload file {idx}: {e}", exc_info=True)
                raise HTTPException(status_code=400, detail=f"Failed to upload file: {str(e)}")
    else:
        logger.warning("No files provided in request")
    
    logger.info(f"Total uploaded image URLs: {len(image_urls)}")
    
    import logging
    logger = logging.getLogger(__name__)
    
    try:
        logger.info(f"Building payload for model: {model}, prompt length: {len(prompt)}, image_urls count: {len(image_urls)}")
        payload, is_gpt4o = await build_payload_for_model(
            model=model,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            output_format=output_format,
            image_urls=image_urls,
        )
        logger.info(f"Payload built, is_gpt4o: {is_gpt4o}")
        
        if settings.kie_callback_url:
            # callBackUrl добавляется на верхний уровень payload, не в input
            payload["callBackUrl"] = settings.kie_callback_url
            logger.info(f"Added callback URL: {settings.kie_callback_url}")
        
        logger.info(f"Creating task, model: {model}, is_gpt4o: {is_gpt4o}")
        if is_gpt4o:
            task_id = await create_gpt4o_task(payload)
        else:
            task_id = await create_task(payload)
        logger.info(f"Task created successfully: {task_id}")
    except KieError as exc:
        logger.error(f"KIE error: {exc}", exc_info=True)
        # Если ошибка содержит код 422, возвращаем 422, иначе 400
        error_str = str(exc)
        if "422" in error_str or "code 422" in error_str.lower() or "validation" in error_str.lower():
            raise HTTPException(status_code=422, detail=error_str)
        raise HTTPException(status_code=400, detail=error_str)
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
    
    import logging
    logger = logging.getLogger(__name__)
    
    is_gpt4o = gen.model == "gpt4o-image"
    logger.info(f"Polling task {gen.kie_task_id} for generation {gen.id}, is_gpt4o={is_gpt4o}")
    data = await poll_task(gen.kie_task_id, is_gpt4o=is_gpt4o)
    logger.info(f"Poll response for task {gen.kie_task_id}: {json.dumps(data, indent=2, ensure_ascii=False)}")
    
    status = (data.get("data") or {}).get("status") or data.get("status")
    if status:
        gen.status = str(status).lower()
        logger.info(f"Status updated to: {gen.status}")
    
    url = extract_result_url(data)
    logger.info(f"Extracted result URL: {url}")
    if url:
        gen.result_url = url
        gen.status = "done"
        logger.info(f"Generation {gen.id} completed, result_url: {url}")
    else:
        logger.warning(f"No result URL found in response for task {gen.kie_task_id}")
    
    await session.commit()
    await session.refresh(gen)
    return gen


@router.get("/proxy-image")
async def proxy_image(
    url: str = Query(...),
    user=Depends(get_current_user),
):
    """Прокси для скачивания изображений (обход CORS)"""
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            # Определяем расширение из URL или content-type
            content_type = resp.headers.get("content-type", "image/png")
            ext = "png"
            if "jpeg" in content_type or "jpg" in content_type:
                ext = "jpg"
            elif "png" in content_type:
                ext = "png"
            
            return Response(
                content=resp.content,
                media_type=content_type,
                headers={
                    "Content-Disposition": f'attachment; filename="generated-image.{ext}"',
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Expose-Headers": "Content-Disposition",
                },
            )
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Failed to proxy image: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch image: {str(e)}")


@router.get("/proxy-image")
async def proxy_image(
    url: str,
    user=Depends(get_current_user),
):
    """Прокси для скачивания изображений (обход CORS)"""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            from fastapi.responses import Response
            return Response(
                content=resp.content,
                media_type=resp.headers.get("content-type", "image/png"),
                headers={
                    "Content-Disposition": f'inline; filename="image.png"',
                    "Access-Control-Allow-Origin": "*",
                },
            )
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Failed to proxy image: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch image: {str(e)}")

