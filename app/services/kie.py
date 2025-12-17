import json
import logging
from typing import Any, Dict, Iterable, List, Optional

import httpx
from fastapi import UploadFile

from ..settings import settings

logger = logging.getLogger(__name__)


class KieError(Exception):
    pass


async def upload_file_stream(file: UploadFile) -> str:
    url = f"{settings.kie_file_upload_base}/api/file-stream-upload"
    headers = {"Authorization": f"Bearer {settings.kie_api_key}"}
    files = {"file": (file.filename, await file.read())}
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(url, headers=headers, files=files)
    try:
        data = resp.json()
    except Exception:
        raise KieError(f"Upload failed: HTTP {resp.status_code}")
    if not isinstance(data, dict) or data.get("code") != 200:
        raise KieError(f"Upload failed: {data}")
    path = data.get("data", {}).get("path") or data.get("data")
    if not path:
        raise KieError(f"Upload missing path: {data}")
    return path


async def create_task(payload: Dict[str, Any]) -> str:
    url = f"{settings.kie_api_base}/api/v1/jobs/createTask"
    headers = {
        "Authorization": f"Bearer {settings.kie_api_key}",
        "Content-Type": "application/json",
    }
    logger.info(f"Creating task with payload: {json.dumps(payload, indent=2, ensure_ascii=False)}")
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(url, headers=headers, content=json.dumps(payload))
    # Обработка ответа точно как в bot.txt
    try:
        data = resp.json()
    except Exception:
        # Если не удалось распарсить JSON, пробуем получить текст
        error_text = resp.text[:1000] if hasattr(resp, 'text') else str(resp.content[:1000] if hasattr(resp, 'content') else '')
        logger.error(f"Failed to parse JSON response, status: {resp.status_code}, text: {error_text}")
        raise KieError(f"Create task failed: HTTP {resp.status_code}")
    logger.info(f"Task creation response (code={data.get('code')}): {json.dumps(data, indent=2, ensure_ascii=False)}")
    # Проверка точно как в bot.txt
    if not (
        isinstance(data, dict)
        and str(data.get("code")) == "200"
        and isinstance(data.get("data"), dict)
    ):
        error_msg = data.get("msg") or str(data)
        logger.error(f"KIE API error: code={data.get('code')}, msg={error_msg}, full_response={json.dumps(data, ensure_ascii=False)}")
        raise KieError(f"Create task failed (code {data.get('code')}): {error_msg}")
    task_id = data.get("data", {}).get("taskId")
    if not task_id:
        raise KieError(f"Create task missing taskId: {data}")
    return str(task_id)


async def create_gpt4o_task(payload: Dict[str, Any]) -> str:
    """Создать задачу для GPT-4o Image (использует другой endpoint)"""
    url = f"{settings.kie_api_base}/api/v1/gpt4o-image/generate"
    headers = {
        "Authorization": f"Bearer {settings.kie_api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(url, headers=headers, content=json.dumps(payload))
    try:
        data = resp.json()
    except Exception:
        raise KieError(f"Create GPT-4o task failed: HTTP {resp.status_code}")
    if not (isinstance(data, dict) and str(data.get("code")) == "200"):
        raise KieError(f"Create GPT-4o task failed: {data}")
    task_id = (data.get("data") or {}).get("taskId")
    if not task_id:
        raise KieError(f"Create GPT-4o task missing taskId: {data}")
    return str(task_id)


async def poll_task(task_id: str, is_gpt4o: bool = False) -> dict:
    """Проверить статус задачи"""
    if is_gpt4o:
        # GPT-4o использует другой endpoint
        url = f"{settings.kie_api_base}/api/v1/gpt4o-image/details"
        headers = {"Authorization": f"Bearer {settings.kie_api_key}"}
        params = {"taskId": task_id}
    else:
        url = f"{settings.kie_api_base}/api/v1/jobs/recordInfo"
        headers = {"Authorization": f"Bearer {settings.kie_api_key}"}
        params = {"taskId": task_id}
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(url, headers=headers, params=params)
    try:
        data = resp.json()
    except Exception:
        raise KieError(f"Poll failed: HTTP {resp.status_code}")
    if not isinstance(data, dict):
        raise KieError(f"Poll invalid response: {data}")
    return data


def extract_result_url(record: dict) -> Optional[str]:
    if not isinstance(record, dict):
        return None
    data = record.get("data") or {}
    response = data.get("response") or data
    
    # GPT-4o может возвращать результат в другом формате
    # Проверяем images массив
    images = response.get("images") or data.get("images")
    if isinstance(images, list) and len(images) > 0:
        first_image = images[0]
        if isinstance(first_image, dict):
            url = first_image.get("url") or first_image.get("imageUrl")
            if isinstance(url, str) and url.startswith("http"):
                return url
        elif isinstance(first_image, str) and first_image.startswith("http"):
            return first_image
    
    # Стандартные ключи
    for key in ("resultUrl", "url", "imageUrl", "resultImageUrl", "image_url"):
        val = response.get(key) or data.get(key)
        if isinstance(val, str) and val.startswith("http"):
            return val
    
    result_json = response.get("resultJson")
    if isinstance(result_json, str):
        try:
            parsed = json.loads(result_json)
            return extract_result_url(parsed)
        except Exception:
            return None
    if isinstance(result_json, dict):
        return extract_result_url(result_json)
    return None


async def build_payload_for_model(
    *,
    model: str,
    prompt: str,
    aspect_ratio: Optional[str],
    resolution: Optional[str],
    output_format: str,
    image_urls: Optional[Iterable[str]] = None,
) -> tuple[Dict[str, Any], bool]:
    """
    Создает payload для модели.
    Возвращает (payload, is_gpt4o) - где is_gpt4o указывает, нужно ли использовать GPT-4o endpoint
    """
    image_urls_list: List[str] = list(image_urls or [])
    is_gpt4o = model == "gpt4o-image"
    
    if is_gpt4o:
        # GPT-4o использует другой формат
        # size: 1:1, 3:2, 2:3
        size_map = {
            "1:1": "1:1",
            "3:2": "3:2",
            "2:3": "2:3",
            "auto": "1:1",  # default
        }
        size = size_map.get(aspect_ratio or "auto", "1:1")
        
        payload = {
            "prompt": prompt,
            "size": size,
            "nVariants": 1,
        }
        if image_urls_list:
            # GPT-4o поддерживает до 5 изображений
            payload["filesUrl"] = image_urls_list[:5]
        return payload, True
    
    # Остальные модели используют Market API
    # Маппинг aspect_ratio -> image_size
    size_map = {
        "9:16": "9:16",
        "16:9": "16:9",
        "1:1": "1:1",
        "3:4": "3:4",
        "4:3": "4:3",
        "2:3": "2:3",
        "3:2": "3:2",
        "5:4": "5:4",
        "4:5": "4:5",
        "21:9": "21:9",
        "auto": "auto",
    }
    image_size = size_map.get(aspect_ratio or "auto", aspect_ratio or "auto")
    
    if model == "nano-banana-pro" or model == "google/pro-image-to-image":
        # NanoBanana PRO - точно как в bot.txt
        # Использует модель "nano-banana-pro" (не "google/pro-image-to-image")
        payload_input: Dict[str, Any] = {
            "prompt": (prompt or "")[:5000],
        }
        if image_urls_list:
            # До 10 изображений, используем image_input
            payload_input["image_input"] = image_urls_list[:10]
        if aspect_ratio:
            # Для PRO используем aspect_ratio напрямую
            payload_input["aspect_ratio"] = aspect_ratio
        if resolution:
            payload_input["resolution"] = resolution
        if output_format:
            payload_input["output_format"] = output_format.lower()
        
        # В bot.txt используется KIE_NANO_PRO_MODEL = "nano-banana-pro"
        payload = {
            "model": "nano-banana-pro",
            "input": payload_input,
        }
    elif model == "google/nano-banana-edit":
        # NanoBanana Edit - точно как в bot.txt
        # В bot.txt: output_format всегда "png", image_size маппится через size_map
        # KIE API требует image_urls всегда, даже если пустой массив
        payload = {
            "model": model,
            "input": {
                "prompt": prompt[:5000],
                "output_format": "png",  # Всегда "png" как в bot.txt
                "image_size": image_size,
                "image_urls": image_urls_list[:10] if image_urls_list else [],  # Всегда передаем, даже пустой массив
            },
        }
        # mode добавляется только если есть изображения
        if image_urls_list:
            payload["input"]["mode"] = "edit"
    elif model in ["flux2/pro-image-to-image", "flux2/flex-image-to-image"]:
        # Flux 2 Image-to-Image
        payload = {
            "model": model,
            "input": {
                "prompt": prompt[:5000],
                "image_size": image_size,
                "output_format": output_format or "png",
            },
        }
        if image_urls_list:
            payload["input"]["image_urls"] = image_urls_list[:5]
    elif model in ["flux2/pro-text-to-image", "flux2/flex-text-to-image"]:
        # Flux 2 Text-to-Image
        payload = {
            "model": model,
            "input": {
                "prompt": prompt[:5000],
                "image_size": image_size,
                "output_format": output_format or "png",
            },
        }
    elif model == "bytedance/seedream-v4-text-to-image" or model == "seedream/4.5-text-to-image":
        # Seedream - точно как в bot.txt
        # В bot.txt используется bytedance/seedream-v4-text-to-image
        payload = {
            "model": "bytedance/seedream-v4-text-to-image" if "v4" in model else model,
            "input": {
                "prompt": prompt[:5000],
                "image_size": image_size,
                "output_format": output_format or "png",
            },
        }
    elif model == "bytedance/seedream-v4-edit" or model == "seedream/4.5-edit":
        # Seedream Edit - точно как в bot.txt
        # В bot.txt используется bytedance/seedream-v4-edit
        payload = {
            "model": "bytedance/seedream-v4-edit" if "v4" in model else model,
            "input": {
                "prompt": prompt[:5000],
                "image_size": image_size,
                "output_format": output_format or "png",
            },
        }
        if image_urls_list:
            payload["input"]["image_urls"] = image_urls_list[:5]
    else:
        # Fallback для неизвестных моделей
        payload = {
            "model": model,
            "input": {
                "prompt": prompt[:5000],
                "image_size": image_size,
                "output_format": output_format or "png",
            },
        }
        if image_urls_list:
            payload["input"]["image_urls"] = image_urls_list[:5]
    
    return payload, False

