import asyncio
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .settings import settings

# Убеждаемся, что URL использует asyncpg драйвер
db_url = settings.database_url
if db_url.startswith("postgresql://") and "+asyncpg" not in db_url:
    db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
elif db_url.startswith("postgres://") and "+asyncpg" not in db_url:
    db_url = db_url.replace("postgres://", "postgresql+asyncpg://", 1)

# Убираем старые ssl параметры из URL, если есть
import re
db_url = re.sub(r'[?&]ssl=[^&]*', '', db_url)
db_url = re.sub(r'[?&]sslmode=[^&]*', '', db_url)

# Добавляем sslmode=prefer в URL для asyncpg
separator = "&" if "?" in db_url else "?"
db_url = f"{db_url}{separator}sslmode=prefer"

engine = create_async_engine(
    db_url,
    echo=False,
    future=True,
    pool_pre_ping=True
)
SessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    from . import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)


def init_db_blocking() -> None:
    asyncio.run(init_db())

