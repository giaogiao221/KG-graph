from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import auth_session, require_permissions
from app.auth.models import User
from app.profiles.models import ExtractionProfile, ProfileVersion
from app.profiles.schemas import (
    ProfileCreate,
    ProfileSummaryResponse,
    ProfileVersionCreate,
    ProfileVersionResponse,
)
from app.profiles.service import (
    ProfileModelError,
    create_profile,
    create_profile_version,
    get_project_profile,
)
from app.projects.service import get_member_project


router = APIRouter(prefix="/api/projects/{project_id}/profiles", tags=["profiles"])


def _is_admin(user: User) -> bool:
    return any(role.name == "admin" for role in user.roles)


def _response(version: ProfileVersion) -> ProfileVersionResponse:
    return ProfileVersionResponse.model_validate(
        {
            "id": version.id,
            "profile_id": version.profile_id,
            "version_number": version.version_number,
            "snapshot": version.snapshot_json,
            "snapshot_sha256": version.snapshot_sha256,
            "created_by_id": version.created_by_id,
            "created_at": version.created_at,
        }
    )


@router.get("", response_model=list[ProfileSummaryResponse])
def list_profiles_route(
    project_id: UUID,
    session: Annotated[Session, Depends(auth_session)],
    user: Annotated[User, Depends(require_permissions("records:view"))],
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 500,
) -> list[ProfileSummaryResponse]:
    get_member_project(session, project_id, user.id, admin=_is_admin(user))
    profiles = session.scalars(
        select(ExtractionProfile)
        .where(ExtractionProfile.project_id == project_id)
        .order_by(ExtractionProfile.created_at, ExtractionProfile.id)
        .offset(offset).limit(limit)
    )
    return [
        ProfileSummaryResponse.model_validate(
            {
                "id": profile.id,
                "project_id": profile.project_id,
                "name": profile.name,
                "next_version_number": profile.next_version_number,
                "created_at": profile.created_at,
                "latest_version": _response(profile.versions[-1]),
            }
        )
        for profile in profiles
    ]


@router.post("", response_model=ProfileVersionResponse, status_code=201)
def create_profile_route(
    project_id: UUID,
    payload: ProfileCreate,
    session: Annotated[Session, Depends(auth_session)],
    user: Annotated[User, Depends(require_permissions("records:create"))],
) -> ProfileVersionResponse:
    get_member_project(session, project_id, user.id, admin=_is_admin(user))
    try:
        value = create_profile(
            session,
            project_id=project_id,
            name=payload.name,
            configuration=ProfileVersionCreate.model_validate(
                payload.model_dump(exclude={"name"})
            ),
            created_by_id=user.id,
        )
    except ProfileModelError as error:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(error)) from None
    return _response(value)


@router.post(
    "/{profile_id}/versions", response_model=ProfileVersionResponse, status_code=201
)
def create_profile_version_route(
    project_id: UUID,
    profile_id: UUID,
    payload: ProfileVersionCreate,
    session: Annotated[Session, Depends(auth_session)],
    user: Annotated[User, Depends(require_permissions("records:edit"))],
) -> ProfileVersionResponse:
    get_member_project(session, project_id, user.id, admin=_is_admin(user))
    profile = get_project_profile(session, project_id=project_id, profile_id=profile_id)
    try:
        value = create_profile_version(
            session,
            profile=profile,
            configuration=payload,
            created_by_id=user.id,
        )
    except ProfileModelError as error:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(error)) from None
    return _response(value)


@router.get(
    "/{profile_id}/versions", response_model=list[ProfileVersionResponse]
)
def list_profile_versions_route(
    project_id: UUID,
    profile_id: UUID,
    session: Annotated[Session, Depends(auth_session)],
    user: Annotated[User, Depends(require_permissions("records:view"))],
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 500,
) -> list[ProfileVersionResponse]:
    get_member_project(session, project_id, user.id, admin=_is_admin(user))
    profile = get_project_profile(session, project_id=project_id, profile_id=profile_id)
    versions = session.scalars(
        select(ProfileVersion)
        .where(ProfileVersion.profile_id == profile.id)
        .order_by(ProfileVersion.version_number)
        .offset(offset).limit(limit)
    )
    return [_response(version) for version in versions]
