from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ExportFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["approve", "modify_approve", "reject", "dispute", "manual_create", "publish"] | None = None
    document_id: UUID | None = None


class ExportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal["tsv", "csv", "json"] = "tsv"
    include_unreviewed: bool = False
    filters: ExportFilters = Field(default_factory=ExportFilters)
    idempotency_key: str = Field(min_length=1, max_length=128)


class ExportResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    project_id: UUID
    actor_id: UUID
    filters_json: dict[str, str]
    format: str
    include_unreviewed: bool
    record_count: int
    size_bytes: int
    sha256: str | None
    status: str
    completed_at: datetime | None
    created_at: datetime


class ExportPage(BaseModel):
    items: list[ExportResponse]
    total: int
    offset: int
    limit: int
