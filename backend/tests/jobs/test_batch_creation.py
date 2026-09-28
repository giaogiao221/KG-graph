from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from app.documents.models import Document, DocumentVersion
from app.jobs.models import DocumentJob, ExtractionBatch, JobStep
from app.jobs import service as job_service
from app.jobs.service import BatchValidationError, create_batch, step_idempotency_key
from app.profiles.models import ExtractionProfile, ProfileVersion


RULE_VERSION = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PLUGIN_VERSION = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
PROMPT_VERSION = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def _profile_version(
    session: Session,
    *,
    project_id: UUID,
    creator_id: UUID,
    routes: dict[str, bool],
) -> ProfileVersion:
    profile = ExtractionProfile(project_id=project_id, name="batch-profile")
    version = ProfileVersion(
        profile=profile,
        created_by_id=creator_id,
        version_number=1,
        snapshot_json={
            "preset": "custom",
            "routes": routes,
            "rule_engine": "builtin",
            "rule_version": RULE_VERSION,
            "plugin_version": PLUGIN_VERSION,
            "prompt_version": PROMPT_VERSION,
        },
        snapshot_sha256="b" * 64,
    )
    session.add(version)
    session.flush()
    return version


def _documents(
    session: Session,
    *,
    project_id: UUID,
    creator_id: UUID,
    count: int = 2,
    is_extractable: bool = True,
) -> list[DocumentVersion]:
    versions = []
    for number in range(count):
        document = Document(project_id=project_id, created_by_id=creator_id)
        version = DocumentVersion(
            document=document,
            uploader_id=creator_id,
            version_number=1,
            original_filename=f"{number}.md",
            storage_key=f"{number:064x}"[-64:],
            sha256=hashlib.sha256(str(number).encode()).hexdigest(),
            size_bytes=1,
            mime_type="text/markdown",
            is_extractable=is_extractable,
        )
        session.add(version)
        versions.append(version)
    session.flush()
    return versions


def test_batch_expands_selected_routes_for_every_document(
    app_session_factory: sessionmaker[Session], project, users
) -> None:
    with app_session_factory() as session:
        profile = _profile_version(
            session,
            project_id=project.id,
            creator_id=users["operator"].id,
            routes={
                "text_rule": True,
                "text_llm": True,
                "table_rule": True,
                "table_llm": True,
            },
        )
        documents = _documents(
            session,
            project_id=project.id,
            creator_id=users["operator"].id,
        )
        batch = create_batch(session, profile.id, [item.id for item in documents])

        assert len(batch.document_jobs) == 2
        for document_job in batch.document_jobs:
            assert {step.kind for step in document_job.steps} == {
                "rule_text",
                "llm_text",
                "rule_table",
                "llm_table",
                "merge",
                "validate",
            }
            assert len({step.idempotency_key for step in document_job.steps}) == 6


def test_batch_only_expands_enabled_generators_plus_required_postprocessing(
    app_session_factory: sessionmaker[Session], project, users
) -> None:
    with app_session_factory() as session:
        profile = _profile_version(
            session,
            project_id=project.id,
            creator_id=users["operator"].id,
            routes={
                "text_rule": False,
                "text_llm": False,
                "table_rule": False,
                "table_llm": True,
            },
        )
        document = _documents(
            session,
            project_id=project.id,
            creator_id=users["operator"].id,
            count=1,
        )[0]

        batch = create_batch(session, profile.id, [document.id])

        assert [step.kind for step in batch.document_jobs[0].steps] == [
            "llm_table",
            "merge",
            "validate",
        ]


def test_steps_persist_generator_merge_validate_schedule(
    app_session_factory: sessionmaker[Session], project, users
) -> None:
    with app_session_factory() as session:
        profile = _profile_version(
            session,
            project_id=project.id,
            creator_id=users["operator"].id,
            routes={"text_rule": True, "table_llm": True},
        )
        document = _documents(
            session,
            project_id=project.id,
            creator_id=users["operator"].id,
            count=1,
        )[0]
        batch = create_batch(session, profile.id, [document.id])
        batch_id = batch.id
        session.commit()
        session.expire_all()

        persisted = session.get(ExtractionBatch, batch_id)
        assert persisted is not None
        steps = persisted.document_jobs[0].steps
        assert [(step.kind, step.position, step.stage) for step in steps] == [
            ("rule_text", 0, 0),
            ("llm_table", 1, 0),
            ("merge", 2, 1),
            ("validate", 3, 2),
        ]


def test_database_rejects_kind_stage_mismatch(
    app_session_factory: sessionmaker[Session], project, users
) -> None:
    with app_session_factory() as session:
        profile = _profile_version(
            session,
            project_id=project.id,
            creator_id=users["operator"].id,
            routes={"text_rule": True},
        )
        document = _documents(
            session,
            project_id=project.id,
            creator_id=users["operator"].id,
            count=1,
        )[0]
        batch = create_batch(session, profile.id, [document.id])
        merge = next(
            step for step in batch.document_jobs[0].steps if step.kind == "merge"
        )
        session.commit()

        with pytest.raises(DBAPIError):
            session.execute(
                update(JobStep)
                .where(JobStep.id == merge.id)
                .values(stage=0)
            )


def test_step_idempotency_key_is_stable_and_uses_every_required_factor() -> None:
    factors = {
        "document_sha256": "1" * 64,
        "profile_snapshot_sha256": "2" * 64,
        "adapter_version": "adapter-v1",
        "step_kind": "rule_text",
    }
    original = step_idempotency_key(**factors)

    assert original == step_idempotency_key(**factors)
    assert len(original) == 64
    for name, replacement in (
        ("document_sha256", "3" * 64),
        ("profile_snapshot_sha256", "4" * 64),
        ("adapter_version", "adapter-v2"),
        ("step_kind", "rule_table"),
    ):
        changed = {**factors, name: replacement}
        assert step_idempotency_key(**changed) != original


def test_repeated_batch_request_reuses_the_existing_batch(
    app_session_factory: sessionmaker[Session], project, users
) -> None:
    with app_session_factory() as session:
        profile = _profile_version(
            session,
            project_id=project.id,
            creator_id=users["operator"].id,
            routes={"text_rule": True},
        )
        documents = _documents(
            session, project_id=project.id, creator_id=users["operator"].id
        )

        first = create_batch(session, profile.id, [item.id for item in documents])
        session.commit()
        repeated = create_batch(
            session, profile.id, [item.id for item in reversed(documents)]
        )

        assert repeated.id == first.id
        assert session.scalar(select(func.count()).select_from(ExtractionBatch)) == 1


def test_request_key_includes_project_profile_and_canonical_documents() -> None:
    project_id = uuid4()
    profile_id = uuid4()
    first, second = uuid4(), uuid4()
    original = job_service.batch_request_key(
        project_id, profile_id, [first, second]
    )

    assert original == job_service.batch_request_key(
        project_id, profile_id, [second, first]
    )
    assert original != job_service.batch_request_key(
        uuid4(), profile_id, [first, second]
    )
    assert original != job_service.batch_request_key(
        project_id, uuid4(), [first, second]
    )
    assert original != job_service.batch_request_key(project_id, profile_id, [first])


def test_same_content_in_distinct_document_versions_has_distinct_batches(
    app_session_factory: sessionmaker[Session], project, users
) -> None:
    with app_session_factory() as session:
        profile = _profile_version(
            session,
            project_id=project.id,
            creator_id=users["operator"].id,
            routes={"text_rule": True},
        )
        first = _documents(
            session,
            project_id=project.id,
            creator_id=users["operator"].id,
            count=1,
        )[0]
        second = DocumentVersion(
            document_id=first.document_id,
            uploader_id=users["operator"].id,
            version_number=2,
            original_filename="same-v2.md",
            storage_key="f" * 64,
            sha256=first.sha256,
            size_bytes=first.size_bytes,
            mime_type=first.mime_type,
            is_extractable=True,
        )
        session.add(second)
        session.flush()

        first_batch = create_batch(session, profile.id, [first.id])
        second_batch = create_batch(session, profile.id, [second.id])

        assert first_batch.id != second_batch.id
        assert (
            first_batch.document_jobs[0].steps[0].idempotency_key
            == second_batch.document_jobs[0].steps[0].idempotency_key
        )


def test_same_content_in_distinct_projects_does_not_collide(
    app_session_factory: sessionmaker[Session], project, foreign_project, users
) -> None:
    with app_session_factory() as session:
        first_profile = _profile_version(
            session,
            project_id=project.id,
            creator_id=users["operator"].id,
            routes={"text_rule": True},
        )
        second_profile = _profile_version(
            session,
            project_id=foreign_project.id,
            creator_id=users["operator"].id,
            routes={"text_rule": True},
        )
        first_document = _documents(
            session,
            project_id=project.id,
            creator_id=users["operator"].id,
            count=1,
        )[0]
        foreign_parent = Document(
            project_id=foreign_project.id, created_by_id=users["operator"].id
        )
        second_document = DocumentVersion(
            document=foreign_parent,
            uploader_id=users["operator"].id,
            version_number=1,
            original_filename="same.md",
            storage_key="e" * 64,
            sha256=first_document.sha256,
            size_bytes=first_document.size_bytes,
            mime_type=first_document.mime_type,
            is_extractable=True,
        )
        session.add(second_document)
        session.flush()

        first_batch = create_batch(session, first_profile.id, [first_document.id])
        second_batch = create_batch(session, second_profile.id, [second_document.id])

        assert first_batch.id != second_batch.id
        assert first_batch.project_id != second_batch.project_id


def test_concurrent_unique_conflict_returns_the_winning_batch(
    app_session_factory: sessionmaker[Session], project, users
) -> None:
    with app_session_factory.begin() as setup:
        profile = _profile_version(
            setup,
            project_id=project.id,
            creator_id=users["operator"].id,
            routes={"text_rule": True},
        )
        document = _documents(
            setup,
            project_id=project.id,
            creator_id=users["operator"].id,
            count=1,
        )[0]
        profile_id, document_id = profile.id, document.id

    winner_session = app_session_factory()
    loser_session = app_session_factory()
    try:
        request_key = job_service.batch_request_key(
            project.id, profile_id, [document_id]
        )
        winner = ExtractionBatch(
            project_id=project.id,
            profile_version_id=profile_id,
            request_key=request_key,
        )
        winner_session.add(winner)

        def commit_winner(*_args: object) -> None:
            winner_session.commit()

        event.listen(loser_session, "before_flush", commit_winner, once=True)
        returned = create_batch(loser_session, profile_id, [document_id])

        assert returned.id == winner.id
    finally:
        winner_session.close()
        loser_session.close()


def test_batch_rejects_document_from_another_project(
    app_session_factory: sessionmaker[Session], project, foreign_project, users
) -> None:
    with app_session_factory() as session:
        profile = _profile_version(
            session,
            project_id=project.id,
            creator_id=users["operator"].id,
            routes={"text_rule": True},
        )
        foreign_document = _documents(
            session,
            project_id=foreign_project.id,
            creator_id=users["operator"].id,
            count=1,
        )[0]

        with pytest.raises(BatchValidationError):
            create_batch(session, profile.id, [foreign_document.id])

        assert session.scalar(select(ExtractionBatch)) is None


def test_batch_rejects_non_extractable_document(
    app_session_factory: sessionmaker[Session], project, users
) -> None:
    with app_session_factory() as session:
        profile = _profile_version(
            session,
            project_id=project.id,
            creator_id=users["operator"].id,
            routes={"text_rule": True},
        )
        document = _documents(
            session,
            project_id=project.id,
            creator_id=users["operator"].id,
            count=1,
            is_extractable=False,
        )[0]

        with pytest.raises(BatchValidationError, match="not extractable"):
            create_batch(session, profile.id, [document.id])

        assert session.scalar(select(ExtractionBatch)) is None


def test_sqlite_trigger_rejects_batch_profile_project_mismatch(
    app_session_factory: sessionmaker[Session], project, foreign_project, users
) -> None:
    with app_session_factory() as session:
        foreign_profile = _profile_version(
            session,
            project_id=foreign_project.id,
            creator_id=users["operator"].id,
            routes={"text_rule": True},
        )
        session.commit()

        with pytest.raises(DBAPIError, match="batch profile project mismatch"):
            with session.begin_nested():
                session.add(
                    ExtractionBatch(
                        project_id=project.id,
                        profile_version_id=foreign_profile.id,
                        request_key="c" * 64,
                    )
                )
                session.flush()


def test_sqlite_trigger_rejects_job_document_project_mismatch(
    app_session_factory: sessionmaker[Session], project, foreign_project, users
) -> None:
    with app_session_factory() as session:
        profile = _profile_version(
            session,
            project_id=project.id,
            creator_id=users["operator"].id,
            routes={"text_rule": True},
        )
        local_document = _documents(
            session,
            project_id=project.id,
            creator_id=users["operator"].id,
            count=1,
        )[0]
        foreign_document = DocumentVersion(
            document=Document(
                project_id=foreign_project.id,
                created_by_id=users["operator"].id,
            ),
            uploader_id=users["operator"].id,
            version_number=1,
            original_filename="foreign.md",
            storage_key="d" * 64,
            sha256="d" * 64,
            size_bytes=1,
            mime_type="text/markdown",
            is_extractable=True,
        )
        session.add(foreign_document)
        session.flush()
        batch = create_batch(session, profile.id, [local_document.id])
        session.commit()

        with pytest.raises(DBAPIError, match="job document project mismatch"):
            with session.begin_nested():
                session.add(
                    DocumentJob(
                        batch_id=batch.id,
                        document_version_id=foreign_document.id,
                    )
                )
                session.flush()


def test_sqlite_trigger_rejects_batch_project_change_that_orphans_jobs(
    app_session_factory: sessionmaker[Session], project, foreign_project, users
) -> None:
    with app_session_factory() as session:
        profile = _profile_version(
            session,
            project_id=project.id,
            creator_id=users["operator"].id,
            routes={"text_rule": True},
        )
        foreign_profile = _profile_version(
            session,
            project_id=foreign_project.id,
            creator_id=users["operator"].id,
            routes={"text_rule": True},
        )
        document = _documents(
            session,
            project_id=project.id,
            creator_id=users["operator"].id,
            count=1,
        )[0]
        batch = create_batch(session, profile.id, [document.id])
        session.commit()

        with pytest.raises(DBAPIError, match="batch project change breaks jobs"):
            session.execute(
                update(ExtractionBatch)
                .where(ExtractionBatch.id == batch.id)
                .values(
                    project_id=foreign_project.id,
                    profile_version_id=foreign_profile.id,
                )
            )


def test_sqlite_trigger_rejects_document_project_change_that_breaks_jobs(
    app_session_factory: sessionmaker[Session], project, foreign_project, users
) -> None:
    with app_session_factory() as session:
        profile = _profile_version(
            session,
            project_id=project.id,
            creator_id=users["operator"].id,
            routes={"text_rule": True},
        )
        document = _documents(
            session,
            project_id=project.id,
            creator_id=users["operator"].id,
            count=1,
        )[0]
        create_batch(session, profile.id, [document.id])
        session.commit()

        with pytest.raises(DBAPIError, match="document project change breaks jobs"):
            session.execute(
                update(Document)
                .where(Document.id == document.document_id)
                .values(project_id=foreign_project.id)
            )


def test_sqlite_trigger_rejects_profile_project_change_that_breaks_batches(
    app_session_factory: sessionmaker[Session], project, foreign_project, users
) -> None:
    with app_session_factory() as session:
        profile = _profile_version(
            session,
            project_id=project.id,
            creator_id=users["operator"].id,
            routes={"text_rule": True},
        )
        document = _documents(
            session,
            project_id=project.id,
            creator_id=users["operator"].id,
            count=1,
        )[0]
        create_batch(session, profile.id, [document.id])
        session.commit()

        with pytest.raises(DBAPIError, match="profile project change breaks batches"):
            session.execute(
                update(ExtractionProfile)
                .where(ExtractionProfile.id == profile.profile_id)
                .values(project_id=foreign_project.id)
            )


def test_batch_api_preserves_permission_and_project_boundaries(
    client_factory, project, foreign_project
) -> None:
    operator = client_factory("operator")
    profile = operator.post(
        f"/api/projects/{project.id}/profiles",
        json={"name": "api-profile", "preset": "rule"},
    ).json()
    document = operator.post(
        f"/api/projects/{project.id}/documents",
        files={"file": ("safe.md", b"safe", "text/markdown")},
    ).json()
    payload = {
        "profile_version_id": profile["id"],
        "document_version_ids": [document["id"]],
    }

    forbidden = client_factory("viewer").post(
        f"/api/projects/{project.id}/batches", json=payload
    )
    wrong_project = operator.post(
        f"/api/projects/{foreign_project.id}/batches", json=payload
    )
    created = operator.post(f"/api/projects/{project.id}/batches", json=payload)

    assert forbidden.status_code == 403
    assert wrong_project.status_code in (403, 404)
    assert created.status_code == 201
    assert len(created.json()["document_jobs"]) == 1


def test_offline_migration_contains_job_constraints() -> None:
    migration = (Path(__file__).parents[2] / "alembic/versions/0004_jobs.py").read_text(
        encoding="utf-8"
    )

    assert "extraction_batches" in migration
    assert "document_jobs" in migration
    assert "job_steps" in migration
    assert "idempotency_key" in migration
    assert "UniqueConstraint" in migration
    assert "CheckConstraint" in migration
    assert "request_key" in migration
    assert "position" in migration
    assert "stage" in migration
    assert "batch profile project mismatch" in migration
    assert "job document project mismatch" in migration
    assert "batch project change breaks jobs" in migration
    assert "TG_OP = 'UPDATE'" in migration
    assert "FOR KEY SHARE" not in migration
    assert migration.count("FOR SHARE") >= 7
    assert "FOR SHARE conflicts with project_id NO KEY UPDATE" in migration
    assert "Lock order: profile/document -> batch -> job" in migration
    job_trigger = migration.index("CREATE FUNCTION enforce_job_document_project")
    document_lock = migration.index("lock referenced document", job_trigger)
    batch_lock = migration.index("lock referenced batch", job_trigger)
    revalidation = migration.index("job document project mismatch", job_trigger)
    assert document_lock < batch_lock < revalidation
    profile_trigger = migration.index("CREATE FUNCTION enforce_batch_profile_project")
    profile_lock = migration.index("lock referenced profile", profile_trigger)
    profile_version_lock = migration.index("lock profile version", profile_trigger)
    profile_revalidation = migration.index("batch profile project mismatch", profile_trigger)
    assert profile_lock < profile_version_lock < profile_revalidation
    document_version_lock = migration.index("lock document version", job_trigger)
    assert document_lock < document_version_lock < batch_lock
    assert "document project change breaks jobs" in migration
    assert "profile project change breaks batches" in migration


def test_alembic_metadata_registers_profile_and_job_models() -> None:
    environment = (Path(__file__).parents[2] / "alembic/env.py").read_text(
        encoding="utf-8"
    )

    assert "import app.profiles.models" in environment
    assert "import app.jobs.models" in environment


def test_rule_preset_expands_text_table_merge_validate_steps(app_session_factory, project, users) -> None:
    from app.jobs.service import create_batch

    with app_session_factory() as session:
        profile = ExtractionProfile(project_id=project.id, name="rule-preset")
        version = ProfileVersion(
            profile=profile,
            created_by_id=users["operator"].id,
            version_number=1,
            snapshot_json={"preset": "rule"},
            snapshot_sha256=hashlib.sha256(uuid4().bytes).hexdigest(),
        )
        document = _documents(
            session, project_id=project.id, creator_id=users["operator"].id, count=1
        )[0]
        session.add(version)
        session.flush()

        batch = create_batch(session, version.id, [document.id])

    kinds = [step.kind for step in batch.document_jobs[0].steps]
    assert kinds == ["rule_text", "rule_table", "merge", "validate"]
    stages = {step.kind: step.stage for step in batch.document_jobs[0].steps}
    assert stages == {"rule_text": 0, "rule_table": 0, "merge": 1, "validate": 2}
