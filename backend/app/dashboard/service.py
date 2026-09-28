"""Dashboard statistics aggregation."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.facts.models import RawFact
from app.jobs.models import DocumentJob, ExtractionBatch
from app.models.models import ModelCall
from app.reviews.models import ReviewTask


def aggregate_dashboard(
    session: Session,
    *,
    project_ids: list[UUID] | None,
) -> dict[str, object]:
    """Aggregate dashboard statistics over the given project scope.

    ``project_ids=None`` means the caller is an admin with unrestricted scope.
    An empty list means the caller can see nothing and all counts are zero.
    """

    if project_ids is not None and not project_ids:
        return _zero_stats()

    conditions = []
    if project_ids is not None:
        conditions.append(RawFact.project_id.in_(project_ids))

    # Facts by review status
    fact_status_rows = session.execute(
        select(RawFact.review_status, func.count(RawFact.id))
        .where(*conditions)
        .group_by(RawFact.review_status)
    ).all()
    facts_by_status = {row[0]: row[1] for row in fact_status_rows}
    facts_total = sum(facts_by_status.values())

    # Facts by route
    fact_route_rows = session.execute(
        select(RawFact.route, func.count(RawFact.id))
        .where(*conditions)
        .group_by(RawFact.route)
    ).all()
    facts_by_route = {row[0]: row[1] for row in fact_route_rows}

    # Average confidence
    avg_confidence = session.scalar(
        select(func.avg(RawFact.confidence)).where(*conditions)
    )

    # Batches by status
    batch_conditions = []
    if project_ids is not None:
        batch_conditions.append(ExtractionBatch.project_id.in_(project_ids))
    batch_rows = session.execute(
        select(ExtractionBatch.status, func.count(ExtractionBatch.id))
        .where(*batch_conditions)
        .group_by(ExtractionBatch.status)
    ).all()
    jobs_by_status = {row[0]: row[1] for row in batch_rows}
    jobs_total = sum(jobs_by_status.values())

    # Document jobs
    doc_job_conditions = []
    if project_ids is not None:
        doc_job_conditions.append(
            DocumentJob.batch.has(ExtractionBatch.project_id.in_(project_ids))
        )
    document_jobs_total = session.scalar(
        select(func.count(DocumentJob.id)).where(*doc_job_conditions)
    ) or 0

    # Review tasks
    review_conditions = []
    if project_ids is not None:
        review_conditions.append(ReviewTask.project_id.in_(project_ids))
    review_rows = session.execute(
        select(ReviewTask.status, func.count(ReviewTask.id))
        .where(*review_conditions)
        .group_by(ReviewTask.status)
    ).all()
    review_by_status = {row[0]: row[1] for row in review_rows}
    review_tasks_pending = review_by_status.get("pending", 0) + review_by_status.get("claimed", 0)
    review_tasks_completed = review_by_status.get("completed", 0)
    total_review = review_tasks_pending + review_tasks_completed
    review_progress_pct = (
        round(review_tasks_completed / total_review * 100, 1) if total_review else 0.0
    )

    # Token usage by currency
    token_conditions = []
    if project_ids is not None:
        token_conditions.append(ModelCall.project_id.in_(project_ids))
    token_rows = session.execute(
        select(
            func.sum(ModelCall.total_tokens).label("total_tokens"),
            func.sum(ModelCall.cost).label("cost"),
            ModelCall.currency.label("currency"),
        )
        .where(*token_conditions)
        .group_by(ModelCall.currency)
    ).all()
    tokens = [
        {
            "total_tokens": int(row.total_tokens or 0),
            "cost": Decimal(str(row.cost or 0)),
            "currency": row.currency,
        }
        for row in token_rows
    ]

    return {
        "jobs_total": jobs_total,
        "jobs_by_status": jobs_by_status,
        "document_jobs_total": document_jobs_total,
        "facts_total": facts_total,
        "facts_by_status": facts_by_status,
        "facts_by_route": facts_by_route,
        "avg_confidence": float(avg_confidence) if avg_confidence is not None else None,
        "review_tasks_pending": review_tasks_pending,
        "review_tasks_completed": review_tasks_completed,
        "review_progress_pct": review_progress_pct,
        "tokens": tokens,
    }


def _zero_stats() -> dict[str, object]:
    return {
        "jobs_total": 0,
        "jobs_by_status": {},
        "document_jobs_total": 0,
        "facts_total": 0,
        "facts_by_status": {},
        "facts_by_route": {},
        "avg_confidence": None,
        "review_tasks_pending": 0,
        "review_tasks_completed": 0,
        "review_progress_pct": 0.0,
        "tokens": [],
    }
