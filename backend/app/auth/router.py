from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.auth.dependencies import auth_session, current_user, require_permissions
from app.auth.models import Role, User
from app.auth.schemas import (
    CurrentUserResponse,
    LoginRequest,
    TokenResponse,
    UserCreate,
    UserResponse,
    UserUpdate,
)
from app.auth.security import create_access_token, hash_password
from app.auth.service import authenticate_user, seed_roles
from app.projects.models import Project, ProjectMembership


router = APIRouter()


def _user_response(user: User) -> UserResponse:
    return UserResponse(
        id=user.id, username=user.username, is_disabled=user.is_disabled,
        roles=sorted(role.name for role in user.roles),
    )


@router.post("/api/auth/login", response_model=TokenResponse)
def login(
    credentials: LoginRequest,
    session: Annotated[Session, Depends(auth_session)],
) -> TokenResponse:
    user = authenticate_user(session, credentials.username, credentials.password)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="invalid username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return TokenResponse(access_token=create_access_token(user.id))


@router.get("/api/auth/me", response_model=CurrentUserResponse)
def me(user: Annotated[User, Depends(current_user)]) -> CurrentUserResponse:
    return CurrentUserResponse(
        id=user.id,
        username=user.username,
        roles=sorted(role.name for role in user.roles),
        permissions=sorted(
            {
                permission.code
                for role in user.roles
                for permission in role.permissions
            }
        ),
    )


@router.post(
    "/api/admin/users",
    response_model=UserResponse,
    status_code=201,
)
def create_user(
    payload: UserCreate,
    session: Annotated[Session, Depends(auth_session)],
    _: Annotated[User, Depends(require_permissions("users:create"))],
) -> UserResponse:
    roles = seed_roles(session)
    unknown_roles = set(payload.roles) - roles.keys()
    if unknown_roles:
        raise HTTPException(status_code=422, detail="unknown role")
    if session.scalar(select(User.id).where(User.username == payload.username)):
        raise HTTPException(status_code=409, detail="username already exists")

    project_ids = list(dict.fromkeys(payload.project_ids))
    if project_ids:
        matched_project_ids = set(session.scalars(
            select(Project.id).where(Project.id.in_(project_ids))
        ))
        if len(matched_project_ids) != len(project_ids):
            raise HTTPException(status_code=404, detail="project not found")

    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        roles=[roles[name] for name in payload.roles],
    )
    session.add(user)
    try:
        session.flush()
        session.add_all(
            ProjectMembership(project_id=project_id, user_id=user.id, role="member")
            for project_id in project_ids
        )
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="username already exists") from None
    session.refresh(user)
    return UserResponse(
        id=user.id,
        username=user.username,
        is_disabled=user.is_disabled,
        roles=[role.name for role in user.roles],
    )


@router.get("/api/admin/users", response_model=list[UserResponse])
def list_users(
    session: Annotated[Session, Depends(auth_session)],
    _: Annotated[User, Depends(require_permissions("users:create"))],
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[UserResponse]:
    users = session.scalars(
        select(User).options(selectinload(User.roles))
        .order_by(User.username, User.id).offset(offset).limit(limit)
    )
    return [_user_response(user) for user in users]


@router.patch("/api/admin/users/{user_id}", response_model=UserResponse)
def update_user(
    user_id: UUID,
    payload: UserUpdate,
    session: Annotated[Session, Depends(auth_session)],
    caller: Annotated[User, Depends(require_permissions("users:create"))],
) -> UserResponse:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    values = payload.model_dump(exclude_unset=True)
    if "roles" in values:
        roles = seed_roles(session)
        unknown_roles = set(values["roles"]) - roles.keys()
        if unknown_roles:
            raise HTTPException(status_code=422, detail="unknown role")
        if user.id == caller.id and "admin" not in values["roles"]:
            raise HTTPException(status_code=422, detail="cannot remove own admin role")
        user.roles = [roles[name] for name in values.pop("roles")]
    if "username" in values:
        conflict = session.scalar(select(User.id).where(User.username == values["username"], User.id != user.id))
        if conflict:
            raise HTTPException(status_code=409, detail="username already exists")
    if "password" in values:
        user.password_hash = hash_password(values.pop("password"))
    if user.id == caller.id and values.get("is_disabled"):
        raise HTTPException(status_code=422, detail="cannot disable own account")
    for key, value in values.items():
        setattr(user, key, value)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="username already exists") from None
    session.refresh(user)
    return _user_response(user)


@router.delete("/api/admin/users/{user_id}", status_code=204)
def disable_user(
    user_id: UUID,
    session: Annotated[Session, Depends(auth_session)],
    caller: Annotated[User, Depends(require_permissions("users:create"))],
) -> None:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    if user.id == caller.id:
        raise HTTPException(status_code=422, detail="cannot disable own account")
    user.is_disabled = True
    session.commit()
