from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, aliased
from fastapi import HTTPException

from app.facts.models import RawFact
from app.facts.schema59 import FIELD
from app.reviews.models import FactVersion, ReviewFactRoot


MAX_EVIDENCE_RESPONSE_CHARS = 8_000
MAX_DETAIL_FIELD_CHARS = 8_000


@dataclass(frozen=True, slots=True)
class FactQuery:
    document_id: UUID | None = None
    document_version_id: UUID | None = None
    subject: str | None = None
    property: str | None = None
    source_type: str | None = None
    extraction_source: str | None = None
    review_status: str | None = None
    offset: int = 0
    limit: int = 50


def query_project_facts(
    session: Session, project_id: UUID, query: FactQuery
) -> tuple[list[dict[str, object]], int]:
    latest = (
        select(FactVersion.root_id, func.max(FactVersion.version_number).label("version_number"))
        .group_by(FactVersion.root_id)
        .subquery()
    )
    version = aliased(FactVersion)
    statement = (
        select(RawFact, version)
        .outerjoin(ReviewFactRoot, ReviewFactRoot.raw_fact_id == RawFact.id)
        .outerjoin(latest, latest.c.root_id == ReviewFactRoot.id)
        .outerjoin(
            version,
            (version.root_id == latest.c.root_id)
            & (version.version_number == latest.c.version_number),
        )
    )
    conditions = [RawFact.project_id == project_id]
    # A fact deletion is represented by an immutable review tombstone. Keep
    # the raw extraction for audit/history, but do not surface it as a result.
    conditions.append(or_(version.id.is_(None), version.is_tombstone.is_(False)))
    for column, value in (
        (RawFact.document_id, query.document_id),
        (RawFact.document_version_id, query.document_version_id),
        (RawFact.subject, query.subject),
        (RawFact.property, query.property),
        (RawFact.source_type, query.source_type),
        (RawFact.extraction_source, query.extraction_source),
    ):
        if value is not None:
            conditions.append(column == value)
    if query.review_status is not None:
        effective_status = case(
            (
                version.id.is_not(None),
                version.row_json[FIELD["review_status"]].as_string(),
            ),
            else_=RawFact.review_status,
        )
        if query.review_status == "candidate":
            conditions.append(effective_status.in_(("candidate", "candidate_review")))
        else:
            conditions.append(effective_status == query.review_status)
    total = session.scalar(
        select(func.count()).select_from(statement.where(*conditions).subquery())
    ) or 0
    facts = list(
        session.execute(
            statement.where(*conditions)
            .order_by(RawFact.created_at.desc(), RawFact.id)
            .offset(query.offset)
            .limit(query.limit)
        ).all()
    )
    items: list[dict[str, object]] = []
    for fact, current in facts:
        row = current.row_json if current is not None else fact.row_json
        evidence_value = row.get(FIELD["evidence_text"], fact.evidence_text)
        evidence = evidence_value[:MAX_EVIDENCE_RESPONSE_CHARS]
        items.append(
            {
                "id": fact.id,
                "project_id": fact.project_id,
                "document_id": fact.document_id,
                "document_version_id": fact.document_version_id,
                "document_job_id": fact.document_job_id,
                "graph_fact_key": fact.graph_fact_key,
                "subject": row.get(FIELD["subject"], fact.subject),
                "property": row.get(FIELD["property"], fact.property),
                "value": row.get(FIELD["value"], fact.value),
                "unit": row.get(FIELD["unit"], fact.unit),
                "condition": row.get(FIELD["condition"], fact.condition),
                "source_type": fact.source_type,
                "confidence": fact.confidence,
                "review_status": row.get(FIELD["review_status"], fact.review_status),
                "evidence_text": evidence,
                "evidence_truncated": len(evidence) != len(evidence_value),
                "evidence_hash": row.get(FIELD["evidence_hash"], fact.evidence_hash),
                "extraction_source": fact.extraction_source,
                "route": fact.route,
                "created_at": fact.created_at,
            }
        )
    return items, int(total)


def get_project_fact_detail(
    session: Session, project_id: UUID, fact_id: UUID
) -> dict[str, object]:
    fact = session.scalar(
        select(RawFact).where(RawFact.id == fact_id, RawFact.project_id == project_id)
    )
    if fact is None:
        raise HTTPException(status_code=404, detail="fact not found")
    root = session.scalar(select(ReviewFactRoot).where(ReviewFactRoot.raw_fact_id == fact.id))
    current = None if root is None else session.scalar(
        select(FactVersion)
        .where(FactVersion.root_id == root.id)
        .order_by(FactVersion.version_number.desc())
        .limit(1)
    )
    effective_row = current.row_json if current is not None else fact.row_json
    row_json: dict[str, str] = {}
    truncated: list[str] = []
    for key, value in effective_row.items():
        safe_value = str(value)
        if len(safe_value) > MAX_DETAIL_FIELD_CHARS:
            safe_value = safe_value[:MAX_DETAIL_FIELD_CHARS]
            truncated.append(key)
        row_json[key] = safe_value
    base = {
        "id": fact.id, "project_id": fact.project_id,
        "document_id": fact.document_id, "document_version_id": fact.document_version_id,
        "document_job_id": fact.document_job_id, "graph_fact_key": fact.graph_fact_key,
        "subject": effective_row.get(FIELD["subject"], fact.subject), "property": effective_row.get(FIELD["property"], fact.property), "value": effective_row.get(FIELD["value"], fact.value),
        "unit": effective_row.get(FIELD["unit"], fact.unit), "condition": effective_row.get(FIELD["condition"], fact.condition), "source_type": fact.source_type,
        "confidence": fact.confidence, "review_status": effective_row.get(FIELD["review_status"], fact.review_status),
        "evidence_text": effective_row.get(FIELD["evidence_text"], fact.evidence_text)[:MAX_EVIDENCE_RESPONSE_CHARS],
        "evidence_truncated": len(effective_row.get(FIELD["evidence_text"], fact.evidence_text)) > MAX_EVIDENCE_RESPONSE_CHARS,
        "evidence_hash": effective_row.get(FIELD["evidence_hash"], fact.evidence_hash), "extraction_source": fact.extraction_source,
        "route": fact.route, "created_at": fact.created_at,
    }
    base.update(row_json=row_json, truncated_fields=truncated)
    return base
