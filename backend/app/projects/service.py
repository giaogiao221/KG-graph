from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.projects.models import Project, ProjectMembership


def get_member_project(
    session: Session,
    project_id: UUID,
    user_id: UUID,
    *,
    admin: bool = False,
) -> Project:
    statement = select(Project).options(selectinload(Project.memberships)).where(
        Project.id == project_id
    )
    if not admin:
        statement = statement.join(ProjectMembership).where(
            ProjectMembership.user_id == user_id
        )
    project = session.scalar(statement)
    if project is None:
        # Do not reveal whether an inaccessible project exists.
        raise HTTPException(status_code=403, detail="project access denied")
    return project


def list_member_projects(
    session: Session, user_id: UUID, *, admin: bool = False,
    offset: int = 0, limit: int = 500,
) -> list[Project]:
    statement = select(Project)
    if not admin:
        statement = statement.join(ProjectMembership).where(
            ProjectMembership.user_id == user_id
        )
    return list(
        session.scalars(
            statement.order_by(Project.created_at, Project.id).offset(offset).limit(limit)
        )
    )


def create_project(
    session: Session, *, name: str, description: str | None, creator_id: UUID
) -> Project:
    project = Project(name=name, description=description)
    project.memberships.append(ProjectMembership(user_id=creator_id, role="owner"))
    session.add(project)
    session.commit()
    session.refresh(project)
    return project
