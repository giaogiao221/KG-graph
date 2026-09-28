from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ModelConfigCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    provider: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    endpoint: str = Field(min_length=1, max_length=2048)
    model_name: str = Field(min_length=1, max_length=200)
    api_key: str = Field(min_length=1, max_length=8192)
    allowed_hosts: tuple[str, ...] = Field(min_length=1, max_length=32)
    allow_private_network: bool = False
    allow_insecure_http: bool = False
    provider_supports_idempotency: bool = True
    is_enabled: bool = True

    @field_validator("allowed_hosts")
    @classmethod
    def valid_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(host.rstrip(".").casefold() for host in value)
        if any(not host or "/" in host or "@" in host or ":" in host for host in normalized):
            raise ValueError("invalid allowed host")
        if len(set(normalized)) != len(normalized):
            raise ValueError("duplicate allowed host")
        return normalized

    @model_validator(mode="after")
    def safe_endpoint(self) -> ModelConfigCreate:
        parsed = urlsplit(self.endpoint)
        host = (parsed.hostname or "").rstrip(".").casefold()
        if parsed.username is not None or parsed.password is not None or parsed.fragment or parsed.query or host not in self.allowed_hosts:
            raise ValueError("invalid model endpoint")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and self.allow_insecure_http):
            raise ValueError("invalid model endpoint")
        if parsed.port not in (None, 80, 443) and not self.allow_private_network:
            raise ValueError("invalid model endpoint")
        return self


class ModelConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    endpoint: str | None = Field(default=None, min_length=1, max_length=2048)
    model_name: str | None = Field(default=None, min_length=1, max_length=200)
    allowed_hosts: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=32)
    allow_private_network: bool | None = None
    allow_insecure_http: bool | None = None
    provider_supports_idempotency: bool | None = None
    is_enabled: bool | None = None


class SecretRotate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: str = Field(min_length=1, max_length=8192)
    key_id: str = Field(default="primary", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class ModelConfigResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    provider: str
    endpoint: str
    model_name: str
    api_key: Literal["********"] = "********"
    allowed_hosts: list[str]
    allow_private_network: bool
    allow_insecure_http: bool
    provider_supports_idempotency: bool
    is_enabled: bool
    cipher_version: int
    key_id: str
    created_at: datetime
    updated_at: datetime


class EnabledModelResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    provider: str
    model_name: str


class PriceVersionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    effective_from: datetime
    effective_to: datetime | None = None
    prompt_per_million: Decimal = Field(ge=0, max_digits=20, decimal_places=8)
    completion_per_million: Decimal = Field(ge=0, max_digits=20, decimal_places=8)
    cached_per_million: Decimal = Field(default=Decimal("0"), ge=0, max_digits=20, decimal_places=8)
    currency: str = Field(default="CNY", pattern=r"^[A-Z]{3}$")

    @model_validator(mode="after")
    def valid_interval(self) -> PriceVersionCreate:
        if self.effective_from.tzinfo is None or (self.effective_to is not None and self.effective_to.tzinfo is None):
            raise ValueError("price timestamps must be timezone-aware")
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("invalid price interval")
        return self


class PriceVersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    model_config_id: UUID
    effective_from: datetime
    effective_to: datetime | None
    prompt_per_million: Decimal
    completion_per_million: Decimal
    cached_per_million: Decimal
    currency: str


class UsageItem(BaseModel):
    key: str
    calls: int
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    total_tokens: int
    cost: Decimal
    currency: str


class UsagePage(BaseModel):
    items: list[UsageItem]
    total: int
    offset: int
    limit: int
