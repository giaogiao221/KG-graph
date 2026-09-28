from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth.dependencies import auth_session
from app.auth.models import User
from app.auth.security import hash_password
from app.auth.service import seed_roles
from app.core.database import Base
from app.core.settings import get_settings
from app.main import create_app
from app.projects.models import Project, ProjectMembership


@pytest.fixture
def session_factory(monkeypatch: pytest.MonkeyPatch) -> sessionmaker[Session]:
    monkeypatch.setenv(
        "EXTRACTION_JWT_SECRET",
        "fixed-test-secret-with-at-least-thirty-two-bytes",
    )
    get_settings.cache_clear()
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    get_settings.cache_clear()


@pytest.fixture
def admin_user(session_factory: sessionmaker[Session]) -> User:
    with session_factory.begin() as session:
        roles = seed_roles(session)
        user = User(
            username="admin",
            password_hash=hash_password("StrongPass1!"),
            roles=[roles["admin"]],
        )
        session.add(user)
    return user


@pytest.fixture
def client(
    session_factory: sessionmaker[Session], admin_user: User
) -> Iterator[TestClient]:
    app = create_app()

    def test_session() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[auth_session] = test_session
    with TestClient(app) as test_client:
        yield test_client


def test_password_is_hashed_and_login_returns_token(
    client: TestClient, admin_user: User
) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "StrongPass1!"},
    )

    assert response.status_code == 200
    assert response.json()["access_token"]
    assert response.json()["token_type"] == "bearer"
    assert admin_user.password_hash != "StrongPass1!"
    assert admin_user.password_hash.startswith("$argon2")


def test_login_rejects_disabled_user(
    client: TestClient,
    admin_user: User,
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory.begin() as session:
        user = session.get(User, admin_user.id)
        assert user is not None
        user.is_disabled = True

    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "StrongPass1!"},
    )

    assert response.status_code == 401


def test_user_response_never_contains_password_hash(
    client: TestClient, admin_user: User
) -> None:
    login = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "StrongPass1!"},
    )
    token = login.json()["access_token"]

    response = client.post(
        "/api/admin/users",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "username": "new-user",
            "password": "AnotherStrong1!",
            "roles": ["viewer"],
        },
    )

    assert response.status_code == 201
    assert "password" not in response.json()
    assert "password_hash" not in response.json()


def test_admin_can_grant_new_user_project_access_when_creating_account(
    client: TestClient, admin_user: User, session_factory: sessionmaker[Session]
) -> None:
    with session_factory.begin() as session:
        project = Project(name="Assigned project", description=None)
        project.memberships.append(ProjectMembership(user_id=admin_user.id, role="owner"))
        session.add(project)
        session.flush()
        project_id = project.id

    token = client.post(
        "/api/auth/login", json={"username": "admin", "password": "StrongPass1!"}
    ).json()["access_token"]
    response = client.post(
        "/api/admin/users",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "username": "project-viewer",
            "password": "AnotherStrong1!",
            "roles": ["viewer"],
            "project_ids": [str(project_id)],
        },
    )
    assert response.status_code == 201

    with session_factory() as session:
        membership = session.scalar(
            select(ProjectMembership).where(
                ProjectMembership.project_id == project_id,
                    ProjectMembership.user_id == UUID(response.json()["id"]),
            )
        )
        assert membership is not None
        assert membership.role == "member"


def test_admin_must_supply_initial_password(
    client: TestClient, admin_user: User
) -> None:
    login = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "StrongPass1!"},
    )

    response = client.post(
        "/api/admin/users",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
        json={"username": "new-user", "roles": ["viewer"]},
    )

    assert response.status_code == 422


def test_access_token_expires_in_about_thirty_minutes(
    client: TestClient, admin_user: User
) -> None:
    before = datetime.now(UTC)
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "StrongPass1!"},
    )
    after = datetime.now(UTC)

    payload = jwt.decode(
        response.json()["access_token"],
        get_settings().jwt_secret.get_secret_value(),
        algorithms=["HS256"],
    )
    expires_at = datetime.fromtimestamp(payload["exp"], UTC)

    assert before + timedelta(minutes=29, seconds=55) <= expires_at
    assert expires_at <= after + timedelta(minutes=30, seconds=5)


def test_rejects_expired_token(
    client: TestClient,
    admin_user: User,
) -> None:
    expired = jwt.encode(
        {"sub": str(admin_user.id), "exp": datetime.now(UTC) - timedelta(seconds=1)},
        get_settings().jwt_secret.get_secret_value(),
        algorithm="HS256",
    )

    response = client.post(
        "/api/admin/users",
        headers={"Authorization": f"Bearer {expired}"},
        json={
            "username": "new-user",
            "password": "AnotherStrong1!",
            "roles": ["viewer"],
        },
    )

    assert response.status_code == 401


def test_rejects_wrong_algorithm_token(
    client: TestClient,
    admin_user: User,
) -> None:
    wrong_algorithm = jwt.encode(
        {"sub": str(admin_user.id), "exp": datetime.now(UTC) + timedelta(minutes=30)},
        get_settings().jwt_secret.get_secret_value(),
        algorithm="HS384",
    )

    response = client.post(
        "/api/admin/users",
        headers={"Authorization": f"Bearer {wrong_algorithm}"},
        json={
            "username": "new-user",
            "password": "AnotherStrong1!",
            "roles": ["viewer"],
        },
    )

    assert response.status_code == 401


def test_me_returns_current_user_roles_and_permissions_without_secrets(
    client: TestClient,
    admin_user: User,
) -> None:
    login = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "StrongPass1!"},
    )

    response = client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "id": str(admin_user.id),
        "username": "admin",
        "roles": ["admin"],
        "permissions": [
            "exports:create",
            "exports:unreviewed",
            "projects:members",
            "records:create",
            "records:edit",
            "records:review",
            "records:view",
            "users:create",
        ],
    }
    body = response.text.lower()
    assert "password" not in body
    assert "hash" not in body


def test_me_requires_a_valid_access_token(client: TestClient) -> None:
    response = client.get("/api/auth/me")

    assert response.status_code == 401
