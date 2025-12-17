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
    logger.info(f"File uploaded successfully, path/URL: {path}")
    return path


async def create_task(payload: Dict[str, Any]) -> str:
    url = f"{settings.kie_api_base}/api/v1/jobs/createTask"
    headers = {
        "Authorization": f"Bearer {settings.kie_api_key}",
        "Content-Type": "application/json",
    }
    # Логируем payload с особым вниманием к image_urls
    payload_str = json.dumps(payload, indent=2, ensure_ascii=False)
    logger.info(f"Creating task with payload: {payload_str}")
    
    # Проверяем наличие image_urls в payload
    input_data = payload.get("input", {})
    image_urls = input_data.get("image_urls")
    logger.info(f"Payload check - model: {payload.get('model')}, image_urls type: {type(image_urls)}, image_urls value: {image_urls}")
    
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
    """Извлекает URL результата из ответа KIE API"""
    if not isinstance(record, dict):
        logger.warning("extract_result_url: record is not a dict")
        return None
    
    logger.info(f"extract_result_url: searching in record keys: {list(record.keys())}")
    
    data = record.get("data") or {}
    response = data.get("response") or data
    
    logger.info(f"extract_result_url: data keys: {list(data.keys()) if isinstance(data, dict) else 'not a dict'}")
    logger.info(f"extract_result_url: response keys: {list(response.keys()) if isinstance(response, dict) else 'not a dict'}")
    
    # GPT-4o может возвращать результат в другом формате
    # Проверяем images массив
    images = response.get("images") or data.get("images")
    if isinstance(images, list) and len(images) > 0:
        logger.info(f"extract_result_url: found images array with {len(images)} items")
        first_image = images[0]
        if isinstance(first_image, dict):
            url = first_image.get("url") or first_image.get("imageUrl")
            if isinstance(url, str) and url.startswith("http"):
                logger.info(f"extract_result_url: found URL in images[0]: {url}")
                return url
        elif isinstance(first_image, str) and first_image.startswith("http"):
            logger.info(f"extract_result_url: found URL as string in images[0]: {first_image}")
            return first_image
    
    # Стандартные ключи
    for key in ("resultUrl", "url", "imageUrl", "resultImageUrl", "image_url", "result_url"):
        val = response.get(key) or data.get(key) or record.get(key)
        if isinstance(val, str) and val.startswith("http"):
            logger.info(f"extract_result_url: found URL in key '{key}': {val}")
            return val
    
    # Проверяем resultUrls (множественное число) - массив URL
    result_urls = response.get("resultUrls") or data.get("resultUrls") or record.get("resultUrls")
    logger.info(f"extract_result_url: checking resultUrls, type: {type(result_urls)}, value: {result_urls}")
    if isinstance(result_urls, list) and len(result_urls) > 0:
        first_url = result_urls[0]
        logger.info(f"extract_result_url: first_url from resultUrls: {first_url}, type: {type(first_url)}")
        if isinstance(first_url, str) and first_url.startswith("http"):
            logger.info(f"extract_result_url: found URL in resultUrls array: {first_url}")
            return first_url
        else:
            logger.warning(f"extract_result_url: first_url is not a valid HTTP URL: {first_url}")
    elif result_urls is not None:
        logger.warning(f"extract_result_url: resultUrls is not a list: {type(result_urls)}")
    
    # Проверяем вложенные структуры
    result_json = response.get("resultJson") or data.get("resultJson")
    if isinstance(result_json, str) and result_json.strip():
        try:
            parsed = json.loads(result_json)
            logger.info(f"extract_result_url: parsing resultJson string")
            return extract_result_url(parsed)
        except Exception as e:
            logger.warning(f"extract_result_url: failed to parse resultJson: {e}, resultJson value: {result_json[:100] if result_json else 'empty'}")
            return None
    elif isinstance(result_json, str) and not result_json.strip():
        logger.info(f"extract_result_url: resultJson is empty string, skipping")
    if isinstance(result_json, dict):
        logger.info(f"extract_result_url: recursing into resultJson dict")
        return extract_result_url(result_json)
    
    logger.warning(f"extract_result_url: no URL found in response")
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
        # В bot.txt используются две модели:
        # - google/nano-banana для создания (text-to-image)
        # - google/nano-banana-edit для редактирования (image-to-image)
        # Если нет изображений, используем google/nano-banana
        if not image_urls_list:
            # Нет изображений - используем модель для создания
            actual_model = "google/nano-banana"
        else:
            # Есть изображения - используем модель для редактирования
            actual_model = "google/nano-banana-edit"
        
        payload = {
            "model": actual_model,
            "input": {
                "prompt": prompt[:5000],
                "output_format": "png",  # Всегда "png" как в bot.txt
                "image_size": image_size,
            },
        }
        # image_urls и mode добавляются только если есть изображения
        if image_urls_list:
            payload["input"]["image_urls"] = image_urls_list[:10]
            payload["input"]["mode"] = "edit"
            logger.info(f"Added image_urls to payload: {len(image_urls_list[:10])} URLs")
        else:
            logger.warning("No image_urls for google/nano-banana-edit model!")
    elif model == "google/nano-banana":
        # NanoBanana для создания (text-to-image) - без image_urls
        payload = {
            "model": model,
            "input": {
                "prompt": prompt[:5000],
                "output_format": "png",  # Всегда "png" как в bot.txt
                "image_size": image_size,
            },
        }
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

