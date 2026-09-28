from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.settings import get_settings
from app.facts.models import RawFact
from app.extraction.contracts import PermanentAdapterError
from app.jobs.models import JobArtifact
from app.jobs.tasks import LeaseLost, StepExecutionFailed, execute_step_once
from app.jobs import tasks as job_tasks

from .test_importer import _job


def test_commit_reconciliation_database_failure_is_unknown() -> None:
    class UnavailableFactory:
        def __call__(self):
            raise SQLAlchemyError("database unavailable")

    lease = job_tasks.StepLease(
        step_id=uuid4(),
        token="lease-token",
        version=2,
        attempt_number=1,
        expires_at=datetime.now(UTC),
    )
    assert job_tasks._reconcile_validate_commit(
        UnavailableFactory(),  # type: ignore[arg-type]
        lease=lease,
        merge_step_id=uuid4(),
        expected_result=None,
    ) == "unknown"


def test_commit_reconciliation_locks_step_before_deciding(
    app_session_factory, project, users, monkeypatch
) -> None:
    with app_session_factory.begin() as session:
        steps = _job(session, project.id, users["operator"].id)
        validate = steps["validate"]
        validate.status = "running"
        validate.lease_token = "locked-reconcile-lease"
        validate.lease_expires_at = datetime.now(UTC)
        validate.version = 2
        lease = job_tasks.StepLease(
            validate.id, validate.lease_token, validate.version, 1,
            validate.lease_expires_at,
        )
        merge_id = steps["merge"].id

    original_scalar = Session.scalar
    statements = []

    def record_scalar(session, statement, *args, **kwargs):
        statements.append(statement)
        return original_scalar(session, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "scalar", record_scalar)
    assert job_tasks._reconcile_validate_commit(
        app_session_factory,
        lease=lease,
        merge_step_id=merge_id,
        expected_result=None,
    ) == "uncommitted"
    step_queries = [
        statement
        for statement in statements
        if any(
            item.get("entity") is job_tasks.JobStep
            for item in statement.column_descriptions
        )
    ]
    assert len(step_queries) == 1
    assert step_queries[0]._for_update_arg is not None


def test_postgres_reconciliation_sets_short_local_lock_timeout() -> None:
    class PostgreSQLSession:
        def __init__(self, step):
            self.step = step
            self.commands = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get_bind(self):
            return type("Bind", (), {"dialect": type("Dialect", (), {"name": "postgresql"})()})()

        def execute(self, statement):
            self.commands.append(statement)

        def scalar(self, statement):
            self.commands.append(statement)
            return self.step

    lease = job_tasks.StepLease(
        uuid4(), "pg-lock-lease", 4, 1, datetime.now(UTC)
    )
    step = type(
        "Step",
        (),
        {"status": "running", "lease_token": lease.token, "version": lease.version},
    )()
    session = PostgreSQLSession(step)
    factory = lambda: session

    assert job_tasks._reconcile_validate_commit(
        factory,  # type: ignore[arg-type]
        lease=lease,
        merge_step_id=uuid4(),
        expected_result=None,
    ) == "uncommitted"
    assert str(session.commands[0]) == "SET LOCAL lock_timeout = '5s'"
    assert session.commands[1]._for_update_arg is not None


def _completed_native_merge(
    app_session_factory, project, users, tmp_path: Path, monkeypatch
) -> dict[str, object]:
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    monkeypatch.setenv("EXTRACTION_STORAGE_ROOT", str(storage_root))
    get_settings.cache_clear()
    content = b"Alpha has mass 3 kg."
    with app_session_factory.begin() as session:
        steps = _job(session, project.id, users["operator"].id, content)
        steps["merge"].status = "queued"
        version = steps["rule_text"].document_job.document_version
        (storage_root / version.storage_key).write_bytes(content)
        ids = {kind: step.id for kind, step in steps.items()}
    execute_step_once(ids["rule_text"], factory=app_session_factory, work_root=tmp_path / "work")
    execute_step_once(ids["merge"], factory=app_session_factory, work_root=tmp_path / "work")
    return {"ids": ids, "storage_root": storage_root}


def test_validate_imports_merge_candidate_and_cleans_owned_artifacts(
    app_session_factory, project, users, tmp_path: Path, monkeypatch
) -> None:
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    monkeypatch.setenv("EXTRACTION_STORAGE_ROOT", str(storage_root))
    get_settings.cache_clear()
    content = b"Alpha has mass 3 kg."
    with app_session_factory.begin() as session:
        steps = _job(session, project.id, users["operator"].id, content)
        # Restore pipeline state: merge must be produced by this test.
        steps["merge"].status = "queued"
        version = steps["rule_text"].document_job.document_version
        (storage_root / version.storage_key).write_bytes(content)
        step_ids = {kind: step.id for kind, step in steps.items()}

    execute_step_once(
        step_ids["rule_text"], factory=app_session_factory, work_root=tmp_path / "work"
    )
    execute_step_once(
        step_ids["merge"], factory=app_session_factory, work_root=tmp_path / "work"
    )
    execute_step_once(
        step_ids["validate"], factory=app_session_factory, work_root=tmp_path / "work"
    )

    with app_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(RawFact)) == 1
        merge_artifacts = list(
            session.scalars(
                select(JobArtifact).where(JobArtifact.step_id == step_ids["merge"])
            )
        )
        assert {artifact.status for artifact in merge_artifacts} == {"imported_cleanup"}
        merge_keys = [artifact.storage_key for artifact in merge_artifacts]
    assert all(not (storage_root / key).exists() for key in merge_keys)


@pytest.mark.parametrize("field,value", [("route", None), ("route", "rule_text"), ("step_id", "wrong")])
def test_validate_rejects_every_malformed_completed_merge_envelope(
    app_session_factory, project, users, tmp_path: Path, monkeypatch, field, value
) -> None:
    setup = _completed_native_merge(app_session_factory, project, users, tmp_path, monkeypatch)
    ids = setup["ids"]
    with app_session_factory.begin() as session:
        merge = session.get(job_tasks.JobStep, ids["merge"])
        merge.result_json = {**merge.result_json, field: value}
    with pytest.raises(StepExecutionFailed):
        execute_step_once(ids["validate"], factory=app_session_factory, work_root=tmp_path / "work")
    with app_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(RawFact)) == 0


def test_validate_copies_and_strictly_checks_claimed_manifest(
    app_session_factory, project, users, tmp_path: Path, monkeypatch
) -> None:
    setup = _completed_native_merge(app_session_factory, project, users, tmp_path, monkeypatch)
    ids = setup["ids"]
    original = job_tasks.LocalStorage.copy_to
    copied_manifest = False

    def corrupt_manifest(self, key, destination, **kwargs):
        nonlocal copied_manifest
        original(self, key, destination, **kwargs)
        if destination.name == "manifest.json":
            copied_manifest = True
            destination.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(job_tasks.LocalStorage, "copy_to", corrupt_manifest)
    with pytest.raises(StepExecutionFailed):
        execute_step_once(ids["validate"], factory=app_session_factory, work_root=tmp_path / "work")
    assert copied_manifest is True


def test_validate_rejects_manifest_record_count_mismatch(
    app_session_factory, project, users, tmp_path: Path, monkeypatch
) -> None:
    setup = _completed_native_merge(app_session_factory, project, users, tmp_path, monkeypatch)
    ids = setup["ids"]
    original = job_tasks.LocalStorage.copy_to

    def change_count(self, key, destination, **kwargs):
        original(self, key, destination, **kwargs)
        if destination.name == "manifest.json":
            import json
            payload = json.loads(destination.read_text(encoding="utf-8"))
            payload["records"] += 1
            destination.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(job_tasks.LocalStorage, "copy_to", change_count)
    with pytest.raises(StepExecutionFailed):
        execute_step_once(ids["validate"], factory=app_session_factory, work_root=tmp_path / "work")
    with app_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(RawFact)) == 0


def test_after_commit_error_reconciles_completed_validate_and_cleans_artifacts(
    app_session_factory, project, users, tmp_path: Path, monkeypatch
) -> None:
    setup = _completed_native_merge(app_session_factory, project, users, tmp_path, monkeypatch)
    ids = setup["ids"]
    real_commit = Session.commit
    raised = False

    def commit_then_disconnect(session):
        nonlocal raised
        validate = session.get(job_tasks.JobStep, ids["validate"])
        should_raise = not raised and validate is not None and validate.status == "completed"
        real_commit(session)
        if should_raise:
            raised = True
            raise SQLAlchemyError("connection lost after COMMIT")

    monkeypatch.setattr(Session, "commit", commit_then_disconnect)
    execute_step_once(ids["validate"], factory=app_session_factory, work_root=tmp_path / "work")
    assert raised is True
    with app_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(RawFact)) == 1
        assert set(
            session.scalars(
                select(JobArtifact.status).where(JobArtifact.step_id == ids["merge"])
            )
        ) == {"imported_cleanup"}


def test_before_commit_rollback_aborts_claim_and_leaves_no_facts(
    app_session_factory, project, users, tmp_path: Path, monkeypatch
) -> None:
    setup = _completed_native_merge(app_session_factory, project, users, tmp_path, monkeypatch)
    ids = setup["ids"]
    real_commit = Session.commit
    raised = False

    def rollback_then_disconnect(session):
        nonlocal raised
        validate = session.get(job_tasks.JobStep, ids["validate"])
        if not raised and validate is not None and validate.status == "completed":
            raised = True
            session.rollback()
            raise SQLAlchemyError("commit rejected before durable write")
        return real_commit(session)

    monkeypatch.setattr(Session, "commit", rollback_then_disconnect)
    with pytest.raises(job_tasks.ControlPlaneUnavailable):
        execute_step_once(ids["validate"], factory=app_session_factory, work_root=tmp_path / "work")
    with app_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(RawFact)) == 0
        artifacts = list(
            session.scalars(select(JobArtifact).where(JobArtifact.step_id == ids["merge"]))
        )
        assert {artifact.status for artifact in artifacts} == {"referenced"}
        assert all(artifact.claim_token is None for artifact in artifacts)


def test_unknown_commit_reconciliation_never_aborts_artifact_claim(
    app_session_factory, project, users, tmp_path: Path, monkeypatch
) -> None:
    setup = _completed_native_merge(app_session_factory, project, users, tmp_path, monkeypatch)
    ids = setup["ids"]
    real_commit = Session.commit
    raised = False

    def rollback_then_disconnect(session):
        nonlocal raised
        validate = session.get(job_tasks.JobStep, ids["validate"])
        if not raised and validate is not None and validate.status == "completed":
            raised = True
            session.rollback()
            raise SQLAlchemyError("commit outcome unavailable")
        return real_commit(session)

    monkeypatch.setattr(Session, "commit", rollback_then_disconnect)
    monkeypatch.setattr(job_tasks, "_reconcile_validate_commit", lambda *_args, **_kwargs: "unknown")
    with pytest.raises(job_tasks.ControlPlaneUnavailable):
        execute_step_once(ids["validate"], factory=app_session_factory, work_root=tmp_path / "work")
    with app_session_factory() as session:
        artifacts = list(
            session.scalars(select(JobArtifact).where(JobArtifact.step_id == ids["merge"]))
        )
        assert {artifact.status for artifact in artifacts} == {"importing"}
        assert all(artifact.claim_token for artifact in artifacts)


def test_validate_uses_immutable_claim_refs_after_result_json_changes(
    app_session_factory, project, users, tmp_path: Path, monkeypatch
) -> None:
    setup = _completed_native_merge(app_session_factory, project, users, tmp_path, monkeypatch)
    ids = setup["ids"]
    original_claim = job_tasks.claim_artifacts_for_import

    def claim_then_replace_result(session, step_id, **kwargs):
        claim = original_claim(session, step_id, **kwargs)
        merge = session.get(job_tasks.JobStep, step_id)
        merge.result_json = {
            **merge.result_json,
            "artifacts": {
                "manifest": {"replaced": True},
                "candidate_tsv": {"replaced": True},
            },
        }
        assert {reference.kind for reference in claim.references} == {
            "manifest", "candidate_tsv"
        }
        return claim

    monkeypatch.setattr(job_tasks, "claim_artifacts_for_import", claim_then_replace_result)
    execute_step_once(ids["validate"], factory=app_session_factory, work_root=tmp_path / "work")
    with app_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(RawFact)) == 1


def test_cleanup_failure_never_rewrites_completed_validate_as_failed(
    app_session_factory, project, users, tmp_path: Path, monkeypatch
) -> None:
    setup = _completed_native_merge(app_session_factory, project, users, tmp_path, monkeypatch)
    ids = setup["ids"]
    monkeypatch.setattr(
        job_tasks,
        "complete_artifact_import",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PermanentAdapterError("cleanup unavailable")
        ),
    )
    with pytest.raises(job_tasks.PostCommitCleanupError):
        execute_step_once(ids["validate"], factory=app_session_factory, work_root=tmp_path / "work")
    with app_session_factory() as session:
        validate = session.get(job_tasks.JobStep, ids["validate"])
        assert validate.status == "completed"
        assert validate.failure_code is None
        assert session.scalar(select(func.count()).select_from(RawFact)) == 1


def test_validate_failure_aborts_artifact_claim_without_partial_fact_commit(
    app_session_factory, project, users, tmp_path: Path, monkeypatch
) -> None:
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    monkeypatch.setenv("EXTRACTION_STORAGE_ROOT", str(storage_root))
    get_settings.cache_clear()
    content = b"Alpha has mass 3 kg."
    with app_session_factory.begin() as session:
        steps = _job(session, project.id, users["operator"].id, content)
        steps["merge"].status = "queued"
        version = steps["rule_text"].document_job.document_version
        (storage_root / version.storage_key).write_bytes(content)
        ids = {kind: step.id for kind, step in steps.items()}
    execute_step_once(ids["rule_text"], factory=app_session_factory, work_root=tmp_path / "work")
    execute_step_once(ids["merge"], factory=app_session_factory, work_root=tmp_path / "work")

    monkeypatch.setattr(
        job_tasks,
        "import_facts",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PermanentAdapterError("invalid candidate")
        ),
    )
    with pytest.raises(StepExecutionFailed):
        execute_step_once(ids["validate"], factory=app_session_factory, work_root=tmp_path / "work")

    with app_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(RawFact)) == 0
        artifacts = list(
            session.scalars(select(JobArtifact).where(JobArtifact.step_id == ids["merge"]))
        )
        assert {artifact.status for artifact in artifacts} == {"referenced"}
        assert all(artifact.claim_token is None for artifact in artifacts)


def test_validate_lease_loss_rolls_back_facts_and_aborts_claim(
    app_session_factory, project, users, tmp_path: Path, monkeypatch
) -> None:
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    monkeypatch.setenv("EXTRACTION_STORAGE_ROOT", str(storage_root))
    get_settings.cache_clear()
    content = b"Alpha has mass 3 kg."
    with app_session_factory.begin() as session:
        steps = _job(session, project.id, users["operator"].id, content)
        steps["merge"].status = "queued"
        version = steps["rule_text"].document_job.document_version
        (storage_root / version.storage_key).write_bytes(content)
        ids = {kind: step.id for kind, step in steps.items()}
    execute_step_once(ids["rule_text"], factory=app_session_factory, work_root=tmp_path / "work")
    execute_step_once(ids["merge"], factory=app_session_factory, work_root=tmp_path / "work")
    original_finish = job_tasks.finish_step_lease

    def lose_validate(session, lease, target, **kwargs):
        if lease.step_id == ids["validate"]:
            raise LeaseLost("lost before commit")
        return original_finish(session, lease, target, **kwargs)

    monkeypatch.setattr(job_tasks, "finish_step_lease", lose_validate)
    with pytest.raises(LeaseLost):
        execute_step_once(ids["validate"], factory=app_session_factory, work_root=tmp_path / "work")
    with app_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(RawFact)) == 0
        artifacts = list(
            session.scalars(select(JobArtifact).where(JobArtifact.step_id == ids["merge"]))
        )
        assert {artifact.status for artifact in artifacts} == {"referenced"}
