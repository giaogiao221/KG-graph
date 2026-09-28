from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker
from uuid import UUID

from app.auth.models import User
from app.projects.models import Project, ProjectMembership


def test_non_member_cannot_read_project(
    client_factory, foreign_project: Project
) -> None:
    response = client_factory("viewer").get(
        f"/api/projects/{foreign_project.id}"
    )

    assert response.status_code == 403


def test_member_can_read_project(client_factory, project: Project) -> None:
    response = client_factory("operator").get(f"/api/projects/{project.id}")

    assert response.status_code == 200
    assert response.json()["name"] == "Member project"


def test_project_creator_becomes_owner(
    client_factory,
    app_session_factory: sessionmaker[Session],
    users: dict[str, User],
) -> None:
    response = client_factory("operator").post(
        "/api/projects",
        json={"name": "Created safely", "description": "Scope"},
    )

    assert response.status_code == 201
    with app_session_factory() as session:
        project = session.get(Project, UUID(response.json()["id"]))
        assert project is not None
        membership = project.memberships[0]
        assert membership.user_id == users["operator"].id
        assert membership.role == "owner"


def test_admin_can_read_foreign_project_without_membership(
    client_factory, foreign_project: Project
) -> None:
    response = client_factory("admin").get(f"/api/projects/{foreign_project.id}")

    assert response.status_code == 200


def test_admin_project_list_includes_projects_without_membership(
    client_factory, foreign_project: Project
) -> None:
    response = client_factory("admin").get("/api/projects")

    assert response.status_code == 200
    assert str(foreign_project.id) in {item["id"] for item in response.json()}


def test_admin_can_add_member_without_being_owner(
    client_factory, foreign_project: Project, users: dict[str, User]
) -> None:
    response = client_factory("admin").post(
        f"/api/projects/{foreign_project.id}/members",
        json={"user_id": str(users["viewer"].id), "role": "member"},
    )

    assert response.status_code == 201


def test_project_owner_can_add_member(
    client_factory, project: Project, users: dict[str, User]
) -> None:
    response = client_factory("operator").post(
        f"/api/projects/{project.id}/members",
        json={"user_id": str(users["viewer"].id), "role": "member"},
    )

    assert response.status_code == 201


def test_project_owner_does_not_need_global_edit_permission(
    client_factory,
    project: Project,
    users: dict[str, User],
    app_session_factory: sessionmaker[Session],
) -> None:
    with app_session_factory.begin() as session:
        session.add(
            ProjectMembership(
                project_id=project.id, user_id=users["viewer"].id, role="owner"
            )
        )

    response = client_factory("viewer").post(
        f"/api/projects/{project.id}/members",
        json={"user_id": str(users["outsider"].id), "role": "member"},
    )

    assert response.status_code == 201


def test_project_member_who_is_not_owner_cannot_add_member(
    client_factory, foreign_project: Project, users: dict[str, User]
) -> None:
    response = client_factory("operator").post(
        f"/api/projects/{foreign_project.id}/members",
        json={"user_id": str(users["viewer"].id), "role": "member"},
    )

    assert response.status_code == 403


def test_non_member_cannot_add_member(
    client_factory, project: Project, users: dict[str, User]
) -> None:
    response = client_factory("outsider").post(
        f"/api/projects/{project.id}/members",
        json={"user_id": str(users["viewer"].id), "role": "member"},
    )

    assert response.status_code == 403


def test_add_member_reports_missing_user(
    client_factory, project: Project
) -> None:
    response = client_factory("admin").post(
        f"/api/projects/{project.id}/members",
        json={"user_id": "00000000-0000-0000-0000-000000000001"},
    )

    assert response.status_code == 404


def test_add_member_reports_duplicate_membership(
    client_factory, project: Project, users: dict[str, User]
) -> None:
    response = client_factory("admin").post(
        f"/api/projects/{project.id}/members",
        json={"user_id": str(users["operator"].id)},
    )

    assert response.status_code == 409
