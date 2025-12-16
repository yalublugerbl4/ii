from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from .. import schemas
from ..auth import get_current_user
from ..db import get_session
from ..models import Generation

router = APIRouter(prefix="/history", tags=["history"])


@router.get("", response_model=list[schemas.GenerationOut])
async def my_history(user=Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(Generation).where(Generation.tgid == user.tgid).order_by(Generation.created_at.desc())
    )
    return result.scalars().all()


@router.get("/{generation_id}", response_model=schemas.GenerationOut)
async def get_generation(
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
    return gen

