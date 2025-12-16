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
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(url, headers=headers, content=json.dumps(payload))
    try:
        data = resp.json()
    except Exception:
        raise KieError(f"Create task failed: HTTP {resp.status_code}")
    if not (isinstance(data, dict) and str(data.get("code")) == "200"):
        raise KieError(f"Create task failed: {data}")
    task_id = (data.get("data") or {}).get("taskId")
    if not task_id:
        raise KieError(f"Create task missing taskId: {data}")
    return str(task_id)


async def poll_task(task_id: str) -> dict:
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
    for key in ("resultUrl", "url", "imageUrl", "resultImageUrl"):
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
) -> Dict[str, Any]:
    image_urls_list: List[str] = list(image_urls or [])
    if model.lower() in {"nanobanana pro", "nanobanana_pro", "nanobanana_pro".lower()} or "pro" in model.lower():
        payload = {
            "model": model,
            "input": {
                "prompt": prompt,
                "aspect_ratio": aspect_ratio or "auto",
                "output_format": output_format or "png",
            },
        }
        if resolution:
            payload["input"]["resolution"] = resolution
        if image_urls_list:
            payload["input"]["image_input"] = image_urls_list[:10]
    else:
        payload = {
            "model": model,
            "input": {
                "prompt": prompt,
                "output_format": output_format or "png",
                "image_size": aspect_ratio or "auto",
            },
        }
        if image_urls_list:
            payload["input"]["image_urls"] = image_urls_list
            payload["input"]["mode"] = "edit"
    return payload

