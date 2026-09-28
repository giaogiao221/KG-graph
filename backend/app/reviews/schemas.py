from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ReviewCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["approve", "modify_approve", "reject", "dispute", "delete", "publish"]
    patch: dict[str, str] = Field(default_factory=dict)
    expected_version: int = Field(ge=0)
    review_task_id: UUID
    expected_lease_version: int = Field(ge=1)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


class FactVersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    root_id: UUID
    raw_fact_id: UUID | None
    project_id: UUID
    version_number: int
    actor_id: UUID
    action: str
    row_json: dict[str, str]
    editable_values: dict[str, str]
    patch_json: dict[str, str]
    audit_json: dict[str, object]
    is_tombstone: bool
    is_published: bool
    created_at: datetime


class FactVersionPage(BaseModel):
    items: list[FactVersionResponse]
    total: int
    offset: int
    limit: int


class ReviewTaskCreate(BaseModel):
    root_id: UUID | None = None
    fact_id: UUID | None = None


class ReviewTaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    project_id: UUID
    root_id: UUID
    raw_fact_id: UUID | None
    batch_id: UUID | None
    subject: str
    property: str
    value: str
    reviewer_id: UUID | None
    lease_expires_at: datetime | None
    lease_version: int
    status: str
    created_at: datetime


class ReviewTaskPage(BaseModel):
    items: list[ReviewTaskResponse]
    total: int
    offset: int
    limit: int


class ReviewTaskBatchClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_ids: list[UUID] = Field(min_length=1, max_length=25)

    @field_validator("task_ids")
    @classmethod
    def task_ids_must_be_unique(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("任务不能重复")
        return value


class ReviewTaskBatchClaimResponse(BaseModel):
    items: list[ReviewTaskResponse]
    skipped_task_ids: list[UUID]


class ReviewBatchSummary(BaseModel):
    batch_id: UUID
    created_at: datetime
    preset: str
    total: int
    pending: int
    claimed: int
    completed: int


class ReviewBatchPage(BaseModel):
    items: list[ReviewBatchSummary]
    total: int


class LeaseChange(BaseModel):
    expected_version: int = Field(ge=1)


class TaskAssignment(BaseModel):
    reviewer_id: UUID


class ManualFactCreate(BaseModel):
    fields: dict[str, str]
    document_id: UUID | None = None
    document_version_id: UUID | None = None
    idempotency_key: str = Field(min_length=1, max_length=128)
