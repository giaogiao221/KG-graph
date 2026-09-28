from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None


class ProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str | None
    created_at: datetime


class MembershipCreate(BaseModel):
    user_id: UUID
    role: Literal["owner", "member"] = "member"


class MembershipResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    user_id: UUID
    role: str


class MembershipUpdate(BaseModel):
    role: Literal["owner", "member"]


class MembershipDetailResponse(MembershipResponse):
    username: str
