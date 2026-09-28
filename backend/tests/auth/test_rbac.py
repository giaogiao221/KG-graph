from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.auth.models import Role, User
from app.auth.security import hash_password
from app.auth.service import seed_roles

from .test_auth import admin_user, client, session_factory


def test_role_seed_is_idempotent(session_factory: sessionmaker[Session]) -> None:
    with session_factory.begin() as session:
        first = seed_roles(session)
        second = seed_roles(session)

        assert set(first) == {"admin", "operator", "reviewer", "viewer"}
        assert {name: role.id for name, role in first.items()} == {
            name: role.id for name, role in second.items()
        }
        assert session.query(Role).count() == 4


def test_viewer_cannot_create_user(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    with session_factory.begin() as session:
        roles = seed_roles(session)
        session.add(
            User(
                username="viewer",
                password_hash=hash_password("ViewerPass1!"),
                roles=[roles["viewer"]],
            )
        )

    login = client.post(
        "/api/auth/login",
        json={"username": "viewer", "password": "ViewerPass1!"},
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

    assert response.status_code == 403


def test_disabled_user_token_is_rejected(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    with session_factory.begin() as session:
        roles = seed_roles(session)
        user = User(
            username="disabled-viewer",
            password_hash=hash_password("ViewerPass1!"),
            roles=[roles["viewer"]],
        )
        session.add(user)

    login = client.post(
        "/api/auth/login",
        json={"username": "disabled-viewer", "password": "ViewerPass1!"},
    )
    token = login.json()["access_token"]

    with session_factory.begin() as session:
        stored_user = session.get(User, user.id)
        assert stored_user is not None
        stored_user.is_disabled = True

    response = client.post(
        "/api/admin/users",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "username": "new-user",
            "password": "AnotherStrong1!",
            "roles": ["viewer"],
        },
    )

    assert response.status_code == 401
