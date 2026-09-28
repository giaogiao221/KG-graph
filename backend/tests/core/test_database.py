from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from uuid import UUID

import pytest
from sqlalchemy import String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.core import database
from app.core.database import Base, get_session, session_scope


class ProbeBase(DeclarativeBase):
    pass


class Probe(ProbeBase):
    __tablename__ = "probes"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))


class CommonProbe(Base):
    __tablename__ = "common_probes"


class TrackingSession(Session):
    closed = False

    def close(self) -> None:
        self.closed = True
        super().close()


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite://")
    ProbeBase.metadata.create_all(engine)
    return sessionmaker(bind=engine, class_=TrackingSession)


def test_session_scope_rolls_back_on_error(session_factory):
    with pytest.raises(RuntimeError):
        with session_scope(session_factory) as session:
            session.add(Probe(name="not-saved"))
            session.flush()
            raise RuntimeError("boom")

    with session_factory() as session:
        assert session.query(Probe).count() == 0


def test_session_scope_commits_and_closes(session_factory):
    with session_scope(session_factory) as session:
        session.add(Probe(name="saved"))

    assert session.closed
    with session_factory() as verification_session:
        assert verification_session.query(Probe).count() == 1


def test_get_session_closes_the_session(session_factory):
    dependency = get_session(session_factory)
    session = next(dependency)

    dependency.close()

    assert session.closed


def test_common_model_declares_shared_fields():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        model = CommonProbe()
        session.add(model)
        session.flush()

    fields = CommonProbe.__table__.columns

    assert isinstance(model.id, UUID)
    assert fields.id.primary_key
    assert fields.created_at.nullable is False
    assert fields.updated_at.nullable is False
    assert fields.version.nullable is False
    assert model.version == 1


def test_database_url_is_required(monkeypatch):
    monkeypatch.delenv("EXTRACTION_DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="EXTRACTION_DATABASE_URL is required"):
        database.get_database_url()


def test_alembic_offline_accepts_percent_encoded_url():
    backend_root = Path(__file__).parents[2]
    environment = os.environ.copy()
    database_url = "postgresql+psycopg://explicit:p%40ss@localhost/example"
    environment["EXTRACTION_DATABASE_URL"] = database_url

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=backend_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert database_url not in result.stderr
    assert "CREATE UNIQUE INDEX uq_review_task_active_root" in result.stdout
    assert "UNIQUE (username)" in result.stdout
