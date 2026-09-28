from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PromptTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=500)
    text_prompt: str = Field(min_length=40, max_length=16000)
    table_prompt: str = Field(min_length=40, max_length=16000)
    is_enabled: bool = True


class PromptTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=500)
    text_prompt: str | None = Field(default=None, min_length=40, max_length=16000)
    table_prompt: str | None = Field(default=None, min_length=40, max_length=16000)
    is_enabled: bool | None = None


class PromptTemplateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str
    text_prompt: str
    table_prompt: str
    is_enabled: bool
    created_at: datetime
    updated_at: datetime
