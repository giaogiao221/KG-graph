from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import auth_session, require_permissions
from app.auth.models import User
from app.facts.models import RawFact
from app.projects.service import get_member_project
from app.reviews.models import ReviewFactRoot, ReviewTask
from app.reviews.schemas import (FactVersionPage, FactVersionResponse, LeaseChange, ManualFactCreate,
    ReviewBatchPage, ReviewCreate, ReviewTaskBatchClaim, ReviewTaskBatchClaimResponse,
    ReviewTaskCreate, ReviewTaskPage, ReviewTaskResponse, TaskAssignment)
from app.reviews.service import (InvalidReview, LeaseConflict, VersionConflict, assign_review_task,
    claim_review_task, create_manual_fact, create_raw_review_task, create_review_task, ensure_raw_root, release_review_task, renew_review_task,
    query_current_facts, review_batch_summaries, review_fact, review_queue, version_history)


router = APIRouter(prefix="/api/projects", tags=["reviews"])


def _admin(user: User) -> bool:
    return any(role.name == "admin" for role in user.roles)


def _project(session: Session, project_id: UUID, user: User) -> None:
    get_member_project(session, project_id, user.id, admin=_admin(user))


def _fact(session: Session, project_id: UUID, fact_id: UUID) -> RawFact:
    value = session.scalar(select(RawFact).where(RawFact.id == fact_id, RawFact.project_id == project_id))
    if value is None: raise HTTPException(status_code=404, detail="事实不存在")
    return value


def _task(session: Session, project_id: UUID, task_id: UUID) -> ReviewTask:
    value = session.scalar(select(ReviewTask).where(ReviewTask.id == task_id, ReviewTask.project_id == project_id))
    if value is None: raise HTTPException(status_code=404, detail="评审任务不存在")
    return value


def _root(session:Session,project_id:UUID,root_id:UUID)->ReviewFactRoot:
    value=session.scalar(select(ReviewFactRoot).where(ReviewFactRoot.id==root_id,ReviewFactRoot.project_id==project_id))
    if value is None:raise HTTPException(status_code=404,detail="事实根不存在")
    return value


def _require_admin(user:Annotated[User,Depends(require_permissions("records:review"))])->User:
    if not _admin(user):raise HTTPException(status_code=403,detail="管理员权限必需")
    return user


@router.post("/{project_id}/review-facts/{root_id}/reviews",response_model=FactVersionResponse,status_code=201)
def create_root_review(project_id:UUID,root_id:UUID,payload:ReviewCreate,session:Annotated[Session,Depends(auth_session)],user:Annotated[User,Depends(require_permissions("records:review"))]):
    _project(session,project_id,user);_root(session,project_id,root_id)
    try:
        value=review_fact(session,root_id,payload.action,payload.patch,payload.expected_version,user.id,review_task_id=payload.review_task_id,expected_lease_version=payload.expected_lease_version,idempotency_key=payload.idempotency_key);session.commit();session.refresh(value);return value
    except (VersionConflict,LeaseConflict) as exc:session.rollback();raise HTTPException(status_code=409,detail=str(exc)) from None
    except InvalidReview as exc:session.rollback();raise HTTPException(status_code=422,detail=str(exc)) from None


@router.get("/{project_id}/review-facts/{root_id}/versions",response_model=FactVersionPage)
def root_versions(project_id:UUID,root_id:UUID,session:Annotated[Session,Depends(auth_session)],user:Annotated[User,Depends(require_permissions("records:view"))],offset:Annotated[int,Query(ge=0,le=1_000_000)]=0,limit:Annotated[int,Query(ge=1,le=500)]=50):
    _project(session,project_id,user);_root(session,project_id,root_id);items,total=version_history(session,project_id,root_id,offset=offset,limit=limit);return FactVersionPage(items=items,total=total,offset=offset,limit=limit)


@router.post("/{project_id}/facts/{fact_id}/reviews", response_model=FactVersionResponse, status_code=201)
def create_review(project_id: UUID, fact_id: UUID, payload: ReviewCreate,
                  session: Annotated[Session, Depends(auth_session)],
                  user: Annotated[User, Depends(require_permissions("records:review"))]):
    _project(session, project_id, user); _fact(session, project_id, fact_id)
    try:
        root=ensure_raw_root(session,fact_id,user.id)
        value = review_fact(session, root.id, payload.action, payload.patch,
                            payload.expected_version, user.id,
                            review_task_id=payload.review_task_id,
                            expected_lease_version=payload.expected_lease_version,
                            idempotency_key=payload.idempotency_key)
        session.commit(); session.refresh(value); return value
    except (VersionConflict, LeaseConflict) as exc:
        session.rollback(); raise HTTPException(status_code=409, detail=str(exc)) from None
    except InvalidReview as exc:
        session.rollback(); raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/{project_id}/facts/{fact_id}/versions", response_model=FactVersionPage)
def list_versions(project_id: UUID, fact_id: UUID,
                  session: Annotated[Session, Depends(auth_session)],
                  user: Annotated[User, Depends(require_permissions("records:view"))],
                  offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
                  limit: Annotated[int, Query(ge=1, le=500)] = 50):
    _project(session, project_id, user); _fact(session, project_id, fact_id)
    root=session.scalar(select(ReviewFactRoot).where(ReviewFactRoot.raw_fact_id==fact_id))
    items,total=([],0) if root is None else version_history(session,project_id,root.id,offset=offset,limit=limit)
    return FactVersionPage(items=items, total=total, offset=offset, limit=limit)


@router.get("/{project_id}/current-facts", response_model=FactVersionPage)
def list_current_facts(project_id: UUID,
                       session: Annotated[Session, Depends(auth_session)],
                       user: Annotated[User, Depends(require_permissions("records:view"))],
                       accepted_only: bool = False,
                       action: Literal["approve", "modify_approve", "reject", "dispute", "manual_create", "delete", "publish"] | None = None,
                       offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
                       limit: Annotated[int, Query(ge=1, le=500)] = 50):
    _project(session, project_id, user)
    items, total = query_current_facts(session, project_id, accepted_only=accepted_only,
                                       action=action, offset=offset, limit=limit)
    return FactVersionPage(items=items, total=total, offset=offset, limit=limit)


@router.post("/{project_id}/review-tasks", response_model=ReviewTaskResponse, status_code=201)
def add_task(project_id: UUID, payload: ReviewTaskCreate,
             session: Annotated[Session, Depends(auth_session)],
             user: Annotated[User, Depends(require_permissions("records:review"))]):
    _project(session, project_id, user)
    try:
        if (payload.root_id is None)==(payload.fact_id is None):raise InvalidReview("必须且只能提供一个事实身份")
        if payload.root_id is not None:_root(session,project_id,payload.root_id);value=create_review_task(session,payload.root_id,user.id)
        else:_fact(session,project_id,payload.fact_id);value=create_raw_review_task(session,payload.fact_id,user.id)
        session.commit(); session.refresh(value); return value
    except (InvalidReview, LeaseConflict) as exc:
        session.rollback(); raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/{project_id}/review-tasks", response_model=ReviewTaskPage)
def list_tasks(project_id: UUID,
               session: Annotated[Session, Depends(auth_session)],
               user: Annotated[User, Depends(require_permissions("records:view"))],
               status: Literal["pending", "claimed", "completed", "available"] | None = None,
               reviewer_id: UUID | None = None,
               batch_id: UUID | None = None,
               offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
               limit: Annotated[int, Query(ge=1, le=500)] = 50):
    _project(session, project_id, user)
    if reviewer_id is not None and reviewer_id != user.id and not _admin(user):
        raise HTTPException(status_code=403, detail="只能筛选本人领取的评审任务")
    items, total = review_queue(
        session, project_id, status=status, reviewer_id=reviewer_id, batch_id=batch_id,
        offset=offset, limit=limit,
    )
    return ReviewTaskPage(items=items, total=total, offset=offset, limit=limit)


@router.get("/{project_id}/review-batches", response_model=ReviewBatchPage)
def list_review_batches(
    project_id: UUID,
    session: Annotated[Session, Depends(auth_session)],
    user: Annotated[User, Depends(require_permissions("records:review"))],
):
    _project(session, project_id, user)
    items = review_batch_summaries(session, project_id)
    return ReviewBatchPage(items=items, total=len(items))


@router.post("/{project_id}/review-tasks/claim-batch", response_model=ReviewTaskBatchClaimResponse)
def claim_task_batch(
    project_id: UUID,
    payload: ReviewTaskBatchClaim,
    session: Annotated[Session, Depends(auth_session)],
    user: Annotated[User, Depends(require_permissions("records:review"))],
):
    _project(session, project_id, user)
    claimed: list[ReviewTask] = []
    skipped: list[UUID] = []
    for task_id in payload.task_ids:
        task = session.get(ReviewTask, task_id)
        if task is None or task.project_id != project_id:
            skipped.append(task_id)
            continue
        try:
            claimed.append(claim_review_task(session, task_id, user.id))
        except LeaseConflict:
            skipped.append(task_id)
    session.commit()
    for task in claimed:
        session.refresh(task)
    return ReviewTaskBatchClaimResponse(items=claimed, skipped_task_ids=skipped)


@router.post("/{project_id}/review-tasks/{task_id}/claim", response_model=ReviewTaskResponse)
def claim_task(project_id: UUID, task_id: UUID,
               session: Annotated[Session, Depends(auth_session)],
               user: Annotated[User, Depends(require_permissions("records:review"))]):
    _project(session, project_id, user); _task(session, project_id, task_id)
    try:
        value = claim_review_task(session, task_id, user.id); session.commit(); session.refresh(value); return value
    except LeaseConflict as exc:
        session.rollback(); raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/{project_id}/review-tasks/{task_id}/assign", response_model=ReviewTaskResponse)
def assign_task(project_id: UUID, task_id: UUID, payload: TaskAssignment,
                session: Annotated[Session, Depends(auth_session)],
                user: Annotated[User, Depends(_require_admin)]):
    _project(session, project_id, user); _task(session, project_id, task_id)
    try:
        value=assign_review_task(session,task_id,user.id,payload.reviewer_id);session.commit();session.refresh(value);return value
    except (LeaseConflict,InvalidReview) as exc:
        session.rollback();raise HTTPException(status_code=409,detail=str(exc)) from None


@router.post("/{project_id}/manual-facts", response_model=FactVersionResponse, status_code=201)
def manual_fact(project_id: UUID,payload:ManualFactCreate,
                session:Annotated[Session,Depends(auth_session)],
                user:Annotated[User,Depends(require_permissions("records:review"))]):
    _project(session,project_id,user)
    try:
        value=create_manual_fact(session,project_id,user.id,payload.fields,document_id=payload.document_id,document_version_id=payload.document_version_id,idempotency_key=payload.idempotency_key);session.commit();session.refresh(value);return value
    except (InvalidReview,VersionConflict) as exc:
        session.rollback();raise HTTPException(status_code=409,detail=str(exc)) from None


@router.post("/{project_id}/review-tasks/{task_id}/renew", response_model=ReviewTaskResponse)
def renew_task(project_id: UUID, task_id: UUID, payload: LeaseChange,
               session: Annotated[Session, Depends(auth_session)],
               user: Annotated[User, Depends(require_permissions("records:review"))]):
    _project(session, project_id, user); _task(session, project_id, task_id)
    try:
        value = renew_review_task(session, task_id, user.id, payload.expected_version); session.commit(); session.refresh(value); return value
    except LeaseConflict as exc:
        session.rollback(); raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/{project_id}/review-tasks/{task_id}/release", response_model=ReviewTaskResponse)
def release_task(project_id: UUID, task_id: UUID, payload: LeaseChange,
                 session: Annotated[Session, Depends(auth_session)],
                 user: Annotated[User, Depends(require_permissions("records:review"))]):
    _project(session, project_id, user); _task(session, project_id, task_id)
    try:
        value = release_review_task(session, task_id, user.id, payload.expected_version); session.commit(); session.refresh(value); return value
    except LeaseConflict as exc:
        session.rollback(); raise HTTPException(status_code=409, detail=str(exc)) from None
