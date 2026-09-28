from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth.bootstrap import BootstrapError, bootstrap_admin
from app.auth.service import authenticate_user
from app.core.audit import AuditEvent
from app.core.database import Base


@pytest.fixture
def session_factory() -> sessionmaker:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_bootstrap_creates_admin_with_hashed_password(session_factory):
    with session_factory.begin() as session:
        user, created = bootstrap_admin(
            session,
            username="root-admin",
            password="long-enough-secret",
            request_id="test-bootstrap-create",
        )
        assert created is True
        assert user.password_hash != "long-enough-secret"
        assert {role.name for role in user.roles} == {"admin"}
    with session_factory() as session:
        authenticated = authenticate_user(session, "root-admin", "long-enough-secret")
        assert authenticated is not None
        assert authenticated.username == "root-admin"


def test_bootstrap_is_idempotent_and_never_duplicates(session_factory):
    with session_factory.begin() as session:
        bootstrap_admin(
            session,
            username="root-admin",
            password="long-enough-secret",
            request_id="test-bootstrap-first",
        )
    with session_factory.begin() as session:
        with pytest.raises(BootstrapError):
            bootstrap_admin(
                session,
                username="root-admin",
                password="another-long-secret",
                request_id="test-bootstrap-second",
            )
    with session_factory() as session:
        assert authenticate_user(session, "root-admin", "long-enough-secret")
        assert authenticate_user(session, "root-admin", "another-long-secret") is None


def test_bootstrap_reset_password_rotates_credentials(session_factory):
    with session_factory.begin() as session:
        bootstrap_admin(
            session,
            username="root-admin",
            password="long-enough-secret",
            request_id="test-bootstrap-rotate-a",
        )
    with session_factory.begin() as session:
        user, created = bootstrap_admin(
            session,
            username="root-admin",
            password="rotated-long-secret",
            request_id="test-bootstrap-rotate-b",
            reset_password=True,
        )
        assert created is False
    with session_factory() as session:
        assert authenticate_user(session, "root-admin", "long-enough-secret") is None
        assert authenticate_user(session, "root-admin", "rotated-long-secret")


def test_bootstrap_rejects_weak_credentials(session_factory):
    with session_factory.begin() as session:
        with pytest.raises(BootstrapError):
            bootstrap_admin(
                session,
                username="root-admin",
                password="short",
                request_id="test-bootstrap-weak-password",
            )
        with pytest.raises(BootstrapError):
            bootstrap_admin(
                session,
                username="bad name!",
                password="long-enough-secret",
                request_id="test-bootstrap-weak-username",
            )


def test_bootstrap_records_append_only_audit(session_factory):
    with session_factory.begin() as session:
        user, _ = bootstrap_admin(
            session,
            username="root-admin",
            password="long-enough-secret",
            request_id="test-bootstrap-audit",
        )
        events = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "admin.bootstrap")
        ).all()
        assert len(events) == 1
        assert events[0].actor_id == user.id
        assert events[0].target_id == user.id
        assert events[0].outcome == "success"
