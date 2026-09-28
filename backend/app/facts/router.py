from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import auth_session, require_permissions
from app.auth.models import User
from app.facts.models import RawFact
from app.facts.schemas import FactBulkDelete, FactDetailResponse, FactPage
from app.facts.service import FactQuery, get_project_fact_detail, query_project_facts
from app.projects.service import get_member_project
from app.reviews.models import FactVersion, ReviewTask
from app.reviews.service import (
    InvalidReview,
    LeaseConflict,
    VersionConflict,
    claim_review_task,
    create_raw_review_task,
    ensure_raw_root,
    review_fact,
)


router = APIRouter(prefix="/api/projects", tags=["facts"])


def _is_admin(user: User) -> bool:
    return any(role.name == "admin" for role in user.roles)


def _require_admin(user: User) -> None:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="administrator required")


def _delete_fact_with_audit(session: Session, fact: RawFact, user_id: UUID) -> None:
    """Create a review tombstone for one raw fact without mutating extraction data."""
    root = ensure_raw_root(session, fact.id, user_id)
    task = create_raw_review_task(session, fact.id, user_id)
    if task.status == "pending":
        task = claim_review_task(session, task.id, user_id)
    elif task.status != "claimed" or task.reviewer_id != user_id:
        raise LeaseConflict("fact is currently being reviewed by another user")
    latest = session.scalar(
        select(FactVersion.version_number)
        .where(FactVersion.root_id == root.id)
        .order_by(FactVersion.version_number.desc())
        .limit(1)
    ) or 0
    review_fact(
        session,
        root.id,
        "delete",
        {},
        latest,
        user_id,
        review_task_id=task.id,
        expected_lease_version=task.lease_version,
        audit={"source": "facts_result_list"},
    )


@router.get("/{project_id}/facts", response_model=FactPage)
def list_project_facts(
    project_id: UUID,
    session: Annotated[Session, Depends(auth_session)],
    user: Annotated[User, Depends(require_permissions("records:view"))],
    document_id: UUID | None = None,
    document_version_id: UUID | None = None,
    subject: Annotated[str | None, Query(max_length=512)] = None,
    property: Annotated[str | None, Query(max_length=512)] = None,
    source_type: Annotated[str | None, Query(max_length=128)] = None,
    source: Annotated[str | None, Query(max_length=128)] = None,
    review_status: Literal[
        "candidate", "candidate_review", "approved", "rejected"
    ] | None = None,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> FactPage:
    get_member_project(session, project_id, user.id, admin=_is_admin(user))
    items, total = query_project_facts(
        session,
        project_id,
        FactQuery(
            document_id=document_id,
            document_version_id=document_version_id,
            subject=subject,
            property=property,
            source_type=source_type,
            extraction_source=source,
            review_status=review_status,
            offset=offset,
            limit=limit,
        ),
    )
    return FactPage(items=items, total=total, offset=offset, limit=limit)


@router.get("/{project_id}/facts/{fact_id}", response_model=FactDetailResponse)
def get_project_fact(
    project_id: UUID,
    fact_id: UUID,
    session: Annotated[Session, Depends(auth_session)],
    user: Annotated[User, Depends(require_permissions("records:view"))],
) -> FactDetailResponse:
    get_member_project(session, project_id, user.id, admin=_is_admin(user))
    return FactDetailResponse.model_validate(
        get_project_fact_detail(session, project_id, fact_id)
    )


@router.delete("/{project_id}/facts/{fact_id}", status_code=204)
def delete_project_fact(
    project_id: UUID,
    fact_id: UUID,
    session: Annotated[Session, Depends(auth_session)],
    user: Annotated[User, Depends(require_permissions("records:review"))],
) -> None:
    """Hide one fact through an audited review tombstone, never raw deletion."""
    _require_admin(user)
    get_member_project(session, project_id, user.id, admin=True)
    fact = session.scalar(
        select(RawFact).where(RawFact.id == fact_id, RawFact.project_id == project_id)
    )
    if fact is None:
        raise HTTPException(status_code=404, detail="fact not found")
    try:
        _delete_fact_with_audit(session, fact, user.id)
        session.commit()
    except (VersionConflict, LeaseConflict) as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except InvalidReview as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.delete("/{project_id}/facts", status_code=204)
def delete_project_facts(
    project_id: UUID,
    payload: FactBulkDelete,
    session: Annotated[Session, Depends(auth_session)],
    user: Annotated[User, Depends(require_permissions("records:review"))],
) -> None:
    """Atomically hide a selected set of facts through audited tombstones."""
    _require_admin(user)
    get_member_project(session, project_id, user.id, admin=True)
    fact_ids = list(dict.fromkeys(payload.fact_ids))
    facts = list(
        session.scalars(
            select(RawFact).where(
                RawFact.id.in_(fact_ids), RawFact.project_id == project_id
            )
        )
    )
    found_ids = {fact.id for fact in facts}
    if len(found_ids) != len(fact_ids):
        raise HTTPException(status_code=404, detail="one or more facts were not found")
    facts_by_id = {fact.id: fact for fact in facts}
    try:
        for fact_id in fact_ids:
            _delete_fact_with_audit(session, facts_by_id[fact_id], user.id)
        session.commit()
    except (VersionConflict, LeaseConflict) as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except InvalidReview as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
