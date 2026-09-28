"""Task 15 acceptance: PostgreSQL-only constraint enforcement.

These tests require a real PostgreSQL server. Configure
``EXTRACTION_TEST_POSTGRES_DSN`` (for example
``postgresql+psycopg://user:password@host:5432/extraction_constraints_test``)
to enable them; they are skipped otherwise. The target database is created
when missing and migrated to Alembic head once per test session. Every test
rolls back its own writes, so the database stays clean between runs.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

BACKEND_ROOT = Path(__file__).resolve().parents[2]
_DATABASE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")


def _dsn() -> str | None:
    value = os.getenv("EXTRACTION_TEST_POSTGRES_DSN", "").strip()
    return value or None


pytestmark = pytest.mark.skipif(
    _dsn() is None, reason="EXTRACTION_TEST_POSTGRES_DSN is not configured"
)


def _ensure_database(dsn: str) -> None:
    url = sa.make_url(dsn)
    database = url.database or ""
    if not _DATABASE_NAME.fullmatch(database):
        raise RuntimeError("EXTRACTION_TEST_POSTGRES_DSN must name a simple database")
    maintenance = sa.create_engine(
        url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    with maintenance.connect() as connection:
        exists = connection.execute(
            sa.text("SELECT 1 FROM pg_database WHERE datname = :name"),
            {"name": database},
        ).scalar()
        if not exists:
            connection.execute(sa.text(f'CREATE DATABASE "{database}"'))
    maintenance.dispose()


@pytest.fixture(scope="session")
def postgres_session_factory() -> Iterator[sessionmaker[Session]]:
    dsn = _dsn()
    assert dsn is not None
    _ensure_database(dsn)
    env = os.environ.copy()
    env["EXTRACTION_DATABASE_URL"] = dsn
    upgrade = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert upgrade.returncode == 0, upgrade.stderr
    yield sessionmaker(bind=sa.create_engine(dsn), expire_on_commit=False)


@pytest.fixture
def postgres_session(
    postgres_session_factory: sessionmaker[Session],
) -> Iterator[Session]:
    session = postgres_session_factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


class _Graph:
    def __init__(self, session: Session) -> None:
        from app.auth.models import User
        from app.auth.service import seed_roles
        from app.documents.models import Document, DocumentVersion
        from app.jobs.models import DocumentJob, ExtractionBatch, JobStep
        from app.profiles.models import ExtractionProfile, ProfileVersion
        from app.projects.models import Project

        roles = seed_roles(session)
        self.user = User(username=f"pg-constraint-{os.getpid()}", password_hash="x")
        self.user.roles.append(roles["admin"])
        self.project = Project(name="Constraint project", description=None)
        session.add_all((self.user, self.project))
        session.flush()

        self.document = Document(project_id=self.project.id, created_by_id=self.user.id)
        session.add(self.document)
        session.flush()
        self.version = DocumentVersion(
            document_id=self.document.id,
            uploader_id=self.user.id,
            version_number=1,
            original_filename="constraints.md",
            storage_key=f"{os.getpid()}-constraints",
            sha256=hashlib.sha256(b"# Constraints").hexdigest(),
            size_bytes=14,
            mime_type="text/markdown",
            is_extractable=True,
        )
        session.add(self.version)
        self.profile = ExtractionProfile(project_id=self.project.id, name="constraints")
        session.add(self.profile)
        session.flush()
        self.profile_version = ProfileVersion(
            profile_id=self.profile.id,
            created_by_id=self.user.id,
            snapshot_json={"preset": "hybrid"},
            snapshot_sha256=hashlib.sha256(b"snapshot").hexdigest(),
        )
        session.add(self.profile_version)
        session.flush()
        self.batch = ExtractionBatch(
            project_id=self.project.id,
            profile_version_id=self.profile_version.id,
            created_by_id=self.user.id,
            request_key=f"constraints-{os.getpid()}",
        )
        session.add(self.batch)
        session.flush()
        self.job = DocumentJob(
            batch_id=self.batch.id, document_version_id=self.version.id
        )
        session.add(self.job)
        session.flush()
        self.merge_step = JobStep(
            document_job_id=self.job.id,
            kind="merge",
            idempotency_key=hashlib.sha256(b"merge").hexdigest(),
            position=4,
            stage=1,
        )
        self.validate_step = JobStep(
            document_job_id=self.job.id,
            kind="validate",
            idempotency_key=hashlib.sha256(b"validate").hexdigest(),
            position=5,
            stage=2,
        )
        session.add_all((self.merge_step, self.validate_step))
        session.flush()


def _insert_raw_fact(session: Session, graph: _Graph, graph_fact_key: str) -> UUID:
    from app.facts.models import RawFact

    canonical = " ".join(graph_fact_key.split()).casefold()
    fact = RawFact(
        project_id=graph.project.id,
        document_id=graph.document.id,
        document_version_id=graph.version.id,
        document_job_id=graph.job.id,
        import_step_id=graph.validate_step.id,
        source_step_id=graph.merge_step.id,
        graph_fact_key=graph_fact_key,
        dedup_key="g:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        subject="约束主体",
        property="约束属性",
        value="1",
        unit="",
        condition="",
        source_type="text",
        confidence=None,
        review_status="candidate",
        evidence_text="约束证据",
        evidence_hash=hashlib.sha256("约束证据".encode("utf-8")).hexdigest(),
        extraction_source="rule_text",
        route="rule_text",
        row_json={"graph_fact_key": graph_fact_key},
    )
    session.add(fact)
    session.flush()
    return fact.id


def test_fact_graph_key_is_unique_per_document_job(
    postgres_session: Session,
) -> None:
    graph = _Graph(postgres_session)
    _insert_raw_fact(postgres_session, graph, "same")
    with pytest.raises(sa.exc.IntegrityError):
        _insert_raw_fact(postgres_session, graph, "same")


def test_raw_facts_are_immutable_on_postgres(postgres_session: Session) -> None:
    graph = _Graph(postgres_session)
    fact_id = _insert_raw_fact(postgres_session, graph, "immutable")
    with pytest.raises(sa.exc.DatabaseError):
        postgres_session.execute(
            sa.text("UPDATE raw_facts SET subject = 'changed' WHERE id = :id"),
            {"id": fact_id},
        )


def test_document_versions_are_immutable_on_postgres(
    postgres_session: Session,
) -> None:
    graph = _Graph(postgres_session)
    with pytest.raises(sa.exc.DatabaseError):
        postgres_session.execute(
            sa.text(
                "UPDATE document_versions SET mime_type = 'text/plain' WHERE id = :id"
            ),
            {"id": graph.version.id},
        )
