from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class FactResponse(BaseModel):
    id: UUID
    project_id: UUID
    document_id: UUID
    document_version_id: UUID
    document_job_id: UUID
    graph_fact_key: str | None
    subject: str
    property: str
    value: str
    unit: str
    condition: str
    source_type: str
    confidence: float | None
    review_status: str
    evidence_text: str
    evidence_truncated: bool
    evidence_hash: str
    extraction_source: str
    route: str
    created_at: datetime


class FactPage(BaseModel):
    items: list[FactResponse]
    total: int
    offset: int
    limit: int


class FactDetailResponse(FactResponse):
    row_json: dict[str, str]
    truncated_fields: list[str]


class FactBulkDelete(BaseModel):
    """The explicitly selected facts on one results page."""

    fact_ids: list[UUID] = Field(min_length=1, max_length=100)
