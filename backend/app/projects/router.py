from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from sqlalchemy import select

from app.auth.dependencies import auth_session, current_user, require_permissions
from app.auth.models import User
from app.projects.models import ProjectMembership
from app.projects.schemas import (
    MembershipCreate,
    MembershipDetailResponse,
    MembershipResponse,
    MembershipUpdate,
    ProjectCreate,
    ProjectResponse,
)
from app.projects.service import create_project, get_member_project, list_member_projects


router = APIRouter(prefix="/api/projects", tags=["projects"])


def _is_admin(user: User) -> bool:
    return any(role.name == "admin" for role in user.roles)


@router.post("", response_model=ProjectResponse, status_code=201)
def create_project_route(
    payload: ProjectCreate,
    session: Annotated[Session, Depends(auth_session)],
    user: Annotated[User, Depends(require_permissions("records:create"))],
) -> ProjectResponse:
    return ProjectResponse.model_validate(
        create_project(
            session,
            name=payload.name,
            description=payload.description,
            creator_id=user.id,
        )
    )


@router.get("", response_model=list[ProjectResponse])
def list_projects_route(
    session: Annotated[Session, Depends(auth_session)],
    user: Annotated[User, Depends(require_permissions("records:view"))],
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 500,
) -> list[ProjectResponse]:
    return [
        ProjectResponse.model_validate(item)
        for item in list_member_projects(session, user.id, admin=_is_admin(user), offset=offset, limit=limit)
    ]


@router.get("/{project_id}", response_model=ProjectResponse)
def get_project_route(
    project_id: UUID,
    session: Annotated[Session, Depends(auth_session)],
    user: Annotated[User, Depends(require_permissions("records:view"))],
) -> ProjectResponse:
    return ProjectResponse.model_validate(
        get_member_project(session, project_id, user.id, admin=_is_admin(user))
    )


@router.post("/{project_id}/members", response_model=MembershipResponse, status_code=201)
def add_project_member(
    project_id: UUID,
    payload: MembershipCreate,
    session: Annotated[Session, Depends(auth_session)],
    user: Annotated[User, Depends(current_user)],
) -> MembershipResponse:
    admin = _is_admin(user)
    project = get_member_project(session, project_id, user.id, admin=admin)
    if not admin:
        caller = next(
            membership
            for membership in project.memberships
            if membership.user_id == user.id
        )
        if caller.role != "owner":
            raise HTTPException(status_code=403, detail="project owner required")
    if session.get(User, payload.user_id) is None:
        raise HTTPException(status_code=404, detail="user not found")
    if session.scalar(
        select(ProjectMembership.id).where(
            ProjectMembership.project_id == project.id,
            ProjectMembership.user_id == payload.user_id,
        )
    ):
        raise HTTPException(status_code=409, detail="project member already exists")
    membership = ProjectMembership(
        project_id=project.id, user_id=payload.user_id, role=payload.role
    )
    session.add(membership)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        if session.get(User, payload.user_id) is None:
            raise HTTPException(status_code=404, detail="user not found") from None
        raise HTTPException(status_code=409, detail="project member already exists") from None
    session.refresh(membership)
    return MembershipResponse.model_validate(membership)


def _manage_project(project_id: UUID, session: Session, user: User):
    admin = _is_admin(user)
    project = get_member_project(session, project_id, user.id, admin=admin)
    if not admin:
        caller = next((item for item in project.memberships if item.user_id == user.id), None)
        if caller is None or caller.role != "owner":
            raise HTTPException(status_code=403, detail="project owner required")
    return project


@router.get("/{project_id}/members", response_model=list[MembershipDetailResponse])
def list_project_members(
    project_id: UUID,
    session: Annotated[Session, Depends(auth_session)],
    user: Annotated[User, Depends(current_user)],
) -> list[MembershipDetailResponse]:
    _manage_project(project_id, session, user)
    memberships = session.scalars(
        select(ProjectMembership)
        .options(selectinload(ProjectMembership.user))
        .where(ProjectMembership.project_id == project_id)
        .order_by(ProjectMembership.role.desc(), ProjectMembership.id)
    )
    return [MembershipDetailResponse(
        id=item.id, project_id=item.project_id, user_id=item.user_id,
        role=item.role, username=item.user.username,
    ) for item in memberships]


@router.patch("/{project_id}/members/{membership_id}", response_model=MembershipResponse)
def update_project_member(
    project_id: UUID,
    membership_id: UUID,
    payload: MembershipUpdate,
    session: Annotated[Session, Depends(auth_session)],
    user: Annotated[User, Depends(current_user)],
) -> MembershipResponse:
    _manage_project(project_id, session, user)
    membership = session.get(ProjectMembership, membership_id)
    if membership is None or membership.project_id != project_id:
        raise HTTPException(status_code=404, detail="project member not found")
    if membership.role == "owner" and payload.role != "owner":
        owners = session.scalar(select(ProjectMembership.id).where(
            ProjectMembership.project_id == project_id,
            ProjectMembership.role == "owner",
            ProjectMembership.id != membership.id,
        ))
        if owners is None:
            raise HTTPException(status_code=422, detail="project must retain an owner")
    membership.role = payload.role
    session.commit(); session.refresh(membership)
    return MembershipResponse.model_validate(membership)


@router.delete("/{project_id}/members/{membership_id}", status_code=204)
def remove_project_member(
    project_id: UUID,
    membership_id: UUID,
    session: Annotated[Session, Depends(auth_session)],
    user: Annotated[User, Depends(current_user)],
) -> None:
    _manage_project(project_id, session, user)
    membership = session.get(ProjectMembership, membership_id)
    if membership is None or membership.project_id != project_id:
        raise HTTPException(status_code=404, detail="project member not found")
    if membership.role == "owner":
        another_owner = session.scalar(select(ProjectMembership.id).where(
            ProjectMembership.project_id == project_id,
            ProjectMembership.role == "owner",
            ProjectMembership.id != membership.id,
        ))
        if another_owner is None:
            raise HTTPException(status_code=422, detail="project must retain an owner")
    session.delete(membership)
    session.commit()
