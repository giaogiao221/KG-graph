from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import Base
from app.documents.models import Document, DocumentVersion
from app.jobs.models import DocumentJob, ExtractionBatch, JobStep
from app.jobs.service import create_batch
from app.jobs import state
from app.jobs.state import InvalidTransition, VersionConflict
from app.profiles.models import ExtractionProfile, ProfileVersion
from app.projects.models import Project


def _build_batch(session: Session, project_id: UUID, creator_id: UUID) -> ExtractionBatch:
    profile = ExtractionProfile(project_id=project_id, name=f"profile-{uuid4()}")
    profile_version = ProfileVersion(
        profile=profile,
        created_by_id=creator_id,
        version_number=1,
        snapshot_json={
            "routes": {"text_rule": True, "table_rule": True},
            "rule_engine": "builtin",
            "rule_version": str(uuid4()),
            "plugin_version": str(uuid4()),
            "prompt_version": str(uuid4()),
        },
        snapshot_sha256=hashlib.sha256(uuid4().bytes).hexdigest(),
    )
    document = Document(project_id=project_id, created_by_id=creator_id)
    document_version = DocumentVersion(
        document=document,
        uploader_id=creator_id,
        version_number=1,
        original_filename="state.md",
        storage_key=hashlib.sha256(uuid4().bytes).hexdigest(),
        sha256=hashlib.sha256(b"state").hexdigest(),
        size_bytes=5,
        mime_type="text/markdown",
        is_extractable=True,
    )
    session.add_all([profile_version, document_version])
    session.flush()
    return create_batch(session, profile_version.id, [document_version.id])


@pytest.fixture
def batch_ids(app_session_factory, project, users) -> tuple[UUID, UUID, list[UUID]]:
    with app_session_factory.begin() as session:
        batch = _build_batch(session, project.id, users["operator"].id)
        job = batch.document_jobs[0]
        return batch.id, job.id, [step.id for step in job.steps]


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("queued", "running"),
        ("queued", "cancelled"),
        ("running", "partial_success"),
        ("running", "completed"),
        ("running", "retryable_failed"),
        ("running", "permanent_failed"),
        ("running", "cancelled"),
        ("partial_success", "completed"),
        ("partial_success", "retryable_failed"),
        ("retryable_failed", "queued"),
        ("retryable_failed", "permanent_failed"),
    ],
)
def test_explicit_allowed_step_transitions(
    app_session_factory, batch_ids, source: str, target: str
) -> None:
    step_id = batch_ids[2][0]
    with app_session_factory() as session:
        source_values = {"status": source, "version": 4}
        if source == "running":
            source_values.update(
                lease_token=uuid4().hex,
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        session.execute(
            update(JobStep).where(JobStep.id == step_id).values(**source_values)
        )
        if source != "queued":
            parent_status = (
                "retryable_failed"
                if (source, target) == ("retryable_failed", "queued")
                else "running"
            )
            session.execute(update(DocumentJob).values(status=parent_status))
            session.execute(update(ExtractionBatch).values(status=parent_status))
        session.commit()

        step = state.transition_step(session, step_id, target, expected_version=4)

        assert step.status == target
        assert step.version == 5


@pytest.mark.parametrize("terminal", ["completed", "permanent_failed", "cancelled"])
def test_terminal_step_cannot_transition(
    app_session_factory, batch_ids, terminal: str
) -> None:
    step_id = batch_ids[2][0]
    with app_session_factory() as session:
        session.execute(
            update(JobStep).where(JobStep.id == step_id).values(status=terminal, version=2)
        )
        session.commit()

        with pytest.raises(InvalidTransition):
            state.transition_step(session, step_id, "running", expected_version=2)

        step = session.get(JobStep, step_id)
        assert step is not None
        assert step.status == terminal
        assert step.version == 2


def test_stale_expected_version_is_rejected_without_mutation(
    app_session_factory, batch_ids
) -> None:
    step_id = batch_ids[2][0]
    with app_session_factory() as session:
        session.execute(
            update(JobStep).where(JobStep.id == step_id).values(status="queued", version=3)
        )
        session.commit()

        with pytest.raises(VersionConflict):
            state.transition_step(session, step_id, "running", expected_version=2)

        step = session.get(JobStep, step_id)
        assert step is not None
        assert step.status == "queued"
        assert step.version == 3


def test_unknown_target_status_is_rejected() -> None:
    with pytest.raises(InvalidTransition, match="unknown target step status: invented"):
        state.validate_transition("queued", "invented")


def test_unknown_source_status_names_the_source() -> None:
    with pytest.raises(InvalidTransition, match="unknown source step status: corrupt"):
        state.validate_transition("corrupt", "running")


def test_two_sessions_cannot_overwrite_a_terminal_step(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'cas.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    creator_id = uuid4()
    with factory.begin() as setup:
        project = Project(name="CAS project")
        setup.add(project)
        setup.flush()
        batch = _build_batch(setup, project.id, creator_id)
        step_id = batch.document_jobs[0].steps[0].id

    first = factory()
    stale = factory()
    try:
        stale_step = stale.get(JobStep, step_id)
        assert stale_step is not None
        assert stale_step.version == 1
        state.transition_step(first, step_id, "running", expected_version=1)
        first.commit()
        state.transition_step(first, step_id, "completed", expected_version=2)
        first.commit()

        with pytest.raises(VersionConflict):
            state.transition_step(stale, step_id, "running", expected_version=1)
        stale.rollback()

        persisted = stale.get(JobStep, step_id)
        assert persisted is not None
        stale.refresh(persisted)
        assert persisted.status == "completed"
        assert persisted.version == 3
    finally:
        first.close()
        stale.close()


def test_document_job_and_batch_have_explicit_cas_transitions(
    app_session_factory, batch_ids
) -> None:
    batch_id, job_id, _ = batch_ids
    with app_session_factory() as session:
        batch = state.transition_batch(session, batch_id, "running", expected_version=1)
        job = state.transition_document_job(session, job_id, "running", expected_version=1)

        assert (job.status, job.version) == ("running", 2)
        assert (batch.status, batch.version) == ("running", 2)

        with pytest.raises(VersionConflict):
            state.transition_document_job(session, job_id, "queued", expected_version=1)
        with pytest.raises(VersionConflict):
            state.transition_batch(session, batch_id, "queued", expected_version=1)


def test_completed_steps_aggregate_document_job_and_batch_to_completed(
    app_session_factory, batch_ids
) -> None:
    batch_id, job_id, step_ids = batch_ids
    with app_session_factory() as session:
        for step_id in step_ids:
            step = state.transition_step(session, step_id, "running", expected_version=1)
            state.transition_step(
                session, step_id, "completed", expected_version=step.version
            )

        job = session.get(DocumentJob, job_id)
        batch = session.get(ExtractionBatch, batch_id)
        assert job is not None and job.status == "completed"
        assert batch is not None and batch.status == "completed"


def test_merge_and_validate_wait_for_prior_stages(
    app_session_factory, batch_ids
) -> None:
    _, _, _step_ids = batch_ids
    with app_session_factory() as session:
        steps = list(
            session.scalars(select(JobStep).order_by(JobStep.position))
        )
        generators = [step for step in steps if step.stage == 0]
        merge = next(step for step in steps if step.kind == "merge")
        validate = next(step for step in steps if step.kind == "validate")

        assert all(state.is_step_ready(session, step.id) for step in generators)
        assert not state.is_step_ready(session, merge.id)
        assert not state.is_step_ready(session, validate.id)
        with pytest.raises(state.PrerequisiteNotMet):
            state.transition_step(
                session, merge.id, "running", expected_version=merge.version
            )

        for generator in generators:
            running = state.transition_step(
                session, generator.id, "running", expected_version=generator.version
            )
            state.transition_step(
                session, generator.id, "completed", expected_version=running.version
            )
        assert state.is_step_ready(session, merge.id)
        assert not state.is_step_ready(session, validate.id)

        running_merge = state.transition_step(
            session, merge.id, "running", expected_version=merge.version
        )
        state.transition_step(
            session,
            merge.id,
            "completed",
            expected_version=running_merge.version,
        )
        assert state.is_step_ready(session, validate.id)


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (["queued", "queued"], "queued"),
        (["running", "queued"], "running"),
        (["completed", "queued"], "running"),
        (["completed", "completed"], "completed"),
        (["cancelled", "cancelled"], "cancelled"),
        (["retryable_failed", "completed"], "retryable_failed"),
        (["permanent_failed", "permanent_failed"], "permanent_failed"),
        (["completed", "permanent_failed"], "partial_success"),
        (["completed", "cancelled"], "partial_success"),
        (["partial_success", "completed"], "partial_success"),
    ],
)
def test_parent_status_aggregation_rules(
    statuses: list[str], expected: str
) -> None:
    assert state.aggregate_status(statuses) == expected


@pytest.mark.parametrize(
    "target", ["partial_success", "completed", "permanent_failed", "cancelled"]
)
@pytest.mark.parametrize("parent", ["document_job", "batch"])
def test_aggregate_parent_states_cannot_be_set_manually(
    app_session_factory, batch_ids, parent: str, target: str
) -> None:
    batch_id, job_id, _ = batch_ids
    with app_session_factory() as session:
        if parent == "document_job":
            running = state.transition_document_job(
                session, job_id, "running", expected_version=1
            )
            transition = state.transition_document_job
        else:
            running = state.transition_batch(
                session, batch_id, "running", expected_version=1
            )
            transition = state.transition_batch

        with pytest.raises(InvalidTransition, match="aggregate-only"):
            transition(session, running.id, target, expected_version=running.version)


def test_completed_parent_cannot_regress_when_child_state_is_inconsistent(
    app_session_factory, batch_ids
) -> None:
    batch_id, job_id, step_ids = batch_ids
    with app_session_factory() as session:
        for step_id in step_ids:
            running = state.transition_step(session, step_id, "running", 1)
            state.transition_step(session, step_id, "completed", running.version)
        session.commit()

        first_step = session.get(JobStep, step_ids[0])
        assert first_step is not None
        session.execute(
            update(JobStep)
            .where(JobStep.id == first_step.id)
            .values(status="queued", version=first_step.version + 1)
        )
        session.commit()
        inconsistent_version = session.scalar(
            select(JobStep.version).where(JobStep.id == first_step.id)
        )
        assert inconsistent_version is not None

        with pytest.raises(VersionConflict, match="terminal parent"):
            state.transition_step(
                session,
                first_step.id,
                "running",
                expected_version=inconsistent_version,
            )
        session.commit()

        persisted_step = session.get(JobStep, first_step.id)
        job = session.get(DocumentJob, job_id)
        batch = session.get(ExtractionBatch, batch_id)
        assert persisted_step is not None and persisted_step.status == "queued"
        assert job is not None and job.status == "completed"
        assert batch is not None and batch.status == "completed"


def test_cancelled_children_aggregate_consistently_to_parents(
    app_session_factory, batch_ids
) -> None:
    batch_id, job_id, step_ids = batch_ids
    with app_session_factory() as session:
        for step_id in step_ids:
            state.transition_step(session, step_id, "cancelled", 1)

        child_statuses = list(
            session.scalars(
                select(JobStep.status).where(JobStep.document_job_id == job_id)
            )
        )
        job = session.get(DocumentJob, job_id)
        batch = session.get(ExtractionBatch, batch_id)
        assert job is not None and job.status == state.aggregate_status(child_statuses)
        assert batch is not None and batch.status == state.aggregate_status([job.status])


def test_parent_aggregation_failure_rolls_back_step_cas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'aggregate-cas.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as setup:
        project = Project(name="Aggregate CAS project")
        setup.add(project)
        setup.flush()
        batch = _build_batch(setup, project.id, uuid4())
        generator_ids = [
            step.id for step in batch.document_jobs[0].steps if step.stage == 0
        ]
        job_id, batch_id = batch.document_jobs[0].id, batch.id

    first = factory()
    second = factory()
    try:
        assert first.get(JobStep, generator_ids[0]) is not None
        assert second.get(JobStep, generator_ids[1]) is not None
        state.transition_step(first, generator_ids[0], "cancelled", 1)
        first.commit()

        original = state._set_aggregate_status

        def conflict_on_second_parent(
            session: Session, model, object_id: UUID, target: str
        ) -> None:
            if session is second and model is DocumentJob:
                raise VersionConflict("parent state changed during aggregation")
            original(session, model, object_id, target)

        monkeypatch.setattr(state, "_set_aggregate_status", conflict_on_second_parent)
        with pytest.raises(VersionConflict):
            state.transition_step(second, generator_ids[1], "cancelled", 1)
        second.commit()
    finally:
        first.close()
        second.close()

    with factory() as verify:
        steps = list(
            verify.scalars(
                select(JobStep)
                .where(JobStep.id.in_(generator_ids))
                .order_by(JobStep.position)
            )
        )
        job = verify.get(DocumentJob, job_id)
        batch = verify.get(ExtractionBatch, batch_id)
        assert [step.status for step in steps] == ["cancelled", "queued"]
        assert job is not None and job.status == "running"
        assert batch is not None and batch.status == "running"


def test_parent_aggregation_uses_independent_retry_transition_table() -> None:
    assert state.ALLOWED_PARENT_AGGREGATE_TRANSITIONS is not state.ALLOWED_TRANSITIONS
    assert "running" in state.ALLOWED_PARENT_AGGREGATE_TRANSITIONS["retryable_failed"]
    assert "running" in state.ALLOWED_PARENT_AGGREGATE_TRANSITIONS["partial_success"]
    assert not state.ALLOWED_PARENT_AGGREGATE_TRANSITIONS["completed"]


@pytest.mark.parametrize("parent_status", ["retryable_failed", "partial_success"])
def test_completed_and_retryable_steps_can_be_requeued_to_running_parent(
    app_session_factory, batch_ids, parent_status
) -> None:
    batch_id, job_id, step_ids = batch_ids
    failed_step_id = step_ids[0]
    with app_session_factory() as session:
        session.execute(
            update(JobStep)
            .where(JobStep.id.in_(step_ids))
            .values(status="completed", version=3)
        )
        session.execute(
            update(JobStep)
            .where(JobStep.id == failed_step_id)
            .values(status="retryable_failed", version=4)
        )
        session.execute(
            update(DocumentJob)
            .where(DocumentJob.id == job_id)
            .values(status=parent_status, version=3)
        )
        session.execute(
            update(ExtractionBatch)
            .where(ExtractionBatch.id == batch_id)
            .values(status=parent_status, version=3)
        )
        session.commit()

        requeued = state.transition_step(
            session, failed_step_id, "queued", expected_version=4
        )

        job = session.get(DocumentJob, job_id)
        batch = session.get(ExtractionBatch, batch_id)
        assert requeued.status == "queued"
        assert job is not None and job.status == "running"
        assert batch is not None and batch.status == "running"
