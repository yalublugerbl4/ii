from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class UserOut(BaseModel):
    tgid: int
    balance: float = 0.0


class TemplateOut(BaseModel):
    id: UUID
    title: str
    description: str
    badge: Optional[str] = None
    is_new: bool
    is_popular: bool
    default_prompt: Optional[str] = None
    preview_image_url: Optional[str] = None
    examples: Optional[list[Any]] = None
    created_at: datetime

    class Config:
        from_attributes = True


class TemplateCreate(BaseModel):
    title: str
    description: str
    badge: Optional[str] = None
    is_new: bool = False
    is_popular: bool = False
    default_prompt: Optional[str] = None
    preview_image_url: Optional[str] = None
    examples: Optional[list[Any]] = None


class TemplateUpdate(TemplateCreate):
    pass


class GenerationOut(BaseModel):
    id: UUID
    template_id: Optional[UUID]
    model: str
    aspect_ratio: Optional[str]
    resolution: Optional[str]
    output_format: Optional[str]
    prompt: str
    status: str
    kie_task_id: Optional[str]
    result_url: Optional[str]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class GenerationCreate(BaseModel):
    prompt: str = Field(..., max_length=6000)
    model: str
    aspect_ratio: Optional[str] = None
    resolution: Optional[str] = None
    output_format: Optional[str] = "png"
    template_id: Optional[UUID] = None


class TelegramAuthRequest(BaseModel):
    initData: str


class TelegramAuthResponse(BaseModel):
    accessToken: str
    user: UserOut
    isAdmin: bool


class ModelInfo(BaseModel):
    id: str
    title: str
    description: str
    modes: list[str] = ["image"]
    supports_resolution: bool = False
    supports_output_format: bool = True
    default_output_format: str = "png"

