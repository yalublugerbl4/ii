import os
from typing import Optional

import httpx
from fastapi import UploadFile

from ..settings import settings


async def upload_file_public(file: UploadFile, bucket: Optional[str] = None, path_prefix: str = "") -> str:
    bucket_name = bucket or settings.supabase_storage_bucket_uploads
    key = f"{path_prefix}{file.filename}"
    url = f"{settings.supabase_url}/storage/v1/object/{bucket_name}/{key}"
    headers = {
        "Authorization": f"Bearer {settings.supabase_service_key}",
        "Content-Type": file.content_type or "application/octet-stream",
    }
    data = await file.read()
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.put(url, headers=headers, content=data)
        resp.raise_for_status()
    public_url = f"{settings.supabase_url}/storage/v1/object/public/{bucket_name}/{key}"
    return public_url

