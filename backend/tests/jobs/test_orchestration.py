from __future__ import annotations

import hashlib
import inspect
import math
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.documents.models import Document, DocumentVersion
from app.documents.storage import LocalStorage
from app.core.settings import get_settings
from app.extraction.contracts import (
    AdapterContext,
    AdapterResult,
    PermanentAdapterError,
    RetryableAdapterError,
    validate_adapter_result,
)
from app.extraction.fake_adapter import FakeAdapter
from app.extraction.native.common import write_route_output
from app.jobs.models import JobCleanupOutbox, JobDispatchOutbox, JobStep
from app.jobs import state
from app.jobs.service import create_batch
from app.jobs.tasks import (
    MAX_ATTEMPTS,
    ControlPlaneUnavailable,
    LeaseNotAcquired,
    acquire_step_lease,
    classify_failure,
    complete_cleanup_attempt,
    execute_step,
    execute_step_once,
    finish_step_lease,
    load_dispatch,
    publish_outbox,
    queue_for_step_kind,
    ready_steps,
    recover_expired_leases,
    recover_step_lease,
    renew_step_lease,
    retry_pending_workdir_cleanup,
    stage_ready_dispatches,
    StepLease,
    StepExecutionFailed,
)
from app.jobs.celery_app import celery_app
from app.jobs import tasks as job_tasks
from app.profiles.models import ExtractionProfile, ProfileVersion
from app.projects.models import Project


@pytest.mark.parametrize(
    "corruption",
    ["non-dict", "missing-artifacts", "route", "step", "document", "execution"],
)
def test_completed_sibling_artifact_identity_is_strict_and_fixed_error(corruption):
    step_id, document_id = uuid4(), uuid4()
    result = {
        "schema": "native-artifacts-v1",
        "route": "rule_text",
        "step_id": str(step_id),
        "document_version_id": str(document_id),
        "execution_key": "expected-key",
        "metrics": {"records": 1},
        "artifacts": {"manifest": {}, "candidate_tsv": {}},
    }
    if corruption == "non-dict":
        result = []
    elif corruption == "missing-artifacts":
        result.pop("artifacts")
    elif corruption == "route":
        result["route"] = "llm_text"
    elif corruption == "step":
        result["step_id"] = str(uuid4())
    elif corruption == "document":
        result["document_version_id"] = str(uuid4())
    elif corruption == "execution":
        result["execution_key"] = "wrong"
    sibling = SimpleNamespace(
        id=step_id,
        kind="rule_text",
        idempotency_key="expected-key",
        result_json=result,
    )

    with pytest.raises(PermanentAdapterError, match="persisted adapter artifacts are invalid") as raised:
        job_tasks._validated_sibling_results([sibling], document_id)
    assert raised.value.__cause__ is None


def test_document_input_is_verified_and_staged_below_workdir(tmp_path: Path):
    storage_root = tmp_path / "storage"; storage_root.mkdir()
    storage = LocalStorage(storage_root)
    content = b"Alpha has mass 3 kg."
    key = "a" * 64 + ".md"
    (storage_root / key).write_bytes(content)
    work = tmp_path / "work"; work.mkdir()

    staged = job_tasks._stage_document_input(
        storage=storage, storage_key=key, expected_size=len(content),
        expected_sha256=hashlib.sha256(content).hexdigest(), work_dir=work,
    )

    assert staged.read_bytes() == content
    assert work.resolve() in staged.resolve().parents


def test_worker_runs_native_rule_route_from_verified_storage_and_persists_artifact(
    app_session_factory, project, users, tmp_path: Path, monkeypatch
):
    storage_root = tmp_path / "native-storage"; storage_root.mkdir()
    monkeypatch.setenv("EXTRACTION_STORAGE_ROOT", str(storage_root))
    get_settings.cache_clear()
    content = b"Alpha has mass 3 kg."
    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id, content)
        step = session.get(JobStep, step_ids["rule_text"])
        version = step.document_job.document_version
        (storage_root / version.storage_key).write_bytes(content)

    execute_step_once(step_ids["rule_text"], factory=app_session_factory, work_root=tmp_path / "work")

    with app_session_factory() as session:
        step = session.get(JobStep, step_ids["rule_text"])
        assert step.status == "completed"
        assert step.result_json["metrics"]["records"] == 1
        assert {
            artifact.status for artifact in step.artifacts
        } == {"referenced"}
        reference = step.result_json["artifacts"]["candidate_tsv"]
        with LocalStorage(storage_root).open_read(reference["key"]) as source:
            assert b"Alpha has mass 3 kg." in source.read()


def test_native_merge_worker_rebuilds_completed_sibling_artifacts_after_cleanup(
    app_session_factory, project, users, tmp_path: Path, monkeypatch
):
    object_root = tmp_path / "objects"
    monkeypatch.setenv("EXTRACTION_STORAGE_ROOT", str(object_root))
    get_settings.cache_clear()
    persisted = {}
    storage = LocalStorage(object_root)
    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)
        document_id = session.get(JobStep, step_ids["rule_text"]).document_job.document_version_id
        for route in ("rule_text", "llm_table"):
            sibling = session.get(JobStep, step_ids[route])
            context = AdapterContext(
                work_dir=tmp_path / f"artifact-{route}",
                execution_idempotency_key=sibling.idempotency_key,
                step_id=sibling.id,
                document_version_id=document_id,
            )
            result = write_route_output(context, route, [{"subject":"Alpha","property":"mass","value":"3","evidence_text":f"{route} evidence"}], lambda _event: None)
            persisted[route] = job_tasks._structured_result(context, result, storage, app_session_factory)
            job_tasks._mark_artifacts_referenced(session, sibling.id, persisted[route])
            running = state.transition_step(session, step_ids[route], "running", 1)
            completed = state.transition_step(session, step_ids[route], "completed", running.version)
            completed.result_json = persisted[route]

    execute_step_once(step_ids["merge"], factory=app_session_factory, work_root=tmp_path / "merge-root")

    with app_session_factory() as session:
        merged = session.get(JobStep, step_ids["merge"])
        assert merged.status == "completed"
        assert merged.result_json["metrics"]["input_routes"] == 2
        candidate = merged.result_json["artifacts"]["candidate_tsv"]
        with storage.open_read(candidate["key"]) as source:
            assert b"rule_text" in source.read()
from app.core.database import Base


def _job(*generator_statuses: tuple[str, str]):
    steps = [
        SimpleNamespace(kind=kind, status=status, stage=0, position=position)
        for position, (kind, status) in enumerate(generator_statuses)
    ]
    steps.extend(
        [
            SimpleNamespace(kind="merge", status="queued", stage=1, position=10),
            SimpleNamespace(kind="validate", status="queued", stage=2, position=11),
        ]
    )
    return SimpleNamespace(steps=steps)


def _persisted_job(
    session: Session,
    project_id: UUID,
    creator_id: UUID,
    document_bytes: bytes = b"task-seven",
) -> tuple[UUID, dict[str, UUID]]:
    profile = ExtractionProfile(project_id=project_id, name=f"task-seven-{uuid4()}")
    profile_version = ProfileVersion(
        profile=profile,
        created_by_id=creator_id,
        version_number=1,
        snapshot_json={
            "routes": {"text_rule": True, "table_llm": True},
            "rule_engine": "builtin",
            "rule_version": "rule-v1",
            "plugin_version": "plugin-v1",
            "prompt_version": "prompt-v1",
        },
        snapshot_sha256=hashlib.sha256(uuid4().bytes).hexdigest(),
    )
    document = Document(project_id=project_id, created_by_id=creator_id)
    document_version = DocumentVersion(
        document=document,
        uploader_id=creator_id,
        version_number=1,
        original_filename="task-seven.md",
        storage_key=hashlib.sha256(uuid4().bytes).hexdigest(),
        sha256=hashlib.sha256(document_bytes).hexdigest(),
        size_bytes=len(document_bytes),
        mime_type="text/markdown",
        is_extractable=True,
    )
    session.add_all([profile_version, document_version])
    session.flush()
    batch = create_batch(session, profile_version.id, [document_version.id])
    job = batch.document_jobs[0]
    return job.id, {step.kind: step.id for step in job.steps}


def test_merge_waits_for_all_selected_generator_steps() -> None:
    job = _job(("rule_text", "completed"), ("llm_table", "queued"))

    assert ready_steps(job) == [job.steps[1]]
    job.steps[1].status = "completed"
    assert [step.kind for step in ready_steps(job)] == ["merge"]


def test_validate_waits_for_merge_completion() -> None:
    job = _job(("rule_text", "completed"), ("llm_table", "completed"))
    merge, validate = job.steps[-2:]

    assert ready_steps(job) == [merge]
    merge.status = "completed"
    assert ready_steps(job) == [validate]


def test_rule_and_llm_steps_have_separate_queues() -> None:
    assert queue_for_step_kind("rule_text") == "rule"
    assert queue_for_step_kind("rule_table") == "rule"
    assert queue_for_step_kind("llm_text") == "llm"
    assert queue_for_step_kind("llm_table") == "llm"


def test_lease_is_cas_exclusive_and_has_an_expiry(
    app_session_factory: sessionmaker[Session], project, users
) -> None:
    now = datetime.now(UTC)
    with app_session_factory.begin() as setup:
        _, step_ids = _persisted_job(setup, project.id, users["operator"].id)
        step_id = step_ids["rule_text"]

    with app_session_factory.begin() as first:
        lease = acquire_step_lease(first, step_id, now=now, lease_for=timedelta(minutes=2))
        assert lease.expires_at == now + timedelta(minutes=2)

    with app_session_factory.begin() as second:
        with pytest.raises(LeaseNotAcquired):
            acquire_step_lease(second, step_id, now=now + timedelta(seconds=1))


def test_expired_worker_lease_requires_recovery_before_reacquire(
    app_session_factory: sessionmaker[Session], project, users
) -> None:
    now = datetime.now(UTC)
    with app_session_factory.begin() as setup:
        _, step_ids = _persisted_job(setup, project.id, users["operator"].id)
        step_id = step_ids["rule_text"]
    with app_session_factory.begin() as first:
        old = acquire_step_lease(first, step_id, now=now, lease_for=timedelta(seconds=1))
    with app_session_factory.begin() as still_running:
        with pytest.raises(LeaseNotAcquired):
            acquire_step_lease(still_running, step_id, now=now + timedelta(seconds=2))
    assert recover_expired_leases(
        app_session_factory, now=now + timedelta(seconds=2)
    ) == 1
    with app_session_factory.begin() as recovered:
        new = acquire_step_lease(recovered, step_id, now=now + timedelta(seconds=3))

    assert new.token != old.token
    assert new.version > old.version


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (RetryableAdapterError("rate limited"), "retryable"),
        (TimeoutError("timeout"), "retryable"),
        (ConnectionError("unavailable"), "retryable"),
        (PermanentAdapterError("bad config"), "permanent"),
        (ValueError("invalid output"), "permanent"),
    ],
)
def test_failures_are_classified_for_bounded_retry(error: Exception, expected: str) -> None:
    assert classify_failure(error) == expected
    assert MAX_ATTEMPTS == 3


def test_lease_columns_are_persistent_and_not_secret_bearing(
    app_session_factory: sessionmaker[Session], project, users
) -> None:
    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)
    with app_session_factory() as session:
        step = session.scalar(select(JobStep).where(JobStep.id == step_ids["rule_text"]))

    assert step is not None
    assert step.lease_token is None
    assert step.lease_expires_at is None
    assert step.attempt_count == 0


def test_worker_reloads_database_state_runs_in_isolation_and_commits_terminal_status(
    app_session_factory: sessionmaker[Session], project, users, tmp_path: Path
) -> None:
    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)
        step_id = step_ids["rule_text"]

    execute_step_once(
        step_id,
        factory=app_session_factory,
        work_root=tmp_path,
        adapters={"rule_text": FakeAdapter(name="rule_text")},
    )

    with app_session_factory() as session:
        step = session.get(JobStep, step_id)
        assert step is not None
        assert step.status == "completed"
        assert step.attempt_count == 1
        assert step.lease_token is None
        assert step.lease_expires_at is None
        assert step.result_json is not None
        assert step.result_json["metrics"] == {"records": 1}
    assert not (tmp_path / str(step_id)).exists()


class _RetryingAdapter(FakeAdapter):
    def run(self, context, emit_progress):
        raise RetryableAdapterError("temporary provider failure")


def test_worker_maps_adapter_failure_to_retryable_terminal_state(
    app_session_factory: sessionmaker[Session], project, users, tmp_path: Path
) -> None:
    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)
        step_id = step_ids["rule_text"]

    with pytest.raises(StepExecutionFailed) as raised:
        execute_step_once(
            step_id,
            factory=app_session_factory,
            work_root=tmp_path,
            adapters={"rule_text": _RetryingAdapter()},
        )

    assert raised.value.failure_kind == "retryable"
    with app_session_factory() as session:
        step = session.get(JobStep, step_id)
        assert step is not None and step.status == "queued"
        assert step.attempt_count == 1
        assert step.failure_code == "dependency_unavailable"
        assert step.failure_summary == "temporary dependency failure"
    assert raised.value.__cause__ is None


def test_celery_task_contract_accepts_only_a_database_id() -> None:
    assert execute_step.name == "app.jobs.tasks.execute_step"
    parameters = list(inspect.signature(execute_step.run).parameters.values())
    assert [parameter.name for parameter in parameters] == ["dispatch_id"]
    assert parameters[0].annotation in {"str", str}
    assert celery_app.conf.task_serializer == "json"
    assert {queue.name for queue in celery_app.conf.task_queues} == {"rule", "llm"}


def test_worker_task_swallows_post_commit_cleanup_error(monkeypatch) -> None:
    dispatch_id, step_id = uuid4(), uuid4()
    monkeypatch.setattr(job_tasks, "get_session_factory", lambda: object())
    monkeypatch.setattr(
        job_tasks, "load_dispatch", lambda *_args, **_kwargs: (step_id, 1)
    )
    monkeypatch.setattr(
        job_tasks,
        "execute_step_once",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            job_tasks.PostCommitCleanupError("cleanup pending")
        ),
    )

    assert job_tasks.execute_step.run(str(dispatch_id)) is None


def test_validate_rejects_completed_merge_without_importable_native_artifacts(
    app_session_factory: sessionmaker[Session], project, users, tmp_path: Path
) -> None:
    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)
        generator_ids = [step_ids["rule_text"], step_ids["llm_table"]]
        for step_id in generator_ids:
            running = state.transition_step(session, step_id, "running", 1)
            state.transition_step(session, step_id, "completed", running.version)

    execute_step_once(
        step_ids["merge"],
        factory=app_session_factory,
        work_root=tmp_path,
        adapters={"merge": FakeAdapter(name="merge")},
    )
    with pytest.raises(StepExecutionFailed) as raised:
        execute_step_once(
            step_ids["validate"], factory=app_session_factory, work_root=tmp_path
        )

    assert raised.value.failure_kind == "permanent"
    with app_session_factory() as session:
        assert session.get(JobStep, step_ids["merge"]).status == "completed"
        assert session.get(JobStep, step_ids["validate"]).status == "permanent_failed"


def test_completed_step_schedules_the_next_ready_stage_using_only_its_id(
    app_session_factory: sessionmaker[Session], project, users
) -> None:
    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)
        for kind in ("rule_text", "llm_table"):
            running = state.transition_step(session, step_ids[kind], "running", 1)
            state.transition_step(session, step_ids[kind], "completed", running.version)
    with app_session_factory.begin() as session:
        staged = stage_ready_dispatches(session, step_ids["llm_table"])

    assert [(row.step_id, row.queue) for row in staged] == [
        (step_ids["merge"], "rule")
    ]


def test_batch_creation_stages_generator_dispatches_without_broker_side_effect(
    app_session_factory: sessionmaker[Session], project, users
) -> None:
    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)
    with app_session_factory() as session:
        rows = list(
            session.scalars(
                select(JobDispatchOutbox).order_by(JobDispatchOutbox.created_at)
            )
        )

    assert {(row.step_id, row.queue) for row in rows} == {
        (step_ids["rule_text"], "rule"),
        (step_ids["llm_table"], "llm"),
    }
    assert all(row.published_at is None for row in rows)


def test_two_generator_completions_create_only_one_merge_dispatch(
    app_session_factory: sessionmaker[Session], project, users, tmp_path: Path
) -> None:
    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)

    execute_step_once(
        step_ids["rule_text"], factory=app_session_factory, work_root=tmp_path,
        adapters={"rule_text": FakeAdapter(name="rule_text")},
    )
    execute_step_once(
        step_ids["llm_table"], factory=app_session_factory, work_root=tmp_path,
        adapters={"llm_table": FakeAdapter(name="llm_table", queue="llm")},
    )

    with app_session_factory() as session:
        merge_rows = list(
            session.scalars(
                select(JobDispatchOutbox).where(
                    JobDispatchOutbox.step_id == step_ids["merge"]
                )
            )
        )
    assert len(merge_rows) == 1


def test_merge_completion_creates_only_one_validate_dispatch(
    app_session_factory: sessionmaker[Session], project, users, tmp_path: Path
) -> None:
    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)
    execute_step_once(
        step_ids["rule_text"], factory=app_session_factory, work_root=tmp_path,
        adapters={"rule_text": FakeAdapter(name="rule_text")},
    )
    execute_step_once(
        step_ids["llm_table"], factory=app_session_factory, work_root=tmp_path,
        adapters={"llm_table": FakeAdapter(name="llm_table", queue="llm")},
    )
    execute_step_once(
        step_ids["merge"], factory=app_session_factory, work_root=tmp_path,
        adapters={"merge": FakeAdapter(name="merge")},
    )

    with app_session_factory() as session:
        rows = list(
            session.scalars(
                select(JobDispatchOutbox).where(
                    JobDispatchOutbox.step_id == step_ids["validate"]
                )
            )
        )
    assert len(rows) == 1


def test_publisher_failure_releases_claim_and_can_be_retried(
    app_session_factory: sessionmaker[Session], project, users
) -> None:
    with app_session_factory.begin() as session:
        _persisted_job(session, project.id, users["operator"].id)
    now = datetime.now(UTC) + timedelta(seconds=1)

    def unavailable(**_message):
        raise ConnectionError("broker URI and credentials must not be persisted")

    assert publish_outbox(app_session_factory, unavailable, now=now) == 0
    with app_session_factory() as session:
        rows = list(session.scalars(select(JobDispatchOutbox)))
        assert all(row.claim_token is None and row.published_at is None for row in rows)
        assert all(row.publish_attempts == 1 for row in rows)
        assert all(row.failure_code == "broker_unavailable" for row in rows)
        assert all("URI" not in (row.failure_summary or "") for row in rows)

    sent: list[dict[str, object]] = []
    future = now + timedelta(minutes=1)
    assert publish_outbox(
        app_session_factory, lambda **message: sent.append(message), now=future
    ) == 2
    assert all(set(message) == {"task_name", "args", "queue"} for message in sent)
    with app_session_factory() as session:
        outbox_ids = {str(value) for value in session.scalars(select(JobDispatchOutbox.id))}
    assert {message["args"][0] for message in sent} == outbox_ids


def test_expired_lease_recovery_requeues_through_outbox_and_fences_old_worker(
    app_session_factory: sessionmaker[Session], project, users
) -> None:
    now = datetime.now(UTC)
    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)
        lease = acquire_step_lease(
            session, step_ids["rule_text"], now=now, lease_for=timedelta(seconds=1)
        )

    assert recover_expired_leases(app_session_factory, now=now + timedelta(seconds=2)) == 1
    with app_session_factory() as session:
        step = session.get(JobStep, step_ids["rule_text"])
        assert step is not None
        assert step.status == "queued"
        assert step.lease_token is None
        assert step.failure_code == "worker_lost"
        dispatch = session.scalar(
            select(JobDispatchOutbox)
            .where(JobDispatchOutbox.step_id == step.id)
            .order_by(JobDispatchOutbox.dispatch_generation.desc())
        )
        assert dispatch is not None and dispatch.dispatch_generation == step.version

    with app_session_factory.begin() as session:
        with pytest.raises(LeaseNotAcquired):
            finish_step_lease(session, lease, "completed")


def test_heartbeat_renews_only_the_current_fenced_lease(
    app_session_factory: sessionmaker[Session], project, users
) -> None:
    now = datetime.now(UTC)
    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)
        lease = acquire_step_lease(
            session, step_ids["rule_text"], now=now, lease_for=timedelta(seconds=1)
        )
    with app_session_factory.begin() as session:
        renewed = renew_step_lease(
            session,
            lease,
            now=now + timedelta(milliseconds=500),
            lease_for=timedelta(seconds=2),
        )

    assert renewed.expires_at == now + timedelta(milliseconds=2500)
    assert recover_expired_leases(
        app_session_factory, now=now + timedelta(seconds=2)
    ) == 0


def test_total_attempt_limit_is_enforced_by_database_state(
    app_session_factory: sessionmaker[Session], project, users
) -> None:
    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)
        session.execute(
            update(JobStep)
            .where(JobStep.id == step_ids["rule_text"])
            .values(attempt_count=MAX_ATTEMPTS)
        )
    with app_session_factory.begin() as session:
        with pytest.raises(LeaseNotAcquired, match="attempt"):
            acquire_step_lease(session, step_ids["rule_text"])


def test_adapter_result_is_validated_at_runtime(tmp_path: Path) -> None:
    context = AdapterContext(work_dir=tmp_path)
    outside = tmp_path.parent / "outside-manifest.json"
    outside.write_text("{}", encoding="utf-8")

    with pytest.raises(PermanentAdapterError, match="manifest"):
        validate_adapter_result(
            context, AdapterResult(manifest_path=outside, metrics={"records": 1})
        )
    manifest = context.output_path("manifest.json")
    manifest.write_text("{}", encoding="utf-8")
    with pytest.raises(PermanentAdapterError, match="metrics"):
        validate_adapter_result(
            context, AdapterResult(manifest_path=manifest, metrics={"records": -1})
        )


def _file_factory(tmp_path: Path) -> tuple[sessionmaker[Session], UUID, dict[str, UUID]]:
    engine = create_engine(
        f"sqlite:///{tmp_path / f'{uuid4()}.sqlite'}",
        connect_args={"timeout": 10},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    creator_id = uuid4()
    with factory.begin() as session:
        project = Project(name=f"concurrency-{uuid4()}")
        session.add(project)
        session.flush()
        job_id, step_ids = _persisted_job(session, project.id, creator_id)
    return factory, job_id, step_ids


def test_parent_lock_queries_compile_to_postgresql_for_update() -> None:
    job_id, batch_id = uuid4(), uuid4()
    statements = []

    class CaptureSession:
        def scalar(self, statement):
            statements.append(statement)
            if len(statements) == 1:
                return SimpleNamespace(id=job_id, batch_id=batch_id)
            return SimpleNamespace(id=batch_id)

    state.lock_step_parents(CaptureSession(), job_id)  # type: ignore[arg-type]
    compiled = [
        str(statement.compile(dialect=postgresql.dialect())) for statement in statements
    ]
    assert all("FOR UPDATE" in sql for sql in compiled)


def test_two_sessions_start_and_finish_siblings_with_consistent_final_aggregation(
    tmp_path: Path,
) -> None:
    factory, job_id, step_ids = _file_factory(tmp_path)
    generator_ids = [step_ids["rule_text"], step_ids["llm_table"]]
    barrier = threading.Barrier(2)
    leases = []
    errors: list[Exception] = []

    def start(step_id: UUID) -> None:
        try:
            barrier.wait()
            with factory.begin() as session:
                leases.append(acquire_step_lease(session, step_id))
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=start, args=(step_id,)) for step_id in generator_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(leases) == 2

    barrier = threading.Barrier(2)

    def finish(lease) -> None:
        try:
            barrier.wait()
            with factory.begin() as session:
                finish_step_lease(session, lease, "completed", result_json={"metrics": {}})
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=finish, args=(lease,)) for lease in leases]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    with factory() as session:
        job = session.get(job_tasks.DocumentJob, job_id)
        merge_events = list(
            session.scalars(
                select(JobDispatchOutbox).where(
                    JobDispatchOutbox.step_id == step_ids["merge"]
                )
            )
        )
        assert job is not None and job.status == "running"
        assert len(merge_events) == 1


def test_two_publishers_claim_each_logical_dispatch_once(tmp_path: Path) -> None:
    factory, _job_id, step_ids = _file_factory(tmp_path)
    sent: list[str] = []
    sent_lock = threading.Lock()
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def sender(**message):
        with sent_lock:
            sent.append(message["args"][0])

    def publish() -> None:
        try:
            barrier.wait()
            publish_outbox(
                factory,
                sender,
                now=datetime.now(UTC) + timedelta(seconds=1),
            )
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=publish) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    with factory() as session:
        outbox_ids = sorted(str(value) for value in session.scalars(select(JobDispatchOutbox.id)))
    assert sorted(sent) == outbox_ids


def test_stale_dispatch_generation_cannot_consume_a_new_retry_generation(
    app_session_factory: sessionmaker[Session], project, users, tmp_path: Path, monkeypatch
) -> None:
    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)
        original_dispatch_id = session.scalar(
            select(JobDispatchOutbox.id).where(
                JobDispatchOutbox.step_id == step_ids["rule_text"]
            )
        )
    with pytest.raises(StepExecutionFailed):
        execute_step_once(
            step_ids["rule_text"],
            factory=app_session_factory,
            work_root=tmp_path,
            adapters={"rule_text": _RetryingAdapter()},
        )
    monkeypatch.setattr(job_tasks, "get_session_factory", lambda: app_session_factory)
    monkeypatch.setenv("EXTRACTION_WORK_ROOT", str(tmp_path))

    result = execute_step.apply(args=[str(original_dispatch_id)])

    assert result.successful()
    with app_session_factory() as session:
        step = session.get(JobStep, step_ids["rule_text"])
        cleanup_rows = list(session.scalars(select(JobCleanupOutbox)))
        assert step is not None
        assert step.status == "queued"
        assert step.attempt_count == 1
        assert len(cleanup_rows) == 1


def test_three_total_failed_attempts_close_the_database_retry_budget(
    app_session_factory: sessionmaker[Session], project, users, tmp_path: Path
) -> None:
    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)
        step_id = step_ids["rule_text"]
    for attempt in range(1, MAX_ATTEMPTS + 1):
        with pytest.raises(StepExecutionFailed):
            execute_step_once(
                step_id,
                factory=app_session_factory,
                work_root=tmp_path,
                adapters={"rule_text": _RetryingAdapter()},
            )
        with app_session_factory() as session:
            step = session.get(JobStep, step_id)
            assert step is not None and step.attempt_count == attempt
            expected = "queued" if attempt < MAX_ATTEMPTS else "permanent_failed"
            assert step.status == expected
    with app_session_factory.begin() as session:
        with pytest.raises(LeaseNotAcquired, match="attempt|ready"):
            acquire_step_lease(session, step_id)


@pytest.mark.parametrize(
    "values",
    [
        {"attempt_count": -1},
        {"lease_token": "token", "lease_expires_at": None},
        {
            "status": "queued",
            "lease_token": "token",
            "lease_expires_at": datetime.now(UTC),
        },
    ],
)
def test_database_rejects_invalid_attempt_and_lease_states(
    app_session_factory: sessionmaker[Session], project, users, values
) -> None:
    with app_session_factory.begin() as setup:
        _, step_ids = _persisted_job(setup, project.id, users["operator"].id)
    with app_session_factory() as session:
        with pytest.raises(IntegrityError):
            session.execute(
                update(JobStep)
                .where(JobStep.id == step_ids["rule_text"])
                .values(**values)
            )
            session.commit()


def test_real_celery_eager_execution_uses_worker_safety_flags(
    app_session_factory: sessionmaker[Session], project, users, tmp_path: Path, monkeypatch
) -> None:
    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)
        dispatch_id = session.scalar(
            select(JobDispatchOutbox.id).where(
                JobDispatchOutbox.step_id == step_ids["rule_text"]
            )
        )
    monkeypatch.setattr(job_tasks, "get_session_factory", lambda: app_session_factory)
    monkeypatch.setenv("EXTRACTION_WORK_ROOT", str(tmp_path))
    previous = celery_app.conf.task_always_eager
    celery_app.conf.task_always_eager = True
    try:
        result = execute_step.apply(args=[str(dispatch_id)])
    finally:
        celery_app.conf.task_always_eager = previous

    assert result.successful()
    assert result.result is None
    assert execute_step.acks_late is True
    assert execute_step.reject_on_worker_lost is True
    assert execute_step.ignore_result is True


def test_celery_rule_and_llm_queues_use_distinct_direct_routes() -> None:
    rule = celery_app.amqp.queues["rule"]
    llm = celery_app.amqp.queues["llm"]
    assert (rule.exchange.name, rule.routing_key) == ("rule", "rule")
    assert (llm.exchange.name, llm.routing_key) == ("llm", "llm")


def test_real_celery_eager_publisher_retries_are_bounded(monkeypatch) -> None:
    attempts = 0

    def unavailable(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(job_tasks, "publish_outbox", unavailable)
    monkeypatch.setattr(job_tasks, "get_session_factory", lambda: None)
    previous_eager = celery_app.conf.task_always_eager
    previous_propagates = celery_app.conf.task_eager_propagates
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = False
    try:
        result = job_tasks.publish_dispatch_outbox.apply()
    finally:
        celery_app.conf.task_always_eager = previous_eager
        celery_app.conf.task_eager_propagates = previous_propagates

    assert result.failed()
    assert attempts == 3


def test_terminal_state_rolls_back_when_outbox_staging_fails(
    app_session_factory: sessionmaker[Session], project, users, monkeypatch
) -> None:
    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)
        lease = acquire_step_lease(session, step_ids["rule_text"])

    def crash_window(*_args, **_kwargs):
        raise RuntimeError("simulated database failure before commit")

    monkeypatch.setattr(job_tasks, "stage_ready_dispatches_for_job", crash_window)
    with pytest.raises(RuntimeError, match="before commit"):
        with app_session_factory.begin() as session:
            finish_step_lease(session, lease, "completed", result_json={"metrics": {}})

    with app_session_factory() as session:
        step = session.get(JobStep, step_ids["rule_text"])
        assert step is not None
        assert step.status == "running"
        assert step.lease_token == lease.token


def test_database_reload_failure_is_sanitized_and_releases_the_lease(
    app_session_factory: sessionmaker[Session], project, users, tmp_path: Path, monkeypatch
) -> None:
    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)

    def database_load_failure(*_args, **_kwargs):
        raise RuntimeError("postgresql://user:secret@host/database")

    monkeypatch.setattr(job_tasks, "selectinload", database_load_failure)
    with pytest.raises(StepExecutionFailed) as raised:
        execute_step_once(
            step_ids["rule_text"],
            factory=app_session_factory,
            work_root=tmp_path,
        )

    assert raised.value.__cause__ is None
    with app_session_factory() as session:
        step = session.get(JobStep, step_ids["rule_text"])
        assert step is not None
        assert step.status == "permanent_failed"
        assert step.lease_token is None
        assert step.failure_code == "adapter_failed"
        assert "secret" not in (step.failure_summary or "")


def test_task7_migration_contains_outbox_and_lease_constraints() -> None:
    migration = (
        Path(__file__).parents[2] / "alembic/versions/0005_step_leases.py"
    ).read_text(encoding="utf-8")

    assert "job_dispatch_outbox" in migration
    assert "uq_job_dispatch_step_generation" in migration
    assert "ck_job_step_attempt_count" in migration
    assert "ck_job_step_lease_pair" in migration
    assert "status = 'running' AND lease_token IS NOT NULL" in migration
    assert "job_cleanup_outbox" in migration
    assert "uq_job_cleanup_attempt_token" in migration
    assert "failure_summary" in migration
    assert "result_json" in migration
    assert set(celery_app.conf.beat_schedule) == {
        "publish-job-dispatch-outbox",
        "recover-expired-step-leases",
        "retry-pending-workdir-cleanup",
        "sweep-job-artifacts",
        "retry-pending-storage-cleanup",
    }


def test_scanner_does_not_recover_a_lease_renewed_after_initial_scan(
    app_session_factory: sessionmaker[Session], project, users, monkeypatch
) -> None:
    now = datetime.now(UTC)
    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)
        lease = acquire_step_lease(
            session, step_ids["rule_text"], now=now, lease_for=timedelta(seconds=1)
        )
    original = job_tasks.recover_step_lease
    renewed = False

    def renew_between_scan_and_recovery(factory, observed, *, recovery_now):
        nonlocal renewed
        if not renewed:
            with factory.begin() as heartbeat_session:
                renew_step_lease(
                    heartbeat_session,
                    lease,
                    now=now + timedelta(milliseconds=1500),
                    lease_for=timedelta(seconds=10),
                )
            renewed = True
        return original(factory, observed, recovery_now=recovery_now)

    monkeypatch.setattr(job_tasks, "recover_step_lease", renew_between_scan_and_recovery)

    assert recover_expired_leases(
        app_session_factory, now=now + timedelta(seconds=2)
    ) == 0
    with app_session_factory() as session:
        step = session.get(JobStep, step_ids["rule_text"])
        assert step is not None and step.status == "running"
        assert step.lease_token == lease.token


def test_scanner_continues_after_one_lease_loses_the_recovery_race(
    app_session_factory: sessionmaker[Session], project, users, monkeypatch
) -> None:
    now = datetime.now(UTC)
    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)
        for kind in ("rule_text", "llm_table"):
            acquire_step_lease(
                session,
                step_ids[kind],
                now=now,
                lease_for=timedelta(seconds=1),
            )
    original = job_tasks.recover_step_lease
    calls = 0

    def lose_first(factory, observed, *, recovery_now):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise LeaseNotAcquired("recovery race lost")
        return original(factory, observed, recovery_now=recovery_now)

    monkeypatch.setattr(job_tasks, "recover_step_lease", lose_first)

    assert recover_expired_leases(
        app_session_factory, now=now + timedelta(seconds=2)
    ) == 1
    assert calls == 2


class _BrokenSession:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def scalar(self, *_args, **_kwargs):
        raise SQLAlchemyError("postgresql://user:secret@host/database SELECT private")

    get = scalar
    execute = scalar

    def rollback(self):
        pass

    def close(self):
        pass

    def commit(self):
        pass


class _BrokenFactory:
    def __call__(self):
        return _BrokenSession()


def test_control_plane_database_errors_are_fixed_and_unchained() -> None:
    with pytest.raises(ControlPlaneUnavailable) as dispatch_error:
        load_dispatch(_BrokenFactory(), uuid4())  # type: ignore[arg-type]
    with pytest.raises(ControlPlaneUnavailable) as scanner_error:
        recover_expired_leases(_BrokenFactory())  # type: ignore[arg-type]

    for error in (dispatch_error.value, scanner_error.value):
        assert error.code == "control_plane_unavailable"
        assert str(error) == "control plane temporarily unavailable"
        assert error.__cause__ is None
        assert "secret" not in str(error)


def test_running_requires_a_complete_lease_and_attempts_cannot_exceed_three(
    app_session_factory: sessionmaker[Session], project, users
) -> None:
    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)
    invalid_states = [
        {"status": "running", "lease_token": None, "lease_expires_at": None},
        {"attempt_count": MAX_ATTEMPTS + 1},
    ]
    for values in invalid_states:
        with app_session_factory() as session:
            with pytest.raises(IntegrityError):
                session.execute(
                    update(JobStep)
                    .where(JobStep.id == step_ids["rule_text"])
                    .values(**values)
                )
                session.commit()


def test_retry_propagates_the_same_external_idempotency_key(
    app_session_factory: sessionmaker[Session], project, users, tmp_path: Path
) -> None:
    observed: list[str] = []

    class ObserveThenRetry(FakeAdapter):
        def run(self, context, emit_progress):
            observed.append(context.execution_idempotency_key)
            raise RetryableAdapterError("temporary")

    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)
        expected = {
            step.kind: step.idempotency_key
            for step in session.scalars(
                select(JobStep).where(JobStep.id.in_(step_ids.values()))
            )
        }
    for _ in range(2):
        with pytest.raises(StepExecutionFailed):
            execute_step_once(
                step_ids["rule_text"],
                factory=app_session_factory,
                work_root=tmp_path,
                adapters={"rule_text": ObserveThenRetry()},
            )

    assert observed == [expected["rule_text"], expected["rule_text"]]
    assert expected["rule_text"] != expected["llm_table"]


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_adapter_metrics_reject_non_finite_values(tmp_path: Path, value: float) -> None:
    context = AdapterContext(
        work_dir=tmp_path, execution_idempotency_key="stable-execution-key"
    )
    manifest = context.output_path("manifest.json")
    manifest.write_text("{}", encoding="utf-8")

    with pytest.raises(PermanentAdapterError, match="metrics"):
        validate_adapter_result(
            context, AdapterResult(manifest_path=manifest, metrics={"records": value})
        )


def test_cleanup_failure_is_persisted_and_retryable(
    app_session_factory: sessionmaker[Session], project, users, tmp_path: Path, monkeypatch
) -> None:
    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)
    real_rmtree = job_tasks.shutil.rmtree

    def cleanup_fails(*_args, **_kwargs):
        raise OSError("C:/sensitive/work/path is locked")

    monkeypatch.setattr(job_tasks.shutil, "rmtree", cleanup_fails)
    execute_step_once(
        step_ids["rule_text"],
        factory=app_session_factory,
        work_root=tmp_path,
        adapters={"rule_text": FakeAdapter(name="rule_text")},
    )
    with app_session_factory() as session:
        step = session.get(JobStep, step_ids["rule_text"])
        cleanup = session.scalar(select(JobCleanupOutbox))
        assert step is not None and step.status == "completed"
        assert step.failure_code is None
        assert step.failure_summary is None
        assert cleanup is not None
        assert cleanup.step_id == step.id
        assert cleanup.attempt_number == 1
        assert cleanup.status == "pending"
        assert cleanup.retry_count == 0

    monkeypatch.setattr(job_tasks.shutil, "rmtree", real_rmtree)
    assert retry_pending_workdir_cleanup(app_session_factory, tmp_path) == 1
    with app_session_factory() as session:
        cleanup = session.scalar(select(JobCleanupOutbox))
        assert cleanup is not None and cleanup.status == "completed"


def test_permanent_failure_cleanup_does_not_overwrite_business_failure(
    app_session_factory: sessionmaker[Session], project, users, tmp_path: Path, monkeypatch
) -> None:
    class PermanentFailure(FakeAdapter):
        def run(self, context, emit_progress):
            raise PermanentAdapterError("provider rejected sensitive payload")

    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)
    real_rmtree = job_tasks.shutil.rmtree
    monkeypatch.setattr(
        job_tasks.shutil,
        "rmtree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("locked")),
    )
    with pytest.raises(StepExecutionFailed):
        execute_step_once(
            step_ids["rule_text"],
            factory=app_session_factory,
            work_root=tmp_path,
            adapters={"rule_text": PermanentFailure()},
        )
    monkeypatch.setattr(job_tasks.shutil, "rmtree", real_rmtree)
    assert retry_pending_workdir_cleanup(app_session_factory, tmp_path) == 1

    with app_session_factory() as session:
        step = session.get(JobStep, step_ids["rule_text"])
        cleanup = session.scalar(select(JobCleanupOutbox))
        assert step is not None and step.status == "permanent_failed"
        assert step.failure_code == "invalid_adapter_output"
        assert step.failure_summary == "adapter output or configuration invalid"
        assert cleanup is not None and cleanup.status == "completed"


def test_two_failed_attempt_cleanups_create_and_complete_two_records(
    app_session_factory: sessionmaker[Session], project, users, tmp_path: Path, monkeypatch
) -> None:
    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)
    real_rmtree = job_tasks.shutil.rmtree
    monkeypatch.setattr(
        job_tasks.shutil,
        "rmtree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("locked")),
    )
    for _ in range(2):
        with pytest.raises(StepExecutionFailed):
            execute_step_once(
                step_ids["rule_text"],
                factory=app_session_factory,
                work_root=tmp_path,
                adapters={"rule_text": _RetryingAdapter()},
            )
    with app_session_factory() as session:
        rows = list(
            session.scalars(
                select(JobCleanupOutbox).order_by(JobCleanupOutbox.attempt_number)
            )
        )
        assert [row.attempt_number for row in rows] == [1, 2]
        assert len({row.work_token for row in rows}) == 2

    monkeypatch.setattr(job_tasks.shutil, "rmtree", real_rmtree)
    assert retry_pending_workdir_cleanup(app_session_factory, tmp_path) == 2
    with app_session_factory() as session:
        assert set(session.scalars(select(JobCleanupOutbox.status))) == {"completed"}


def test_two_cleanup_workers_claim_each_record_once(tmp_path: Path) -> None:
    factory, _job_id, step_ids = _file_factory(tmp_path)
    records = []
    with factory.begin() as session:
        for attempt, step_id in enumerate(
            (step_ids["rule_text"], step_ids["llm_table"]), start=1
        ):
            token = uuid4().hex
            row = JobCleanupOutbox(
                step_id=step_id,
                attempt_number=attempt,
                work_token=token,
            )
            session.add(row)
            records.append((step_id, token))
    for step_id, token in records:
        target = tmp_path / str(step_id) / token
        target.mkdir(parents=True)
        (target / "artifact.tmp").write_text("temporary", encoding="utf-8")
    barrier = threading.Barrier(2)
    results: list[int] = []
    errors: list[Exception] = []

    def clean() -> None:
        try:
            barrier.wait()
            results.append(retry_pending_workdir_cleanup(factory, tmp_path))
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=clean) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sum(results) == 2
    with factory() as session:
        assert set(session.scalars(select(JobCleanupOutbox.status))) == {"completed"}


def test_execute_commit_database_error_is_fixed_and_unchained(
    app_session_factory: sessionmaker[Session], project, users, tmp_path: Path, monkeypatch
) -> None:
    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)

    def commit_fails(_session):
        raise SQLAlchemyError("postgresql://user:secret@host/database COMMIT")

    monkeypatch.setattr(Session, "commit", commit_fails)
    with pytest.raises(ControlPlaneUnavailable) as raised:
        execute_step_once(
            step_ids["rule_text"],
            factory=app_session_factory,
            work_root=tmp_path,
        )
    assert raised.value.__cause__ is None
    assert "secret" not in str(raised.value)


def test_cleanup_read_database_error_is_fixed_and_unchained(tmp_path: Path) -> None:
    with pytest.raises(ControlPlaneUnavailable) as raised:
        retry_pending_workdir_cleanup(_BrokenFactory(), tmp_path)  # type: ignore[arg-type]
    assert raised.value.__cause__ is None
    assert raised.value.code == "control_plane_unavailable"


@pytest.mark.parametrize(
    "operation",
    [
        lambda session, step_id, naive: acquire_step_lease(session, step_id, now=naive),
        lambda session, step_id, naive: renew_step_lease(
            session,
            StepLease(step_id, "token", 1, 1, naive),
            now=naive,
        ),
    ],
)
def test_public_lease_now_parameters_reject_naive_datetimes(
    app_session_factory: sessionmaker[Session], project, users, operation
) -> None:
    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)
        with pytest.raises(ValueError, match="timezone-aware"):
            operation(session, step_ids["rule_text"], datetime.now())


def test_scanner_now_rejects_naive_datetime(app_session_factory) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        recover_expired_leases(app_session_factory, now=datetime.now())


def test_lease_acquisition_atomically_preregisters_cleanup_attempt(
    app_session_factory: sessionmaker[Session], project, users
) -> None:
    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)
        lease = acquire_step_lease(session, step_ids["rule_text"])

    with app_session_factory() as session:
        cleanup = session.scalar(
            select(JobCleanupOutbox).where(JobCleanupOutbox.step_id == lease.step_id)
        )
        assert cleanup is not None
        assert lease.attempt_number == 1
        assert cleanup.attempt_number == lease.attempt_number
        assert cleanup.work_token == lease.token
        assert cleanup.status == "pending"


def test_hard_crash_directory_is_removed_from_preregistered_cleanup(
    app_session_factory: sessionmaker[Session], project, users, tmp_path: Path
) -> None:
    now = datetime.now(UTC)
    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)
        lease = acquire_step_lease(
            session, step_ids["rule_text"], now=now, lease_for=timedelta(seconds=1)
        )
    target = tmp_path / str(lease.step_id) / lease.token
    target.mkdir(parents=True)
    (target / "partial.tmp").write_text("hard crash", encoding="utf-8")

    assert retry_pending_workdir_cleanup(
        app_session_factory, tmp_path, now=now + timedelta(seconds=2)
    ) == 0
    assert target.exists()
    assert recover_expired_leases(
        app_session_factory, now=now + timedelta(seconds=2)
    ) == 1
    assert retry_pending_workdir_cleanup(
        app_session_factory, tmp_path, now=now + timedelta(seconds=2)
    ) == 1
    assert not target.exists()
    with app_session_factory() as session:
        cleanup = session.scalar(select(JobCleanupOutbox))
        assert cleanup is not None and cleanup.status == "completed"


def test_cleanup_waits_for_scanner_even_when_current_lease_is_expired_and_renewed(
    app_session_factory: sessionmaker[Session], project, users, tmp_path: Path
) -> None:
    now = datetime.now(UTC)
    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)
        lease = acquire_step_lease(
            session, step_ids["rule_text"], now=now, lease_for=timedelta(seconds=1)
        )
    target = tmp_path / str(lease.step_id) / lease.token
    target.mkdir(parents=True)
    (target / "active.tmp").write_text("still owned", encoding="utf-8")

    assert retry_pending_workdir_cleanup(
        app_session_factory, tmp_path, now=now + timedelta(seconds=2)
    ) == 0
    assert target.exists()
    with app_session_factory.begin() as session:
        renewed = renew_step_lease(
            session,
            lease,
            now=now + timedelta(seconds=2),
            lease_for=timedelta(seconds=2),
        )
    assert retry_pending_workdir_cleanup(
        app_session_factory, tmp_path, now=now + timedelta(seconds=3)
    ) == 0
    assert target.exists()

    assert recover_expired_leases(
        app_session_factory, now=now + timedelta(seconds=5)
    ) == 1
    assert retry_pending_workdir_cleanup(
        app_session_factory, tmp_path, now=now + timedelta(seconds=5)
    ) == 1
    assert not target.exists()


def test_delayed_attempt_one_finally_keeps_attempt_one_ownership(
    app_session_factory: sessionmaker[Session], project, users
) -> None:
    now = datetime.now(UTC)
    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)
        first = acquire_step_lease(
            session, step_ids["rule_text"], now=now, lease_for=timedelta(seconds=1)
        )
    assert recover_expired_leases(
        app_session_factory, now=now + timedelta(seconds=2)
    ) == 1
    with app_session_factory.begin() as session:
        second = acquire_step_lease(
            session, step_ids["rule_text"], now=now + timedelta(seconds=3)
        )
    complete_cleanup_attempt(app_session_factory, first)

    assert first.attempt_number == 1
    assert second.attempt_number == 2
    with app_session_factory() as session:
        rows = list(
            session.scalars(
                select(JobCleanupOutbox).order_by(JobCleanupOutbox.attempt_number)
            )
        )
        assert [(row.attempt_number, row.status) for row in rows] == [
            (1, "completed"),
            (2, "pending"),
        ]


def test_cleanup_preregistration_commit_failure_prevents_adapter_execution(
    app_session_factory: sessionmaker[Session], project, users, tmp_path: Path
) -> None:
    ran = False

    class MustNotRun(FakeAdapter):
        def run(self, context, emit_progress):
            nonlocal ran
            ran = True
            return super().run(context, emit_progress)

    with app_session_factory.begin() as session:
        _, step_ids = _persisted_job(session, project.id, users["operator"].id)

    def registration_fails(*_args, **_kwargs):
        raise SQLAlchemyError("postgresql://user:secret@host INSERT cleanup")

    event.listen(JobCleanupOutbox, "before_insert", registration_fails)
    try:
        with pytest.raises(ControlPlaneUnavailable) as raised:
            execute_step_once(
                step_ids["rule_text"],
                factory=app_session_factory,
                work_root=tmp_path,
                adapters={"rule_text": MustNotRun()},
            )
    finally:
        event.remove(JobCleanupOutbox, "before_insert", registration_fails)

    assert ran is False
    assert raised.value.__cause__ is None
    assert "secret" not in str(raised.value)


def test_rule_table_waits_for_rule_text_completion() -> None:
    job = _job(("rule_text", "queued"), ("rule_table", "queued"))

    queued = ready_steps(job)
    assert [step.kind for step in queued] == ["rule_text"]

    job.steps[0].status = "completed"
    assert [step.kind for step in ready_steps(job)] == ["rule_table"]

    job.steps[1].status = "completed"
    assert [step.kind for step in ready_steps(job)] == ["merge"]


def test_rule_table_runs_alone_when_no_rule_text_sibling() -> None:
    job = _job(("rule_table", "queued"), ("llm_table", "queued"))

    kinds = {step.kind for step in ready_steps(job)}
    assert kinds == {"rule_table", "llm_table"}
