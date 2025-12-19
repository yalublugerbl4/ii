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
    create_veo_task,
    extract_result_url,
    extract_veo_result_url,
    poll_task,
    upload_file_stream,
)
from ..settings import settings

router = APIRouter(prefix="/generate", tags=["generate"])

# Стоимость генерации по моделям (в кредитах)
MODEL_PRICES = {
    "google/nano-banana-edit": 5.0,
    "google/nano-banana": 5.0,  # Используется когда нет фото для edit модели
    "nano-banana-pro": 10.0,
    "seedream/4.5-text-to-image": 10.0,
    "recraft/remove-background": 5.0,
    "recraft/crisp-upscale": 5.0,
}

# Минимальный баланс для генерации по моделям (в кредитах)
MIN_BALANCE_REQUIRED = {
    "veo3": 280.0,
    "veo3_fast": 70.0,
    "grok-imagine/text-to-video": 30.0,
    "seedream/4.5-text-to-image": 10.0,
    "google/nano-banana-edit": 5.0,
    "google/nano-banana": 5.0,
    "nano-banana-pro": 20.0,
    "recraft/remove-background": 5.0,
}


def get_generation_price(model: str) -> float:
    """Возвращает стоимость генерации для модели"""
    return MODEL_PRICES.get(model, 10.0)


def get_min_balance_required(model: str) -> float:
    """Возвращает минимальный баланс, необходимый для генерации"""
    return MIN_BALANCE_REQUIRED.get(model, 10.0)


@router.post("/upload-file")
async def upload_file(
    file: UploadFile = File(...),
    user=Depends(get_current_user),
):
    """Загрузить файл и получить URL"""
    import logging
    logger = logging.getLogger(__name__)
    
    logger.info(f"upload_file called: filename={file.filename}, content_type={file.content_type}")
    
    try:
        url = await upload_file_stream(file)
        logger.info(f"File uploaded successfully: {url}")
        return {"url": url, "filename": file.filename}
    except Exception as e:
        logger.error(f"Failed to upload file: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"Failed to upload file: {str(e)}")


@router.get("/models", response_model=list[schemas.ModelInfo])
async def list_models():
    # Оставляем только банану обычную, про и сидрим 4.5
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
            id="seedream/4.5-text-to-image",
            title="Seedream 4.5",
            description="Новейшая модель Seedream 4.5",
            supports_output_format=True,
        ),
    ]
    return models


@router.get("/video-models", response_model=list[schemas.ModelInfo])
async def list_video_models():
    # Модели для генерации видео
    models = [
        schemas.ModelInfo(
            id="grok-imagine/text-to-video",
            title="Grok Imagine",
            description="Генерация видео",
            modes=["video"],
            supports_output_format=False,
        ),
        schemas.ModelInfo(
            id="veo3",
            title="Veo 3.1 Quality",
            description="Высококачественная модель Veo 3.1",
            modes=["video"],
            supports_output_format=False,
        ),
        schemas.ModelInfo(
            id="veo3_fast",
            title="Veo 3.1 Fast",
            description="Быстрая модель Veo 3.1",
            modes=["video"],
            supports_output_format=False,
        ),
    ]
    return models


@router.post("/video")
async def generate_video(
    request: Request,
    prompt: str = Form(...),
    model: str = Form(...),  # grok-imagine/text-to-video, veo3, veo3_fast
    aspect_ratio: Optional[str] = Form(None),  # Для Grok: 2:3, 3:2, 1:1. Для Veo: 16:9, 9:16, Auto
    mode: Optional[str] = Form(None),  # Для Grok: normal, fun, spicy. Для Veo: generation_type
    files: Optional[List[UploadFile]] = File(None),
    generation_type: Optional[str] = Form(None),  # Для Veo: TEXT_2_VIDEO, FIRST_AND_LAST_FRAMES_2_VIDEO, REFERENCE_2_VIDEO
    seeds: Optional[int] = Form(None),  # Для Veo: 10000-99999
    enable_translation: Optional[bool] = Form(True),  # Для Veo
    watermark: Optional[str] = Form(None),  # Для Veo
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    import logging
    logger = logging.getLogger(__name__)
    
    # Получаем image_urls из form напрямую
    form = await request.form()
    image_urls_list = form.getlist("image_urls")
    
    # Нормализуем files - если None, делаем пустой список
    files_list = files if files else []
    
    logger.info(f"generate_video called: model={model}, prompt_length={len(prompt)}, files_count={len(files_list)}, image_urls_count={len(image_urls_list)}")
    
    # Получаем пользователя из БД
    result = await session.execute(select(User).where(User.tgid == user.tgid))
    db_user = result.scalars().first()
    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Проверяем баланс перед генерацией
    min_balance = get_min_balance_required(model)
    user_balance = float(db_user.balance) if db_user.balance else 0.0
    if user_balance < min_balance:
        raise HTTPException(
            status_code=402,
            detail={
                "message": f"Недостаточно средств. Требуется {min_balance} кредитов для модели {model}",
                "required_balance": min_balance,
                "current_balance": user_balance,
                "model": model,
            }
        )
    
    # Используем переданные image_urls или загружаем файлы
    final_image_urls: list[str] = []
    
    if image_urls_list:
        final_image_urls = list(image_urls_list)
        logger.info(f"Using provided image_urls: {len(final_image_urls)} URLs")
    elif files_list:
        for idx, file in enumerate(files_list):
            logger.info(f"Uploading file {idx}: {file.filename}")
            try:
                url = await upload_file_stream(file)
                final_image_urls.append(url)
                logger.info(f"File {idx} uploaded successfully: {url}")
            except Exception as e:
                logger.error(f"Failed to upload file {idx}: {e}", exc_info=True)
                raise HTTPException(status_code=400, detail=f"Failed to upload file: {str(e)}")
    
    logger.info(f"Total image URLs: {len(final_image_urls)}")
    
    # Определяем тип модели
    is_veo = model in ("veo3", "veo3_fast")
    is_grok = model == "grok-imagine/text-to-video"
    
    # Проверяем, есть ли вебхуки n8n
    n8n_webhooks = None
    if settings.n8n_webhook_urls:
        n8n_webhooks = [url.strip() for url in settings.n8n_webhook_urls.split(",") if url.strip()]
        logger.info(f"Found {len(n8n_webhooks)} n8n webhook(s)")
    
    if n8n_webhooks:
        # Отправляем на вебхуки n8n
        logger.info("Sending video data to n8n webhooks instead of KIE")
        
        webhook_data = {
            "prompt": prompt,
            "model": model,
            "image_urls": final_image_urls,
            "user_tgid": user.tgid,
            "user_id": str(user.id) if user.id else None,
            "template_id": None,
        }
        
        if is_veo:
            # Параметры для Veo 3.1
            if aspect_ratio:
                webhook_data["aspect_ratio"] = aspect_ratio
            if generation_type:
                webhook_data["generation_type"] = generation_type
            if seeds is not None:
                webhook_data["seeds"] = seeds
            if enable_translation is not None:
                webhook_data["enable_translation"] = enable_translation
            if watermark:
                webhook_data["watermark"] = watermark
        else:
            # Параметры для Grok Imagine
            webhook_data["mode"] = mode or "normal"
            # aspect_ratio передаем только если нет фото (text-to-video)
            if aspect_ratio and len(final_image_urls) == 0:
                webhook_data["aspect_ratio"] = aspect_ratio
        
        webhook_errors = []
        for webhook_url in n8n_webhooks:
            try:
                logger.info(f"Sending to n8n webhook: {webhook_url}")
                async with httpx.AsyncClient(timeout=30) as client:
                    response = await client.post(webhook_url, json=webhook_data)
                    response.raise_for_status()
                    logger.info(f"Successfully sent to webhook: {webhook_url}, status: {response.status_code}")
            except Exception as e:
                logger.error(f"Failed to send to webhook {webhook_url}: {e}", exc_info=True)
                webhook_errors.append(f"{webhook_url}: {str(e)}")
        
        if webhook_errors and len(webhook_errors) == len(n8n_webhooks):
            raise HTTPException(
                status_code=500,
                detail=f"Failed to send to all webhooks: {'; '.join(webhook_errors)}"
            )
        
        # Создаем запись в БД
        gen = Generation(
            tgid=user.tgid,
            template_id=None,
            model=model,
            aspect_ratio=aspect_ratio,
            resolution=None,
            output_format="mp4",
            prompt=prompt,
            status="sent_to_n8n",
            kie_task_id=None,
        )
        session.add(gen)
        await session.commit()
        await session.refresh(gen)
        
        logger.info(f"Generation {gen.id} sent to n8n webhooks successfully")
        return {"generation_id": str(gen.id), "status": "sent_to_n8n", "message": "Data sent to n8n"}
    
    # Старая логика через KIE (если вебхуки не настроены)
    try:
        logger.info(f"Building payload for video model: {model}, prompt length: {len(prompt)}, image_urls count: {len(final_image_urls)}")
        # aspect_ratio передаем только если нет фото (text-to-video)
        video_aspect_ratio = aspect_ratio if len(final_image_urls) == 0 else None
        payload, is_gpt4o = await build_payload_for_model(
            model=model,
            prompt=prompt,
            aspect_ratio=video_aspect_ratio,
            resolution=None,
            output_format="mp4",
            quality=None,
            mode=mode,
            image_urls=final_image_urls,
        )
        logger.info(f"Payload built, is_gpt4o: {is_gpt4o}")
        
        if settings.kie_callback_url:
            payload["callBackUrl"] = settings.kie_callback_url
            logger.info(f"Added callback URL: {settings.kie_callback_url}")
        
        logger.info(f"Creating video task, model: {model}")
        task_id = await create_task(payload)
        logger.info(f"Task created successfully: {task_id}")
    except KieError as exc:
        logger.error(f"KIE error: {exc}", exc_info=True)
        error_str = str(exc)
        if "422" in error_str or "code 422" in error_str.lower() or "validation" in error_str.lower():
            raise HTTPException(status_code=422, detail=error_str)
        raise HTTPException(status_code=400, detail=error_str)
    except Exception as exc:
        logger.error(f"Unexpected error: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal error: {str(exc)}")
    
    # Баланс не списываем - это делается в n8n
    gen = Generation(
        tgid=user.tgid,
        template_id=None,
        model=model,
        aspect_ratio=aspect_ratio,
        resolution=None,
        output_format="mp4",
        prompt=prompt,
        status="queued",
        kie_task_id=task_id,
    )
    session.add(gen)
    await session.commit()
    await session.refresh(gen)
    return {"generation_id": str(gen.id), "task_id": task_id, "status": gen.status}


@router.post("/image")
async def generate_image(
    request: Request,
    prompt: str = Form(...),
    model: str = Form(...),
    aspect_ratio: Optional[str] = Form("auto"),
    resolution: Optional[str] = Form(None),
    output_format: str = Form("png"),
    quality: Optional[str] = Form(None),  # Для Seedream 4.5: basic или high
    template_id: Optional[str] = Form(None),
    files: Optional[List[UploadFile]] = File(None),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    import logging
    logger = logging.getLogger(__name__)
    
    # Получаем image_urls из form напрямую (для списка строк нужно использовать getlist)
    form = await request.form()
    image_urls_list = form.getlist("image_urls")
    
    # Нормализуем files - если None, делаем пустой список
    files_list = files if files else []
    
    logger.info(f"generate_image called: model={model}, prompt_length={len(prompt)}, files_count={len(files_list)}, image_urls_count={len(image_urls_list)}")
    
    template = None
    if template_id:
        result = await session.execute(select(Template).where(Template.id == template_id))
        template = result.scalars().first()
        if not template:
            raise HTTPException(status_code=404, detail="Template not found")
        if template.default_prompt and not prompt:
            prompt = template.default_prompt
    
    # Получаем пользователя из БД
    result = await session.execute(select(User).where(User.tgid == user.tgid))
    db_user = result.scalars().first()
    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Проверяем баланс перед генерацией
    min_balance = get_min_balance_required(model)
    user_balance = float(db_user.balance) if db_user.balance else 0.0
    if user_balance < min_balance:
        raise HTTPException(
            status_code=402,
            detail={
                "message": f"Недостаточно средств. Требуется {min_balance} кредитов для модели {model}",
                "required_balance": min_balance,
                "current_balance": user_balance,
                "model": model,
            }
        )
    
    # Используем переданные image_urls или загружаем файлы
    final_image_urls: list[str] = []
    
    if image_urls_list:
        # Используем уже загруженные URL
        final_image_urls = list(image_urls_list)
        logger.info(f"Using provided image_urls: {len(final_image_urls)} URLs")
    elif files_list:
        # Загружаем файлы
        for idx, file in enumerate(files_list):
            logger.info(f"Uploading file {idx}: {file.filename}")
            try:
                url = await upload_file_stream(file)
                final_image_urls.append(url)
                logger.info(f"File {idx} uploaded successfully: {url}")
            except Exception as e:
                logger.error(f"Failed to upload file {idx}: {e}", exc_info=True)
                raise HTTPException(status_code=400, detail=f"Failed to upload file: {str(e)}")
    
    logger.info(f"Total image URLs: {len(final_image_urls)}")
    
    # Проверяем, есть ли вебхуки n8n
    n8n_webhooks = None
    if settings.n8n_webhook_urls:
        # Разделяем по запятой, если несколько вебхуков
        n8n_webhooks = [url.strip() for url in settings.n8n_webhook_urls.split(",") if url.strip()]
        logger.info(f"Found {len(n8n_webhooks)} n8n webhook(s)")
    
    if n8n_webhooks:
        # Отправляем на вебхуки n8n вместо обработки через KIE
        logger.info("Sending data to n8n webhooks instead of KIE")
        
        # Подготавливаем данные для отправки на вебхук
        # Конвертируем UUID в строки для JSON сериализации
        webhook_data = {
            "prompt": prompt,
            "model": model,
            "aspect_ratio": aspect_ratio,
            "resolution": resolution or None,  # Убеждаемся что None, а не пустая строка
            "output_format": output_format,
            "quality": quality or None,  # Для Seedream 4.5: basic или high
            "image_urls": final_image_urls,
            "user_tgid": user.tgid,
            "user_id": str(user.id) if user.id else None,  # Конвертируем UUID в строку
            "template_id": str(template_id) if template_id else None,  # Конвертируем UUID в строку
        }
        
        # Отправляем на все указанные вебхуки
        webhook_errors = []
        for webhook_url in n8n_webhooks:
            try:
                logger.info(f"Sending to n8n webhook: {webhook_url}")
                async with httpx.AsyncClient(timeout=30) as client:
                    response = await client.post(webhook_url, json=webhook_data)
                    response.raise_for_status()
                    logger.info(f"Successfully sent to webhook: {webhook_url}, status: {response.status_code}")
            except Exception as e:
                logger.error(f"Failed to send to webhook {webhook_url}: {e}", exc_info=True)
                webhook_errors.append(f"{webhook_url}: {str(e)}")
        
        if webhook_errors and len(webhook_errors) == len(n8n_webhooks):
            # Все вебхуки вернули ошибку
            raise HTTPException(
                status_code=500,
                detail=f"Failed to send to all webhooks: {'; '.join(webhook_errors)}"
            )
        
        # Создаем запись в БД со статусом "sent_to_n8n" (или "queued")
        gen = Generation(
            tgid=user.tgid,
            template_id=template.id if template else None,
            model=model,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            output_format=output_format,
            prompt=prompt,
            status="sent_to_n8n",  # Новый статус для отправки в n8n
            kie_task_id=None,  # Нет задачи в KIE
        )
        session.add(gen)
        await session.commit()
        await session.refresh(gen)
        
        logger.info(f"Generation {gen.id} sent to n8n webhooks successfully")
        return {"generation_id": str(gen.id), "status": "sent_to_n8n", "message": "Data sent to n8n"}
    
    # Старая логика через KIE (если вебхуки не настроены)
    try:
        logger.info(f"Building payload for model: {model}, prompt length: {len(prompt)}, image_urls count: {len(final_image_urls)}")
        payload, is_gpt4o = await build_payload_for_model(
            model=model,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            output_format=output_format,
            quality=quality,
            mode=None,  # Для изображений mode не используется
            image_urls=final_image_urls,
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
    
    # Баланс не списываем - это делается в n8n
    gen = Generation(
        tgid=user.tgid,
        template_id=template.id if template else None,
        model=model,
        aspect_ratio=aspect_ratio,
        resolution=None,
        output_format="mp4",
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
    
    import logging
    logger = logging.getLogger(__name__)
    
    is_veo = gen.model in ("veo3", "veo3_fast")
    is_gpt4o = gen.model == "gpt4o-image"
    logger.info(f"Polling task {gen.kie_task_id} for generation {gen.id}, is_veo={is_veo}, is_gpt4o={is_gpt4o}")
    
    data = await poll_task(gen.kie_task_id, is_gpt4o=is_gpt4o)
    logger.info(f"Poll response for task {gen.kie_task_id}: {json.dumps(data, indent=2, ensure_ascii=False)}")
    
    status = (data.get("data") or {}).get("status") or data.get("status")
    if status:
        gen.status = str(status).lower()
        logger.info(f"Status updated to: {gen.status}")
    
    # Используем extract_veo_result_url для Veo 3.1, иначе extract_result_url
    if is_veo:
        url = extract_veo_result_url(data)
        logger.info(f"Extracted Veo result URL: {url}")
    else:
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
):
    """Прокси для скачивания изображений (обход CORS) - публичный endpoint"""
    import logging
    logger = logging.getLogger(__name__)
    
    try:
        logger.info(f"Proxying image from: {url}")
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
            elif "webp" in content_type:
                ext = "webp"
            
            # Определяем имя файла из URL если возможно
            filename = "generated-image"
            if "/" in url:
                url_filename = url.split("/")[-1].split("?")[0]
                if "." in url_filename:
                    filename = url_filename.rsplit(".", 1)[0]
            
            logger.info(f"Proxying image: content_type={content_type}, ext={ext}, size={len(resp.content)} bytes")
            
            return Response(
                content=resp.content,
                media_type=content_type,
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}.{ext}"',
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Expose-Headers": "Content-Disposition",
                    "Content-Length": str(len(resp.content)),
                },
            )
    except Exception as e:
        logger.error(f"Failed to proxy image from {url}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to fetch image: {str(e)}")


@router.post("/remove-background")
async def remove_background(
    request: Request,
    files: Optional[List[UploadFile]] = File(None),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Удаление фона с изображения (бесплатно)"""
    import logging
    logger = logging.getLogger(__name__)
    
    model = "recraft/remove-background"
    
    # Удаление фона бесплатно - проверка баланса не требуется
    
    # Получаем image_urls из form напрямую
    form = await request.form()
    image_urls_list = form.getlist("image_urls")
    
    # Нормализуем files - если None, делаем пустой список
    files_list = files if files else []
    
    logger.info(f"remove_background called: files_count={len(files_list)}, image_urls_count={len(image_urls_list)}")
    
    # Используем переданные image_urls или загружаем файлы
    final_image_urls: list[str] = []
    
    if image_urls_list:
        # Используем уже загруженные URL
        final_image_urls = list(image_urls_list)
        logger.info(f"Using provided image_urls: {len(final_image_urls)} URLs")
    elif files_list:
        # Загружаем файлы
        if len(files_list) > 1:
            raise HTTPException(status_code=400, detail="Можно загрузить только одно изображение")
        
        file = files_list[0]
        try:
            image_url = await upload_file_stream(file, upload_path="images/remove-bg")
            final_image_urls.append(image_url)
            logger.info(f"File uploaded for remove-background: {image_url}")
        except Exception as e:
            logger.error(f"Failed to upload file for remove-background: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Ошибка загрузки файла: {str(e)}")
    else:
        raise HTTPException(status_code=400, detail="Необходимо загрузить хотя бы одно изображение")
    
    if len(final_image_urls) == 0:
        raise HTTPException(status_code=400, detail="Необходимо загрузить хотя бы одно изображение")
    
    if len(final_image_urls) > 1:
        raise HTTPException(status_code=400, detail="Можно загрузить только одно изображение")
    
    image_url = final_image_urls[0]
    
    # Проверяем, есть ли вебхуки n8n
    n8n_webhooks = None
    if settings.n8n_webhook_urls:
        n8n_webhooks = [url.strip() for url in settings.n8n_webhook_urls.split(",") if url.strip()]
        logger.info(f"Found {len(n8n_webhooks)} n8n webhook(s)")
    
    if n8n_webhooks:
        # Отправляем на вебхуки n8n
        logger.info("Sending remove-background data to n8n webhooks instead of KIE")
        
        webhook_data = {
            "model": model,
            "image_urls": [image_url],
            "user_tgid": user.tgid,
            "user_id": str(user.id) if user.id else None,
            "template_id": None,
        }
        
        webhook_errors = []
        for webhook_url in n8n_webhooks:
            try:
                logger.info(f"Sending to n8n webhook: {webhook_url}")
                async with httpx.AsyncClient(timeout=30) as client:
                    response = await client.post(webhook_url, json=webhook_data)
                    response.raise_for_status()
                    logger.info(f"Successfully sent to webhook: {webhook_url}, status: {response.status_code}")
            except Exception as e:
                logger.error(f"Failed to send to webhook {webhook_url}: {e}", exc_info=True)
                webhook_errors.append(f"{webhook_url}: {str(e)}")
        
        if webhook_errors and len(webhook_errors) == len(n8n_webhooks):
            raise HTTPException(
                status_code=500,
                detail=f"Failed to send to all webhooks: {'; '.join(webhook_errors)}"
            )
        
        # Создаем запись в БД
        gen = Generation(
            tgid=user.tgid,
            template_id=None,
            model=model,
            aspect_ratio=None,
            resolution=None,
            output_format="png",
            prompt="",  # Для remove-background prompt не нужен
            status="sent_to_n8n",
            kie_task_id=None,
        )
        session.add(gen)
        await session.commit()
        await session.refresh(gen)
        
        logger.info(f"Remove background generation {gen.id} sent to n8n webhooks successfully")
        return {"generation_id": str(gen.id), "status": "sent_to_n8n", "message": "Data sent to n8n"}
    
    # Старая логика через KIE (если вебхуки не настроены)
    # Создаем задачу в KIE API
    payload = {
        "model": model,
        "input": {
            "image": image_url,
        },
    }
    
    if settings.kie_callback_url:
        payload["callBackUrl"] = settings.kie_callback_url
        logger.info(f"Added callback URL: {settings.kie_callback_url}")
    
    try:
        task_id = await create_task(payload)
        logger.info(f"Remove background task created: {task_id}")
    except Exception as e:
        logger.error(f"Failed to create remove-background task: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Ошибка создания задачи: {str(e)}")
    
    # Создаем запись в БД
    gen = Generation(
        tgid=user.tgid,
        template_id=None,
        model=model,
        aspect_ratio=None,
        resolution=None,
        output_format="png",
        prompt="",  # Для remove-background prompt не нужен
        status="queued",
        kie_task_id=task_id,
    )
    session.add(gen)
    await session.commit()
    await session.refresh(gen)
    
    logger.info(f"Remove background generation {gen.id} created with task {task_id}")
    return {"generation_id": str(gen.id), "status": "queued", "task_id": task_id}


@router.post("/upscale")
async def upscale_image(
    request: Request,
    files: Optional[List[UploadFile]] = File(None),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Улучшение качества изображения (Crisp Upscale)"""
    import logging
    logger = logging.getLogger(__name__)
    
    model = "recraft/crisp-upscale"
    
    # Получаем image_urls из form напрямую
    form = await request.form()
    image_urls_list = form.getlist("image_urls")
    
    # Нормализуем files - если None, делаем пустой список
    files_list = files if files else []
    
    logger.info(f"upscale_image called: files_count={len(files_list)}, image_urls_count={len(image_urls_list)}")
    
    # Используем переданные image_urls или загружаем файлы
    final_image_urls: list[str] = []
    
    if image_urls_list:
        # Используем уже загруженные URL
        final_image_urls = list(image_urls_list)
        logger.info(f"Using provided image_urls: {len(final_image_urls)} URLs")
    elif files_list:
        # Загружаем файлы
        if len(files_list) > 1:
            raise HTTPException(status_code=400, detail="Можно загрузить только одно изображение")
        
        file = files_list[0]
        try:
            image_url = await upload_file_stream(file, upload_path="images/upscale")
            final_image_urls.append(image_url)
            logger.info(f"File uploaded for upscale: {image_url}")
        except Exception as e:
            logger.error(f"Failed to upload file for upscale: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Ошибка загрузки файла: {str(e)}")
    else:
        raise HTTPException(status_code=400, detail="Необходимо загрузить хотя бы одно изображение")
    
    if len(final_image_urls) == 0:
        raise HTTPException(status_code=400, detail="Необходимо загрузить хотя бы одно изображение")
    
    if len(final_image_urls) > 1:
        raise HTTPException(status_code=400, detail="Можно загрузить только одно изображение")
    
    image_url = final_image_urls[0]
    
    # Проверяем, есть ли вебхуки n8n
    n8n_webhooks = None
    if settings.n8n_webhook_urls:
        n8n_webhooks = [url.strip() for url in settings.n8n_webhook_urls.split(",") if url.strip()]
        logger.info(f"Found {len(n8n_webhooks)} n8n webhook(s)")
    
    if n8n_webhooks:
        # Отправляем на вебхуки n8n
        logger.info("Sending upscale data to n8n webhooks instead of KIE")
        
        webhook_data = {
            "model": model,
            "image_urls": [image_url],
            "user_tgid": user.tgid,
            "user_id": str(user.id) if user.id else None,
            "template_id": None,
        }
        
        webhook_errors = []
        for webhook_url in n8n_webhooks:
            try:
                logger.info(f"Sending to n8n webhook: {webhook_url}")
                async with httpx.AsyncClient(timeout=30) as client:
                    response = await client.post(webhook_url, json=webhook_data)
                    response.raise_for_status()
                    logger.info(f"Successfully sent to webhook: {webhook_url}, status: {response.status_code}")
            except Exception as e:
                logger.error(f"Failed to send to webhook {webhook_url}: {e}", exc_info=True)
                webhook_errors.append(f"{webhook_url}: {str(e)}")
        
        if webhook_errors and len(webhook_errors) == len(n8n_webhooks):
            raise HTTPException(
                status_code=500,
                detail=f"Failed to send to all webhooks: {'; '.join(webhook_errors)}"
            )
        
        # Создаем запись в БД
        gen = Generation(
            tgid=user.tgid,
            template_id=None,
            model=model,
            aspect_ratio=None,
            resolution=None,
            output_format="png",
            prompt="",  # Для upscale prompt не нужен
            status="sent_to_n8n",
            kie_task_id=None,
        )
        session.add(gen)
        await session.commit()
        await session.refresh(gen)
        
        logger.info(f"Upscale generation {gen.id} sent to n8n webhooks successfully")
        return {"generation_id": str(gen.id), "status": "sent_to_n8n", "message": "Data sent to n8n"}
    
    # Старая логика через KIE (если вебхуки не настроены)
    # Создаем задачу в KIE API
    payload = {
        "model": model,
        "input": {
            "image": image_url,
        },
    }
    
    if settings.kie_callback_url:
        payload["callBackUrl"] = settings.kie_callback_url
        logger.info(f"Added callback URL: {settings.kie_callback_url}")
    
    try:
        task_id = await create_task(payload)
        logger.info(f"Upscale task created: {task_id}")
    except Exception as e:
        logger.error(f"Failed to create upscale task: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Ошибка создания задачи: {str(e)}")
    
    # Создаем запись в БД
    gen = Generation(
        tgid=user.tgid,
        template_id=None,
        model=model,
        aspect_ratio=None,
        resolution=None,
        output_format="png",
        prompt="",  # Для upscale prompt не нужен
        status="queued",
        kie_task_id=task_id,
    )
    session.add(gen)
    await session.commit()
    await session.refresh(gen)
    
    logger.info(f"Upscale generation {gen.id} created with task {task_id}")
    return {"generation_id": str(gen.id), "status": "queued", "task_id": task_id}



