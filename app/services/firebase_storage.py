import json
import logging
from io import BytesIO
from typing import Optional

import firebase_admin
from firebase_admin import credentials, storage
from fastapi import UploadFile

from ..settings import settings

logger = logging.getLogger(__name__)

# Инициализация Firebase Admin (один раз)
_firebase_app = None


def get_firebase_app():
    """Получить или инициализировать Firebase Admin приложение"""
    global _firebase_app
    
    if _firebase_app is None:
        try:
            # Пробуем использовать credentials из переменной окружения
            if settings.firebase_credentials_json:
                try:
                    # Если это JSON строка, парсим её
                    cred_dict = json.loads(settings.firebase_credentials_json)
                    cred = credentials.Certificate(cred_dict)
                except json.JSONDecodeError:
                    # Если это путь к файлу
                    cred = credentials.Certificate(settings.firebase_credentials_json)
            else:
                # Используем Application Default Credentials (для production)
                cred = credentials.ApplicationDefault()
            
            _firebase_app = firebase_admin.initialize_app(
                cred,
                {
                    'storageBucket': settings.firebase_storage_bucket or 'iiapp-66742.firebasestorage.app'
                }
            )
            logger.info("Firebase Admin initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Firebase Admin: {e}")
            raise
    
    return _firebase_app


async def upload_to_firebase_storage(
    file: UploadFile,
    folder: str = "template-previews"
) -> str:
    """
    Загрузить файл в Firebase Storage и вернуть публичный URL
    
    Args:
        file: Файл для загрузки
        folder: Папка в Storage (по умолчанию "template-previews")
    
    Returns:
        Публичный URL загруженного файла
    """
    try:
        app = get_firebase_app()
        bucket = storage.bucket()
        
        # Генерируем уникальное имя файла
        import uuid
        file_ext = file.filename.split('.')[-1] if '.' in file.filename else 'jpg'
        file_name = f"{folder}/{uuid.uuid4()}.{file_ext}"
        
        # Читаем содержимое файла
        file_content = await file.read()
        file_obj = BytesIO(file_content)
        
        # Загружаем в Firebase Storage
        blob = bucket.blob(file_name)
        blob.upload_from_file(file_obj, content_type=file.content_type or 'image/jpeg')
        
        # Делаем файл публичным
        blob.make_public()
        
        # Получаем публичный URL
        public_url = blob.public_url
        
        logger.info(f"File uploaded to Firebase Storage: {file_name}, URL: {public_url}")
        return public_url
        
    except Exception as e:
        logger.error(f"Failed to upload to Firebase Storage: {e}", exc_info=True)
        raise


async def upload_from_url_to_firebase_storage(
    url: str,
    folder: str = "template-previews"
) -> str:
    """
    Загрузить файл по URL в Firebase Storage и вернуть публичный URL
    
    Args:
        url: URL файла для загрузки
        folder: Папка в Storage (по умолчанию "template-previews")
    
    Returns:
        Публичный URL загруженного файла
    """
    import httpx
    
    try:
        # Загружаем файл по URL
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            
            # Определяем расширение из URL или Content-Type
            content_type = response.headers.get('content-type', 'image/jpeg')
            ext = 'jpg'
            if 'png' in content_type:
                ext = 'png'
            elif 'gif' in content_type:
                ext = 'gif'
            elif 'webp' in content_type:
                ext = 'webp'
            
            file_content = response.content
            if len(file_content) == 0:
                raise ValueError("Empty file content")
            
            # Загружаем в Firebase Storage
            app = get_firebase_app()
            bucket = storage.bucket()
            
            import uuid
            file_name = f"{folder}/{uuid.uuid4()}.{ext}"
            
            blob = bucket.blob(file_name)
            blob.upload_from_string(file_content, content_type=content_type)
            blob.make_public()
            
            public_url = blob.public_url
            logger.info(f"File uploaded to Firebase Storage from URL: {file_name}, URL: {public_url}")
            return public_url
            
    except Exception as e:
        logger.error(f"Failed to upload from URL to Firebase Storage: {e}", exc_info=True)
        raise

