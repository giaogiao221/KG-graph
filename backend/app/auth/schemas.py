from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=12)
    roles: list[str] = Field(default_factory=lambda: ["viewer"])
    project_ids: list[UUID] = Field(default_factory=list, max_length=500)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    username: str
    is_disabled: bool
    roles: list[str]


class UserUpdate(BaseModel):
    username: str | None = Field(default=None, min_length=1, max_length=100)
    password: str | None = Field(default=None, min_length=12)
    roles: list[str] | None = None
    is_disabled: bool | None = None


class CurrentUserResponse(BaseModel):
    id: UUID
    username: str
    roles: list[str]
    permissions: list[str]
