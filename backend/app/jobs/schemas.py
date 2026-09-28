from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class BatchCreate(BaseModel):
    profile_version_id: UUID
    document_version_ids: list[UUID] = Field(min_length=1)


class JobStepResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    kind: str
    status: str
    idempotency_key: str
    version: int
    attempt_count: int
    failure_code: str | None
    failure_summary: str | None


class DocumentJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    document_version_id: UUID
    original_filename: str
    document_version_number: int
    status: str
    version: int
    steps: list[JobStepResponse]


class BatchResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    profile_version_id: UUID
    status: str
    version: int
    created_at: datetime
    document_jobs: list[DocumentJobResponse]


class BatchPage(BaseModel):
    items: list[BatchResponse]
    total: int
    offset: int
    limit: int


class RetryStep(BaseModel):
    expected_version: int = Field(ge=1)
