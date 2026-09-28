from __future__ import annotations

import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.auth.dependencies import auth_session, require_permissions
from app.auth.models import User
from app.jobs.models import DocumentJob, ExtractionBatch, JobStep
from app.jobs.schemas import BatchCreate, BatchPage, BatchResponse, JobStepResponse, RetryStep
from app.jobs.service import BatchValidationError, create_batch
from app.jobs.state import InvalidTransition, VersionConflict, transition_step
from app.jobs.tasks import stage_ready_dispatches_for_job
from app.projects.service import get_member_project


router = APIRouter(prefix="/api/projects/{project_id}/batches", tags=["jobs"])


def _is_admin(user: User) -> bool:
    return any(role.name == "admin" for role in user.roles)


def _batch_statement(project_id: UUID, batch_id: UUID | None = None):
    statement = (
        select(ExtractionBatch)
        .options(
            selectinload(ExtractionBatch.document_jobs).selectinload(DocumentJob.steps),
            selectinload(ExtractionBatch.document_jobs).selectinload(DocumentJob.document_version),
        )
        .where(ExtractionBatch.project_id == project_id)
    )
    return statement.where(ExtractionBatch.is_archived.is_(False)) if batch_id is None else statement.where(ExtractionBatch.id == batch_id)


def _get_batch(session: Session, project_id: UUID, batch_id: UUID) -> ExtractionBatch:
    batch = session.scalar(_batch_statement(project_id, batch_id))
    if batch is None:
        raise HTTPException(status_code=404, detail="batch not found")
    return batch


@router.post("", response_model=BatchResponse, status_code=201)
def create_batch_route(
    project_id: UUID,
    payload: BatchCreate,
    session: Annotated[Session, Depends(auth_session)],
    user: Annotated[User, Depends(require_permissions("records:create"))],
) -> BatchResponse:
    get_member_project(session, project_id, user.id, admin=_is_admin(user))
    try:
        batch = create_batch(
            session, payload.profile_version_id, payload.document_version_ids,
            created_by_id=user.id,
        )
        if batch.project_id != project_id:
            session.rollback()
            raise HTTPException(status_code=404, detail="batch inputs not found")
        session.commit()
        session.refresh(batch)
    except BatchValidationError:
        session.rollback()
        raise HTTPException(status_code=404, detail="batch inputs not found") from None
    return BatchResponse.model_validate(batch)


@router.get("", response_model=BatchPage)
def list_batches_route(
    project_id: UUID,
    session: Annotated[Session, Depends(auth_session)],
    user: Annotated[User, Depends(require_permissions("records:view"))],
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> BatchPage:
    get_member_project(session, project_id, user.id, admin=_is_admin(user))
    total = session.scalar(
        select(func.count()).select_from(ExtractionBatch).where(ExtractionBatch.project_id == project_id, ExtractionBatch.is_archived.is_(False))
    ) or 0
    items = session.scalars(
        _batch_statement(project_id)
        .order_by(ExtractionBatch.created_at.desc(), ExtractionBatch.id)
        .offset(offset)
        .limit(limit)
    )
    return BatchPage(items=list(items), total=total, offset=offset, limit=limit)


@router.get("/{batch_id}", response_model=BatchResponse)
def get_batch_route(
    project_id: UUID,
    batch_id: UUID,
    session: Annotated[Session, Depends(auth_session)],
    user: Annotated[User, Depends(require_permissions("records:view"))],
) -> BatchResponse:
    get_member_project(session, project_id, user.id, admin=_is_admin(user))
    return BatchResponse.model_validate(_get_batch(session, project_id, batch_id))


@router.post("/{batch_id}/steps/{step_id}/retry", response_model=JobStepResponse)
def retry_step_route(
    project_id: UUID,
    batch_id: UUID,
    step_id: UUID,
    payload: RetryStep,
    session: Annotated[Session, Depends(auth_session)],
    user: Annotated[User, Depends(require_permissions("records:create"))],
) -> JobStepResponse:
    get_member_project(session, project_id, user.id, admin=_is_admin(user))
    step = session.scalar(
        select(JobStep)
        .join(DocumentJob)
        .join(ExtractionBatch)
        .where(
            JobStep.id == step_id,
            DocumentJob.batch_id == batch_id,
            ExtractionBatch.id == batch_id,
            ExtractionBatch.project_id == project_id,
        )
    )
    if step is None:
        raise HTTPException(status_code=404, detail="step not found")
    if step.status != "retryable_failed":
        raise HTTPException(status_code=409, detail="step is not retryable")
    try:
        value = transition_step(session, step.id, "queued", payload.expected_version)
        stage_ready_dispatches_for_job(session, value.document_job_id)
        session.commit()
        session.refresh(value)
    except (InvalidTransition, VersionConflict):
        session.rollback()
        raise HTTPException(status_code=409, detail="step changed; reload required") from None
    return JobStepResponse.model_validate(value)


@router.post("/{batch_id}/cancel", response_model=BatchResponse)
def cancel_batch_route(
    project_id: UUID, batch_id: UUID,
    session: Annotated[Session, Depends(auth_session)],
    user: Annotated[User, Depends(require_permissions("records:create"))],
) -> BatchResponse:
    get_member_project(session, project_id, user.id, admin=_is_admin(user))
    batch = _get_batch(session, project_id, batch_id)
    for job in batch.document_jobs:
        for step in job.steps:
            if step.status not in {"completed", "permanent_failed", "cancelled"}:
                step.status = "cancelled"; step.lease_token = None; step.lease_expires_at = None
        job.status = "cancelled"
    batch.status = "cancelled"
    session.commit(); session.refresh(batch)
    return BatchResponse.model_validate(batch)


@router.delete("/{batch_id}", status_code=204)
def delete_batch_route(
    project_id: UUID, batch_id: UUID,
    session: Annotated[Session, Depends(auth_session)],
    user: Annotated[User, Depends(require_permissions("records:create"))],
) -> None:
    get_member_project(session, project_id, user.id, admin=_is_admin(user))
    batch = _get_batch(session, project_id, batch_id)
    # Model calls are immutable audit records. A task delete is a soft delete.
    for job in batch.document_jobs:
        for step in job.steps:
            if step.status not in {"completed", "permanent_failed", "cancelled"}:
                step.status = "cancelled"; step.lease_token = None; step.lease_expires_at = None
        job.status = "cancelled"
    batch.status = "cancelled"
    batch.is_archived = True
    session.commit()


@router.get("/{batch_id}/events")
def batch_events_route(
    project_id: UUID,
    batch_id: UUID,
    session: Annotated[Session, Depends(auth_session)],
    user: Annotated[User, Depends(require_permissions("records:view"))],
) -> StreamingResponse:
    get_member_project(session, project_id, user.id, admin=_is_admin(user))
    payload = BatchResponse.model_validate(
        _get_batch(session, project_id, batch_id)
    ).model_dump(mode="json")
    body = f"event: progress\ndata: {json.dumps(payload, separators=(',', ':'))}\n\nretry: 3000\n\n"
    return StreamingResponse(
        iter((body,)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
