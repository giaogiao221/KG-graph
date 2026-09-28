from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth.dependencies import auth_session
from app.auth.models import User
from app.auth.security import create_access_token, hash_password
from app.auth.service import seed_roles
from app.core.database import Base
from app.core.settings import get_settings
from app.documents.storage import LocalStorage, get_storage
from app.main import create_app
from app.projects.models import Project, ProjectMembership


@pytest.fixture
def app_session_factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    monkeypatch.setenv(
        "EXTRACTION_JWT_SECRET",
        "task-four-test-secret-with-at-least-thirty-two-bytes",
    )
    get_settings.cache_clear()
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    get_settings.cache_clear()


@pytest.fixture
def users(app_session_factory: sessionmaker[Session]) -> dict[str, User]:
    with app_session_factory.begin() as session:
        roles = seed_roles(session)
        result = {
            key: User(
                username=f"{key}-task-four",
                password_hash=hash_password("StrongPass1!"),
                roles=[roles[role]],
            )
            for key, role in (
                ("admin", "admin"),
                ("operator", "operator"),
                ("outsider", "operator"),
                ("reviewer", "reviewer"),
                ("viewer", "viewer"),
            )
        }
        session.add_all(result.values())
        session.flush()
    return result


@pytest.fixture
def project(
    app_session_factory: sessionmaker[Session], users: dict[str, User]
) -> Project:
    with app_session_factory.begin() as session:
        value = Project(name="Member project", description="In scope")
        value.memberships.append(
            ProjectMembership(user_id=users["operator"].id, role="owner")
        )
        value.memberships.append(
            ProjectMembership(user_id=users["reviewer"].id, role="member")
        )
        session.add(value)
        session.flush()
    return value


@pytest.fixture
def foreign_project(
    app_session_factory: sessionmaker[Session], users: dict[str, User]
) -> Project:
    with app_session_factory.begin() as session:
        value = Project(name="Foreign project", description=None)
        value.memberships.append(
            ProjectMembership(user_id=users["operator"].id, role="member")
        )
        session.add(value)
        session.flush()
    return value


@pytest.fixture
def client_factory(
    app_session_factory: sessionmaker[Session],
    users: dict[str, User],
    tmp_path: Path,
) -> Iterator[Callable[[str], TestClient]]:
    app = create_app()

    def test_session() -> Iterator[Session]:
        with app_session_factory() as session:
            yield session

    storage = LocalStorage(tmp_path / "objects")
    app.dependency_overrides[auth_session] = test_session
    app.dependency_overrides[get_storage] = lambda: storage
    opened: list[TestClient] = []

    def build(role: str) -> TestClient:
        client = TestClient(
            app,
            headers={
                "Authorization": f"Bearer {create_access_token(users[role].id)}"
            },
        )
        client.__enter__()
        opened.append(client)
        return client

    build.app = app  # type: ignore[attr-defined]
    build.storage = storage  # type: ignore[attr-defined]

    yield build
    for client in opened:
        client.__exit__(None, None, None)
