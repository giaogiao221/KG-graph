"""Dashboard statistics response schemas."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel


class JobStatusCounts(BaseModel):
    queued: int = 0
    running: int = 0
    partial_success: int = 0
    completed: int = 0
    retryable_failed: int = 0
    permanent_failed: int = 0
    cancelled: int = 0


class FactStatusCounts(BaseModel):
    candidate: int = 0
    candidate_review: int = 0
    approved: int = 0
    rejected: int = 0


class TokenSummary(BaseModel):
    total_tokens: int
    cost: Decimal
    currency: str


class DashboardStatsResponse(BaseModel):
    jobs_total: int
    jobs_by_status: JobStatusCounts
    document_jobs_total: int
    facts_total: int
    facts_by_status: FactStatusCounts
    facts_by_route: dict[str, int]
    avg_confidence: float | None
    review_tasks_pending: int
    review_tasks_completed: int
    review_progress_pct: float
    tokens: list[TokenSummary]
