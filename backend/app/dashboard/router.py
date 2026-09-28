"""Dashboard statistics API."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import auth_session, require_permissions
from app.auth.models import User
from app.dashboard.schemas import DashboardStatsResponse
from app.dashboard.service import aggregate_dashboard
from app.projects.models import ProjectMembership

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/stats", response_model=DashboardStatsResponse)
def dashboard_stats(
    session: Annotated[Session, Depends(auth_session)],
    user: Annotated[User, Depends(require_permissions("records:view"))],
    project_id: UUID | None = None,
) -> DashboardStatsResponse:
    is_admin = any(role.name == "admin" for role in user.roles)
    if is_admin:
        project_ids: list[UUID] | None = None
    else:
        project_ids = list(
            session.scalars(
                select(ProjectMembership.project_id).where(
                    ProjectMembership.user_id == user.id
                )
            )
        )

    if project_id is not None:
        if not is_admin and project_id not in set(project_ids or []):
            raise HTTPException(status_code=403, detail="project access denied")
        project_ids = [project_id]

    result = aggregate_dashboard(session, project_ids=project_ids)
    return DashboardStatsResponse.model_validate(result)
