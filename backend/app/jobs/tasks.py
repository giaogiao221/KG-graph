from __future__ import annotations

import json
import hashlib
import math
import os
import shutil
from io import BytesIO
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from typing import Literal, Protocol
from uuid import UUID, uuid4

from celery import Task
from sqlalchemy import exists, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.core.database import get_session_factory, session_scope
from app.extraction.contracts import (
    AdapterContext,
    AdapterError,
    ExtractionAdapter,
    PermanentAdapterError,
    RetryableAdapterError,
    validate_adapter_result,
)
from app.extraction.registry import ADAPTERS, LEGACY_ADAPTERS, execution_adapter_for
from app.extraction.registry import native_registry
from app.extraction.native.common import (
    MAX_CANDIDATE_ROWS,
    MAX_DOCUMENT_BYTES,
    MAX_ROUTE_TSV_BYTES,
    ROUTES,
    assert_no_links,
)
from app.documents.storage import (
    ArtifactOwnership,
    LocalStorage,
    Storage,
    StorageCollisionError,
    StorageOwnershipError,
)
from app.core.settings import get_settings
from app.facts.importer import import_facts
from app.facts.models import RawFact
from app.jobs import state
from app.jobs.celery_app import celery_app
from app.jobs.models import (
    DocumentJob,
    ExtractionBatch,
    JobCleanupOutbox,
    JobDispatchOutbox,
    JobArtifact,
    JobStep,
)
from app.models.client import DatabaseModelClient
from app.models.models import ModelConfig
from app.models.service import resolved_secret


MAX_ATTEMPTS = 3
LEASE_DURATION = timedelta(minutes=15)
HEARTBEAT_INTERVAL_SECONDS = 60.0
OUTBOX_CLAIM_TIMEOUT = timedelta(minutes=2)
FailureKind = Literal["retryable", "permanent"]


def _require_aware(value: datetime, name: str = "now") -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


class LeaseNotAcquired(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    artifact_id: UUID
    kind: Literal["manifest", "candidate_tsv"]
    storage_key: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ImportClaim:
    token: str
    artifact_ids: tuple[UUID, UUID]
    expires_at: datetime
    references: tuple[ArtifactReference, ArtifactReference] = ()
    source_step_id: UUID | None = None
    document_version_id: UUID | None = None
    execution_key: str | None = None

    def reference(self, kind: Literal["manifest", "candidate_tsv"]) -> ArtifactReference:
        try:
            return next(value for value in self.references if value.kind == kind)
        except StopIteration:
            raise PermanentAdapterError("artifact import claim is invalid") from None


class LeaseLost(RuntimeError):
    pass


class ControlPlaneUnavailable(RuntimeError):
    code = "control_plane_unavailable"

    def __init__(self):
        super().__init__("control plane temporarily unavailable")


class StepExecutionFailed(RuntimeError):
    def __init__(self, failure_kind: FailureKind):
        super().__init__(f"step execution {failure_kind} failure")
        self.failure_kind = failure_kind


class PostCommitCleanupError(RuntimeError):
    """Validate committed durably, but artifact cleanup could not be reconciled."""


class ArtifactPersistenceError(PermanentAdapterError):
    pass


@dataclass(frozen=True, slots=True)
class StepLease:
    step_id: UUID
    token: str
    version: int
    attempt_number: int
    expires_at: datetime


class StepLike(Protocol):
    id: UUID
    kind: str
    status: str
    stage: int
    position: int
    version: int


class JobLike(Protocol):
    steps: list[StepLike]


def ready_steps(job: JobLike) -> list[StepLike]:
    ordered = sorted(job.steps, key=lambda step: step.position)
    generators = [step for step in ordered if step.stage == 0]
    queued_generators = [step for step in generators if step.status == "queued"]
    rule_text = next((step for step in generators if step.kind == "rule_text"), None)
    if rule_text is not None and rule_text.status != "completed":
        # A queued/running rule_table step must wait for its rule_text sibling:
        # the table route reuses the text route's engine output and cannot start
        # first.  Remove it from this dispatch round.
        queued_generators = [
            step for step in queued_generators if step.kind != "rule_table"
        ]
    if queued_generators:
        return queued_generators
    # With rule_text completed, a queued rule_table becomes dispatchable.
    if rule_text is not None and rule_text.status == "completed":
        rule_table = next(
            (step for step in generators if step.kind == "rule_table"), None
        )
        if rule_table is not None and rule_table.status == "queued":
            return [rule_table]
    if not generators or any(step.status != "completed" for step in generators):
        return []
    merge = next((step for step in ordered if step.kind == "merge"), None)
    if merge is not None and merge.status == "queued":
        return [merge]
    if merge is None or merge.status != "completed":
        return []
    validate = next((step for step in ordered if step.kind == "validate"), None)
    return [validate] if validate is not None and validate.status == "queued" else []


def queue_for_step_kind(kind: str) -> Literal["rule", "llm"]:
    return execution_adapter_for(kind).queue


def _load_job(session: Session, job_id: UUID) -> DocumentJob | None:
    return session.scalar(
        select(DocumentJob)
        .options(selectinload(DocumentJob.steps))
        .where(DocumentJob.id == job_id)
    )


def stage_ready_dispatches_for_job(
    session: Session, job_id: UUID, *, now: datetime | None = None
) -> list[JobDispatchOutbox]:
    job = _load_job(session, job_id)
    if job is None:
        return []
    now = now or datetime.now(UTC)
    staged: list[JobDispatchOutbox] = []
    for step in ready_steps(job):
        existing = session.scalar(
            select(JobDispatchOutbox).where(
                JobDispatchOutbox.step_id == step.id,
                JobDispatchOutbox.dispatch_generation == step.version,
            )
        )
        if existing is not None:
            staged.append(existing)
            continue
        row = JobDispatchOutbox(
            step_id=step.id,
            dispatch_generation=step.version,
            queue=queue_for_step_kind(step.kind),
            available_at=now,
        )
        try:
            with session.begin_nested():
                session.add(row)
                session.flush()
        except IntegrityError:
            row = session.scalar(
                select(JobDispatchOutbox).where(
                    JobDispatchOutbox.step_id == step.id,
                    JobDispatchOutbox.dispatch_generation == step.version,
                )
            )
            if row is None:
                raise
        staged.append(row)
    return staged


def stage_ready_dispatches(
    session: Session, completed_step_id: UUID, *, now: datetime | None = None
) -> list[JobDispatchOutbox]:
    job_id = session.scalar(
        select(JobStep.document_job_id).where(JobStep.id == completed_step_id)
    )
    if job_id is None:
        return []
    return stage_ready_dispatches_for_job(session, job_id, now=now)


def _prerequisites_completed(session: Session, step: JobStep) -> bool:
    incomplete = session.scalar(
        select(JobStep.id)
        .where(
            JobStep.document_job_id == step.document_job_id,
            JobStep.stage < step.stage,
            JobStep.status != "completed",
        )
        .limit(1)
    )
    return incomplete is None


def _acquire_step_lease(
    session: Session,
    step_id: UUID,
    *,
    now: datetime | None = None,
    lease_for: timedelta = LEASE_DURATION,
    expected_generation: int | None = None,
) -> StepLease:
    if lease_for <= timedelta(0):
        raise ValueError("lease duration must be positive")
    now = _require_aware(now, "now") if now is not None else datetime.now(UTC)
    step = session.get(JobStep, step_id)
    if step is None:
        raise LeaseNotAcquired("step is missing")
    state.lock_step_parents(session, step.document_job_id)
    session.refresh(step)
    if expected_generation is not None and step.version != expected_generation:
        raise LeaseNotAcquired("dispatch generation is stale")
    if step.attempt_count >= MAX_ATTEMPTS:
        raise LeaseNotAcquired("step total attempt limit reached")
    if step.status != "queued" or not _prerequisites_completed(session, step):
        raise LeaseNotAcquired("step is not ready")
    token = uuid4().hex
    expires_at = now + lease_for
    result = session.execute(
        update(JobStep)
        .where(
            JobStep.id == step.id,
            JobStep.version == step.version,
            JobStep.status == "queued",
            JobStep.attempt_count < MAX_ATTEMPTS,
        )
        .values(
            status="running",
            version=step.version + 1,
            lease_token=token,
            lease_expires_at=expires_at,
            attempt_count=JobStep.attempt_count + 1,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise LeaseNotAcquired("step lease is already held")
    attempt_number = step.attempt_count + 1
    session.add(
        JobCleanupOutbox(
            step_id=step.id,
            attempt_number=attempt_number,
            work_token=token,
        )
    )
    state._aggregate_parents(session, step.document_job_id)
    return StepLease(
        step.id, token, step.version + 1, attempt_number, expires_at
    )


def acquire_step_lease(
    session: Session,
    step_id: UUID,
    *,
    now: datetime | None = None,
    lease_for: timedelta = LEASE_DURATION,
    expected_generation: int | None = None,
) -> StepLease:
    try:
        return _acquire_step_lease(
            session,
            step_id,
            now=now,
            lease_for=lease_for,
            expected_generation=expected_generation,
        )
    except (LeaseNotAcquired, ValueError):
        raise
    except SQLAlchemyError:
        raise ControlPlaneUnavailable() from None


def renew_step_lease(
    session: Session,
    lease: StepLease,
    *,
    now: datetime | None = None,
    lease_for: timedelta = LEASE_DURATION,
) -> StepLease:
    if lease_for <= timedelta(0):
        raise ValueError("lease duration must be positive")
    now = _require_aware(now, "now") if now is not None else datetime.now(UTC)
    expires_at = now + lease_for
    result = session.execute(
        update(JobStep)
        .where(
            JobStep.id == lease.step_id,
            JobStep.version == lease.version,
            JobStep.status == "running",
            JobStep.lease_token == lease.token,
        )
        .values(lease_expires_at=expires_at, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise LeaseLost("step lease was replaced")
    return replace(lease, expires_at=expires_at)


def classify_failure(error: Exception) -> FailureKind:
    if isinstance(error, (RetryableAdapterError, TimeoutError, ConnectionError)):
        return "retryable"
    if isinstance(error, (PermanentAdapterError, ValueError)):
        return "permanent"
    if isinstance(error, AdapterError):
        return error.failure_kind
    return "permanent"


def _failure_fields(error: Exception) -> tuple[FailureKind, str, str]:
    kind = classify_failure(error)
    public_summary = str(error).strip()
    if not public_summary.startswith("KGchouqu "):
        public_summary = ""
    if kind == "retryable":
        return kind, "dependency_unavailable", (public_summary or "temporary dependency failure")[:200]
    if isinstance(error, PermanentAdapterError):
        return kind, "invalid_adapter_output", (public_summary or "adapter output or configuration invalid")[:200]
    if isinstance(error, AdapterError):
        return kind, "adapter_failed", (public_summary or "adapter execution failed")[:200]
    return kind, "adapter_failed", "adapter execution failed"


def finish_step_lease(
    session: Session,
    lease: StepLease,
    status: str,
    *,
    result_json: dict[str, object] | None = None,
    failure_code: str | None = None,
    failure_summary: str | None = None,
    now: datetime | None = None,
    observed_expiry: datetime | None = None,
    expired_by: datetime | None = None,
) -> JobStep:
    now = _require_aware(now, "now") if now is not None else datetime.now(UTC)
    step = session.get(JobStep, lease.step_id)
    if step is None:
        raise LeaseNotAcquired("leased step no longer exists")
    state.lock_step_parents(session, step.document_job_id)
    session.refresh(step)
    if (
        step.status != "running"
        or step.version != lease.version
        or step.lease_token != lease.token
    ):
        raise LeaseNotAcquired("step lease was replaced")
    target = status
    if status == "retryable_failed":
        target = "queued" if step.attempt_count < MAX_ATTEMPTS else "permanent_failed"
    conditions = [
            JobStep.id == lease.step_id,
            JobStep.version == lease.version,
            JobStep.status == "running",
            JobStep.lease_token == lease.token,
    ]
    if observed_expiry is not None:
        conditions.append(JobStep.lease_expires_at == observed_expiry)
    if expired_by is not None:
        conditions.append(JobStep.lease_expires_at <= expired_by)
    result = session.execute(
        update(JobStep)
        .where(*conditions)
        .values(
            status=target,
            version=lease.version + 1,
            lease_token=None,
            lease_expires_at=None,
            failure_code=failure_code,
            failure_summary=failure_summary,
            result_json=result_json,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise LeaseNotAcquired("step lease was replaced")
    if target == "permanent_failed":
        session.execute(
            update(JobStep)
            .where(
                JobStep.document_job_id == step.document_job_id,
                JobStep.stage > step.stage,
                JobStep.status == "queued",
            )
            .values(
                status="cancelled",
                version=JobStep.version + 1,
                failure_code="upstream_failed",
                failure_summary=f"waiting for failed upstream step ({step.kind})",
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
    state._aggregate_parents(session, step.document_job_id)
    session.expire_all()
    if target in {"completed", "queued"}:
        stage_ready_dispatches_for_job(session, step.document_job_id, now=now)
    refreshed = session.get(JobStep, step.id)
    if refreshed is None:
        raise LeaseNotAcquired("step no longer exists")
    return refreshed


class LeaseHeartbeat(AbstractContextManager["LeaseHeartbeat"]):
    def __init__(
        self,
        factory: sessionmaker[Session],
        lease: StepLease,
        *,
        interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
        lease_for: timedelta = LEASE_DURATION,
    ):
        self.factory = factory
        self.lease = lease
        self.interval_seconds = interval_seconds
        self.lease_for = lease_for
        self.error: Exception | None = None
        self._stop = Event()
        self._thread = Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                with session_scope(self.factory) as session:
                    self.lease = renew_step_lease(
                        session, self.lease, lease_for=self.lease_for
                    )
            except Exception as error:
                self.error = error
                return

    def __enter__(self) -> "LeaseHeartbeat":
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        self._thread.join(timeout=max(self.interval_seconds, 1.0))


def _isolated_work_dir(root: Path, lease: StepLease) -> Path:
    resolved_root = root.resolve()
    target = (resolved_root / str(lease.step_id) / lease.token).resolve()
    if resolved_root not in target.parents:
        raise PermanentAdapterError("work directory escapes configured root")
    target.mkdir(parents=True, exist_ok=False)
    return target


def _reserve_write_artifact(
    factory: sessionmaker[Session],
    storage: Storage,
    step_id: UUID,
    artifact_kind: str,
    suffix: str,
    payload: bytes,
) -> JobArtifact:
    artifact_id = uuid4()
    owner_token = uuid4().hex
    key = storage.artifact_storage_key(owner_token, suffix)
    ownership = ArtifactOwnership(artifact_id, owner_token, key)
    with session_scope(factory) as session:
        row = JobArtifact(
            id=artifact_id,
            step_id=step_id,
            artifact_kind=artifact_kind,
            owner_token=owner_token,
            storage_key=key,
            status="reserved",
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        session.add(row)
        session.flush()
        version = row.version

    # Persist the writer fence before creating any filesystem state.
    with session_scope(factory) as session:
        changed = session.execute(
            update(JobArtifact)
            .where(
                JobArtifact.id == artifact_id,
                JobArtifact.owner_token == owner_token,
                JobArtifact.status == "reserved",
                JobArtifact.version == version,
            )
            .values(status="writing", version=version + 1, updated_at=datetime.now(UTC))
        )
        if changed.rowcount != 1:
            raise ArtifactPersistenceError("adapter artifact persistence failed")
    version += 1

    try:
        storage.create_artifact_ownership(ownership)
        # Refresh the fence immediately before the irreversible payload create.
        with session_scope(factory) as session:
            changed = session.execute(
                update(JobArtifact)
                .where(
                    JobArtifact.id == artifact_id,
                    JobArtifact.owner_token == owner_token,
                    JobArtifact.status == "writing",
                    JobArtifact.version == version,
                )
                .values(version=version + 1, updated_at=datetime.now(UTC))
            )
            if changed.rowcount != 1:
                raise ArtifactPersistenceError("adapter artifact persistence failed")
        version += 1
        stored = storage.save_owned_artifact(ownership, BytesIO(payload))
    except StorageCollisionError:
        with session_scope(factory) as session:
            session.execute(
                update(JobArtifact)
                .where(
                    JobArtifact.id == artifact_id,
                    JobArtifact.owner_token == owner_token,
                    JobArtifact.status == "writing",
                    JobArtifact.version == version,
                )
                .values(
                    status="ownership_failed",
                    version=version + 1,
                    updated_at=datetime.now(UTC),
                )
            )
        raise
    try:
        with session_scope(factory) as session:
            changed = session.execute(
                update(JobArtifact)
                .where(
                    JobArtifact.id == artifact_id,
                    JobArtifact.owner_token == owner_token,
                    JobArtifact.status == "writing",
                    JobArtifact.version == version,
                )
                .values(
                    status="ready",
                    size_bytes=stored.size_bytes,
                    sha256=stored.sha256,
                    version=version + 1,
                    updated_at=datetime.now(UTC),
                )
            )
            if changed.rowcount != 1:
                raise ArtifactPersistenceError("adapter artifact persistence failed")
            result = session.get(JobArtifact, artifact_id)
            assert result is not None
    except SQLAlchemyError:
        # A disconnect after COMMIT is ambiguous. Trust only the exact durable row.
        with factory() as session:
            result = session.get(JobArtifact, artifact_id)
            if not (
                result is not None
                and result.owner_token == owner_token
                and result.storage_key == key
                and result.status == "ready"
                and result.size_bytes == stored.size_bytes
                and result.sha256 == stored.sha256
            ):
                raise ArtifactPersistenceError(
                    "adapter artifact persistence failed"
                ) from None
    return result


def _mark_artifacts_referenced(
    session: Session, step_id: UUID, result: dict[str, object]
) -> None:
    for value in result.get("artifacts", {}).values():  # type: ignore[union-attr]
        artifact_id, key, size, sha256 = _artifact_reference(value, MAX_ROUTE_TSV_BYTES)
        changed = session.execute(
            update(JobArtifact)
            .where(
                JobArtifact.id == artifact_id,
                JobArtifact.step_id == step_id,
                JobArtifact.storage_key == key,
                JobArtifact.size_bytes == size,
                JobArtifact.sha256 == sha256,
                JobArtifact.status == "ready",
            )
            .values(status="referenced", version=JobArtifact.version + 1, updated_at=datetime.now(UTC))
        )
        if changed.rowcount != 1:
            raise ArtifactPersistenceError("adapter artifact persistence failed")


def _structured_result(
    context: AdapterContext,
    result: object,
    storage: Storage,
    factory: sessionmaker[Session],
) -> dict[str, object]:
    validated = validate_adapter_result(context, result)  # type: ignore[arg-type]
    try:
        manifest = json.loads(validated.manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise PermanentAdapterError("adapter manifest is invalid") from None
    if not isinstance(manifest, (dict, list)):
        raise PermanentAdapterError("adapter manifest is invalid")
    artifacts: dict[str, object] = {}
    route: str | None = None
    if isinstance(manifest, dict) and manifest.get("route") in ROUTES | {"merge"}:
        try:
            manifest_bytes = validated.manifest_path.read_bytes()
            candidate_name = manifest["candidate_tsv"]
            if not isinstance(candidate_name, str) or Path(candidate_name).name != candidate_name:
                raise ValueError
            candidate = validated.manifest_path.parent / candidate_name
            if candidate.is_symlink() or not candidate.is_file():
                raise ValueError
            candidate_bytes = candidate.read_bytes()
            if len(manifest_bytes) > 64_000 or len(candidate_bytes) > MAX_ROUTE_TSV_BYTES:
                raise ValueError
            manifest_bytes.decode("utf-8")
            candidate_bytes.decode("utf-8")
            route = manifest["route"]
            if context.step_id is None:
                raise ValueError
            manifest_object = _reserve_write_artifact(
                factory, storage, context.step_id, "manifest", ".json", manifest_bytes
            )
            candidate_object = _reserve_write_artifact(
                factory, storage, context.step_id, "candidate_tsv", ".tsv", candidate_bytes
            )
            artifacts = {
                "manifest": {"id": str(manifest_object.id), "key": manifest_object.storage_key, "size": manifest_object.size_bytes, "sha256": manifest_object.sha256},
                "candidate_tsv": {"id": str(candidate_object.id), "key": candidate_object.storage_key, "size": candidate_object.size_bytes, "sha256": candidate_object.sha256},
            }
            shared_name = manifest.get("shared_source_tsv")
            if shared_name is not None:
                if not isinstance(shared_name, str) or Path(shared_name).name != shared_name:
                    raise ValueError
                shared = validated.manifest_path.parent / shared_name
                if shared.is_symlink() or not shared.is_file():
                    raise ValueError
                shared_bytes = shared.read_bytes()
                if len(shared_bytes) > MAX_ROUTE_TSV_BYTES:
                    raise ValueError
                shared_object = _reserve_write_artifact(
                    factory, storage, context.step_id, "shared_source_tsv", ".tsv", shared_bytes
                )
                artifacts["shared_source_tsv"] = {"id": str(shared_object.id), "key": shared_object.storage_key, "size": shared_object.size_bytes, "sha256": shared_object.sha256}
        except Exception:
            raise ArtifactPersistenceError("adapter artifact persistence failed") from None
    return {
        "schema": "native-artifacts-v1",
        "route": route,
        "step_id": str(context.step_id) if context.step_id else None,
        "document_version_id": str(context.document_version_id) if context.document_version_id else None,
        "execution_key": context.execution_idempotency_key,
        "metrics": dict(validated.metrics),
        "artifacts": artifacts,
    }


def _artifact_reference(value: object, max_bytes: int) -> tuple[UUID, str, int, str]:
    if not isinstance(value, dict) or set(value) != {"id", "key", "size", "sha256"}:
        raise PermanentAdapterError("persisted adapter artifacts are invalid")
    artifact_id, key, size, expected = value["id"], value["key"], value["size"], value["sha256"]
    if (
        not isinstance(key, str)
        or not key
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
        or size > max_bytes
        or not isinstance(expected, str)
        or len(expected) != 64
    ):
        raise PermanentAdapterError("persisted adapter artifacts are invalid")
    try:
        return UUID(str(artifact_id)), key, size, expected
    except ValueError:
        raise PermanentAdapterError("persisted adapter artifacts are invalid") from None


def _persisted_artifact_keys(result: object) -> tuple[str, ...]:
    try:
        if not isinstance(result, dict) or not isinstance(result.get("artifacts"), dict):
            return ()
        artifacts = result["artifacts"]
        if not artifacts:
            return ()
        manifest = _artifact_reference(artifacts["manifest"], 64_000)
        candidate = _artifact_reference(artifacts["candidate_tsv"], MAX_ROUTE_TSV_BYTES)
        return manifest[1], candidate[1]
    except (KeyError, TypeError):
        raise PermanentAdapterError("persisted adapter artifacts are invalid") from None


def _claim_artifact_cleanup(
    factory: sessionmaker[Session],
    *,
    now: datetime,
    grace: timedelta,
    claim_timeout: timedelta = OUTBOX_CLAIM_TIMEOUT,
    excluded_ids: set[UUID] | None = None,
) -> tuple[ArtifactOwnership, str, str] | None:
    token = f"expire-{uuid4().hex}"
    with session_scope(factory) as session:
        query = select(JobArtifact).where(
                or_(
                    (
                        (JobArtifact.status == "cleanup_failed")
                        & JobArtifact.claim_token.like("expire-%")
                    ),
                    ((JobArtifact.status == "ready") & (JobArtifact.created_at <= now - grace)),
                    ((JobArtifact.status == "writing") & (JobArtifact.updated_at <= now - grace)),
                    ((JobArtifact.status == "referenced") & (JobArtifact.expires_at <= now)),
                    ((JobArtifact.status == "expiring") & (JobArtifact.claimed_at <= now - claim_timeout)),
                )
            )
        if excluded_ids:
            query = query.where(JobArtifact.id.not_in(excluded_ids))
        row = session.scalar(
            query
            .order_by(JobArtifact.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return None
        changed = session.execute(
            update(JobArtifact)
            .where(JobArtifact.id == row.id, JobArtifact.version == row.version, JobArtifact.status == row.status)
            .values(
                status="expiring",
                claim_token=token,
                claimed_at=now,
                version=row.version + 1,
                updated_at=now,
            )
        )
        if changed.rowcount != 1:
            return None
        return (
            ArtifactOwnership(row.id, row.owner_token, row.storage_key),
            token,
            row.status,
        )


def _expire_stale_reserved(
    factory: sessionmaker[Session],
    *,
    now: datetime,
    grace: timedelta,
    limit: int,
) -> int:
    expired = 0
    attempted: set[UUID] = set()
    for _ in range(limit):
        with session_scope(factory) as session:
            query = (
                select(JobArtifact)
                .where(
                    JobArtifact.status == "reserved",
                    JobArtifact.created_at <= now - grace,
                )
                .order_by(JobArtifact.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if attempted:
                query = query.where(JobArtifact.id.not_in(attempted))
            row = session.scalar(query)
            if row is None:
                break
            attempted.add(row.id)
            changed = session.execute(
                update(JobArtifact)
                .where(
                    JobArtifact.id == row.id,
                    JobArtifact.version == row.version,
                    JobArtifact.status == "reserved",
                )
                .values(
                    status="expired",
                    version=row.version + 1,
                    updated_at=now,
                )
            )
            if changed.rowcount == 1:
                expired += 1
    return expired


def _claim_stale_import(
    factory: sessionmaker[Session],
    *,
    now: datetime,
    claim_timeout: timedelta,
    excluded_ids: set[UUID],
) -> tuple[ArtifactOwnership, str, int] | None:
    token = f"import-recovery-{uuid4().hex}"
    with session_scope(factory) as session:
        query = (
            select(JobArtifact)
            .where(
                JobArtifact.status == "importing",
                JobArtifact.claimed_at <= now - claim_timeout,
            )
            .order_by(JobArtifact.claimed_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if excluded_ids:
            query = query.where(JobArtifact.id.not_in(excluded_ids))
        row = session.scalar(query)
        if row is None:
            return None
        old_version = row.version
        changed = session.execute(
            update(JobArtifact)
            .where(
                JobArtifact.id == row.id,
                JobArtifact.version == old_version,
                JobArtifact.status == "importing",
                JobArtifact.claim_token == row.claim_token,
            )
            .values(
                claim_token=token,
                claimed_at=now,
                version=old_version + 1,
                updated_at=now,
            )
        )
        if changed.rowcount != 1:
            return None
        return ArtifactOwnership(row.id, row.owner_token, row.storage_key), token, old_version + 1


def _recover_stale_imports(
    factory: sessionmaker[Session],
    storage: Storage,
    *,
    now: datetime,
    claim_timeout: timedelta,
    limit: int,
) -> int:
    recovered_cleanup = 0
    attempted: set[UUID] = set()
    for _ in range(limit):
        claimed = _claim_stale_import(
            factory,
            now=now,
            claim_timeout=claim_timeout,
            excluded_ids=attempted,
        )
        if claimed is None:
            break
        ownership, token, version = claimed
        artifact_id, key = ownership.artifact_id, ownership.storage_key
        attempted.add(artifact_id)
        target: str | None = None
        try:
            storage.verify_artifact_ownership(ownership)
            with storage.open_read(key):
                object_exists = True
        except StorageOwnershipError:
            try:
                storage.delete_owned_artifact(ownership)
            except StorageOwnershipError:
                target = "ownership_failed"
            except Exception:
                target = "cleanup_failed"
            else:
                target = "imported_cleanup"
            object_exists = False
        except ValueError:
            object_exists = False
        except Exception:
            continue
        if target is None and object_exists:
            target = "referenced"
        elif target is None:
            try:
                storage.delete_owned_artifact(ownership)
            except StorageOwnershipError:
                target = "ownership_failed"
            except Exception:
                target = "cleanup_failed"
            else:
                target = "imported_cleanup"
        assert target is not None
        terminal_committed = False
        with session_scope(factory) as session:
            clear_claim = target in {
                "referenced",
                "imported_cleanup",
                "ownership_failed",
            }
            changed = session.execute(
                update(JobArtifact)
                .where(
                    JobArtifact.id == artifact_id,
                    JobArtifact.version == version,
                    JobArtifact.status == "importing",
                    JobArtifact.claim_token == token,
                )
                .values(
                    status=target,
                    claim_token=None if clear_claim else token,
                    claimed_at=None if clear_claim else now,
                    version=version + 1,
                    updated_at=now,
                )
            )
            if changed.rowcount == 1 and target == "imported_cleanup":
                recovered_cleanup += 1
                terminal_committed = True
        if terminal_committed:
            _best_effort_finalize_artifact(storage, ownership)
    return recovered_cleanup


def _claim_stale_import_cleanup(
    factory: sessionmaker[Session],
    *,
    now: datetime,
    claim_timeout: timedelta,
    excluded_ids: set[UUID],
) -> tuple[ArtifactOwnership, str, int] | None:
    token = f"import-cleanup-recovery-{uuid4().hex}"
    with session_scope(factory) as session:
        query = (
            select(JobArtifact)
            .where(
                JobArtifact.status.in_(("import_cleanup", "cleanup_failed")),
                JobArtifact.claim_token.like("import-%"),
                JobArtifact.claimed_at <= now - claim_timeout,
            )
            .order_by(JobArtifact.claimed_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if excluded_ids:
            query = query.where(JobArtifact.id.not_in(excluded_ids))
        row = session.scalar(query)
        if row is None:
            return None
        old_version = row.version
        changed = session.execute(
            update(JobArtifact)
            .where(
                JobArtifact.id == row.id,
                JobArtifact.version == old_version,
                JobArtifact.status == row.status,
                JobArtifact.claim_token == row.claim_token,
            )
            .values(
                status="import_cleanup",
                claim_token=token,
                claimed_at=now,
                version=old_version + 1,
                updated_at=now,
            )
        )
        if changed.rowcount != 1:
            return None
        return ArtifactOwnership(row.id, row.owner_token, row.storage_key), token, old_version + 1


def _recover_stale_import_cleanup(
    factory: sessionmaker[Session],
    storage: Storage,
    *,
    now: datetime,
    claim_timeout: timedelta,
    limit: int,
) -> int:
    completed = 0
    attempted: set[UUID] = set()
    for _ in range(limit):
        claimed = _claim_stale_import_cleanup(
            factory,
            now=now,
            claim_timeout=claim_timeout,
            excluded_ids=attempted,
        )
        if claimed is None:
            break
        ownership, token, version = claimed
        artifact_id = ownership.artifact_id
        attempted.add(artifact_id)
        try:
            storage.delete_owned_artifact(ownership)
        except StorageOwnershipError:
            target = "ownership_failed"
        except Exception:
            target = "cleanup_failed"
        else:
            target = "imported_cleanup"
        terminal_committed = False
        with session_scope(factory) as session:
            values: dict[str, object] = {
                "status": target,
                "version": version + 1,
                "updated_at": datetime.now(UTC),
            }
            if target in {"imported_cleanup", "ownership_failed"}:
                values.update(claim_token=None, claimed_at=None)
            changed = session.execute(
                update(JobArtifact)
                .where(
                    JobArtifact.id == artifact_id,
                    JobArtifact.version == version,
                    JobArtifact.status == "import_cleanup",
                    JobArtifact.claim_token == token,
                )
                .values(**values)
            )
            if changed.rowcount == 1 and target == "imported_cleanup":
                completed += 1
                terminal_committed = True
        if terminal_committed:
            _best_effort_finalize_artifact(storage, ownership)
    return completed


def _best_effort_finalize_artifact(
    storage: Storage, ownership: ArtifactOwnership
) -> None:
    try:
        storage.finalize_owned_artifact_cleanup(ownership)
    except Exception:
        pass


def _finalize_terminal_artifacts(
    factory: sessionmaker[Session], storage: Storage, *, limit: int
) -> None:
    with factory() as session:
        rows = tuple(
            session.scalars(
                select(JobArtifact)
                .where(JobArtifact.status.in_(("expired", "imported_cleanup")))
                .order_by(JobArtifact.updated_at)
                .limit(limit)
            )
        )
    for row in rows:
        _best_effort_finalize_artifact(
            storage, ArtifactOwnership(row.id, row.owner_token, row.storage_key)
        )


def sweep_job_artifacts(
    factory: sessionmaker[Session], storage: Storage, *, now: datetime | None = None,
    grace: timedelta = timedelta(minutes=5), limit: int = 100,
) -> int:
    now = _require_aware(now, "now") if now is not None else datetime.now(UTC)
    _finalize_terminal_artifacts(factory, storage, limit=min(limit, 20))
    _expire_stale_reserved(
        factory, now=now, grace=grace, limit=limit
    )
    completed = _recover_stale_import_cleanup(
        factory, storage, now=now, claim_timeout=OUTBOX_CLAIM_TIMEOUT, limit=limit
    )
    completed += _recover_stale_imports(
        factory, storage, now=now, claim_timeout=OUTBOX_CLAIM_TIMEOUT, limit=limit
    )
    attempted: set[UUID] = set()
    for _ in range(limit):
        try:
            claimed = _claim_artifact_cleanup(
                factory, now=now, grace=grace, excluded_ids=attempted
            )
        except SQLAlchemyError:
            raise ControlPlaneUnavailable() from None
        if claimed is None:
            break
        ownership, token, previous_status = claimed
        artifact_id = ownership.artifact_id
        attempted.add(artifact_id)
        try:
            storage.delete_owned_artifact(ownership)
        except StorageOwnershipError:
            target = "ownership_failed"
        except Exception:
            target = "cleanup_failed"
        else:
            target = "expired"
            completed += 1
        terminal_committed = False
        with session_scope(factory) as session:
            values: dict[str, object] = {
                "status": target,
                "version": JobArtifact.version + 1,
                "updated_at": datetime.now(UTC),
            }
            if target in {"expired", "ownership_failed"}:
                values.update(claim_token=None, claimed_at=None)
            changed = session.execute(
                update(JobArtifact)
                .where(JobArtifact.id == artifact_id, JobArtifact.status == "expiring", JobArtifact.claim_token == token)
                .values(**values)
            )
            terminal_committed = changed.rowcount == 1 and target == "expired"
        if terminal_committed:
            _best_effort_finalize_artifact(storage, ownership)
    return completed


def claim_artifacts_for_import(
    session: Session,
    step_id: UUID,
    *,
    now: datetime | None = None,
    claim_for: timedelta = OUTBOX_CLAIM_TIMEOUT,
) -> ImportClaim:
    now = _require_aware(now, "now") if now is not None else datetime.now(UTC)
    step = session.get(JobStep, step_id)
    try:
        if step is None or not isinstance(step.result_json, dict):
            raise ValueError
        result = step.result_json
        strict_merge = step.kind == "merge"
        if strict_merge:
            if (
                step.status != "completed"
                or set(result)
                != {
                    "schema", "route", "step_id", "document_version_id",
                    "execution_key", "metrics", "artifacts",
                }
                or result["schema"] != "native-artifacts-v1"
                or result["route"] != "merge"
                or result["step_id"] != str(step.id)
                or result["document_version_id"]
                != str(step.document_job.document_version_id)
                or result["execution_key"] != step.idempotency_key
                or not isinstance(result["metrics"], dict)
                or any(
                    not isinstance(key, str)
                    or not key
                    or isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value < 0
                    for key, value in result["metrics"].items()
                )
            ):
                raise ValueError
        artifacts = result["artifacts"]
        if not isinstance(artifacts, dict) or set(artifacts) != {"manifest", "candidate_tsv"}:
            raise ValueError
        references = {
            "manifest": _artifact_reference(artifacts["manifest"], 64_000),
            "candidate_tsv": _artifact_reference(
                artifacts["candidate_tsv"], MAX_ROUTE_TSV_BYTES
            ),
        }
        artifact_ids = tuple(reference[0] for reference in references.values())
        if len(set(artifact_ids)) != 2:
            raise ValueError
    except (KeyError, TypeError, ValueError, PermanentAdapterError):
        raise PermanentAdapterError("job artifacts are not importable")

    rows = {
        row.id: row
        for row in session.scalars(
            select(JobArtifact)
            .where(JobArtifact.id.in_(artifact_ids))
            .with_for_update()
        )
    }
    if len(rows) != 2:
        raise PermanentAdapterError("job artifacts are not importable")
    for kind, reference in references.items():
        row = rows.get(reference[0])
        if (
            row is None
            or row.step_id != step_id
            or row.artifact_kind != kind
            or row.status != "referenced"
            or (row.storage_key, row.size_bytes, row.sha256) != reference[1:]
        ):
            raise PermanentAdapterError("job artifacts are not importable")

    token = f"import-{uuid4().hex}"
    for row in rows.values():
        changed = session.execute(
            update(JobArtifact)
            .where(
                JobArtifact.id == row.id,
                JobArtifact.version == row.version,
                JobArtifact.status == "referenced",
                JobArtifact.claim_token.is_(None),
                JobArtifact.claimed_at.is_(None),
            )
            .values(
                status="importing",
                claim_token=token,
                claimed_at=now,
                version=row.version + 1,
                updated_at=now,
            )
        )
        if changed.rowcount != 1:
            raise PermanentAdapterError("job artifacts are not importable")
    return ImportClaim(
        token=token,
        artifact_ids=(artifact_ids[0], artifact_ids[1]),
        expires_at=now + claim_for,
        references=tuple(
            ArtifactReference(
                artifact_id=reference[0],
                kind=kind,  # type: ignore[arg-type]
                storage_key=reference[1],
                size_bytes=reference[2],
                sha256=reference[3],
            )
            for kind, reference in references.items()
        ),  # type: ignore[arg-type]
        source_step_id=step.id if strict_merge else None,
        document_version_id=(
            step.document_job.document_version_id if strict_merge else None
        ),
        execution_key=step.idempotency_key if strict_merge else None,
    )


def abort_artifact_import(session: Session, claim: ImportClaim) -> int:
    rows = tuple(
        session.scalars(
            select(JobArtifact)
            .where(JobArtifact.id.in_(claim.artifact_ids))
            .with_for_update()
        )
    )
    if (
        len(rows) != 2
        or {row.id for row in rows} != set(claim.artifact_ids)
        or any(
            row.status != "importing" or row.claim_token != claim.token
            for row in rows
        )
    ):
        raise PermanentAdapterError("artifact import claim is invalid")
    for row in rows:
        changed = session.execute(
            update(JobArtifact)
            .where(
                JobArtifact.id == row.id,
                JobArtifact.version == row.version,
                JobArtifact.status == "importing",
                JobArtifact.claim_token == claim.token,
            )
            .values(
                status="referenced",
                claim_token=None,
                claimed_at=None,
                version=row.version + 1,
                updated_at=datetime.now(UTC),
            )
        )
        if changed.rowcount != 1:
            raise PermanentAdapterError("artifact import claim is invalid")
    return 2


def _fence_import_cleanup(
    factory: sessionmaker[Session], claim: ImportClaim, *, now: datetime
) -> tuple[int, tuple[tuple[ArtifactOwnership, int], ...]]:
    pending: list[tuple[ArtifactOwnership, int]] = []
    completed = 0
    with session_scope(factory) as session:
        rows = {
            row.id: row
            for row in session.scalars(
                select(JobArtifact)
                .where(JobArtifact.id.in_(claim.artifact_ids))
                .with_for_update()
            )
        }
        if len(rows) != 2:
            raise PermanentAdapterError("artifact import claim is invalid")
        for artifact_id in claim.artifact_ids:
            row = rows[artifact_id]
            if row.status == "imported_cleanup":
                completed += 1
            elif (
                row.status in {"importing", "import_cleanup", "cleanup_failed"}
                and row.claim_token == claim.token
            ):
                old_version = row.version
                changed = session.execute(
                    update(JobArtifact)
                    .where(
                        JobArtifact.id == row.id,
                        JobArtifact.version == old_version,
                        JobArtifact.status == row.status,
                        JobArtifact.claim_token == claim.token,
                    )
                    .values(
                        status="import_cleanup",
                        claimed_at=now,
                        version=old_version + 1,
                        updated_at=now,
                    )
                )
                if changed.rowcount != 1:
                    raise PermanentAdapterError("artifact import claim is invalid")
                pending.append(
                    (
                        ArtifactOwnership(row.id, row.owner_token, row.storage_key),
                        old_version + 1,
                    )
                )
            else:
                raise PermanentAdapterError("artifact import claim is invalid")
    return completed, tuple(pending)


def complete_artifact_import(
    factory: sessionmaker[Session], claim: ImportClaim, storage: Storage
) -> int:
    completed, pending = _fence_import_cleanup(
        factory, claim, now=datetime.now(UTC)
    )

    for ownership, version in pending:
        artifact_id = ownership.artifact_id
        try:
            storage.delete_owned_artifact(ownership)
        except StorageOwnershipError:
            target = "ownership_failed"
        except Exception:
            target = "cleanup_failed"
        else:
            target = "imported_cleanup"
        terminal_committed = False
        with session_scope(factory) as session:
            values: dict[str, object] = {
                "status": target,
                "version": version + 1,
                "updated_at": datetime.now(UTC),
            }
            if target in {"imported_cleanup", "ownership_failed"}:
                values.update(claim_token=None, claimed_at=None)
            changed = session.execute(
                update(JobArtifact)
                .where(
                    JobArtifact.id == artifact_id,
                    JobArtifact.version == version,
                    JobArtifact.status == "import_cleanup",
                    JobArtifact.claim_token == claim.token,
                )
                .values(**values)
            )
            if changed.rowcount != 1:
                actual = session.get(JobArtifact, artifact_id)
                if actual is None or actual.status != "imported_cleanup":
                    raise PermanentAdapterError("artifact import claim is invalid")
            terminal_committed = target == "imported_cleanup"
        if target == "imported_cleanup":
            completed += 1
        if terminal_committed:
            _best_effort_finalize_artifact(storage, ownership)
    return completed


def _restore_route_artifacts(
    work_dir: Path,
    results: list[dict[str, object]],
    storage: Storage,
    factory: sessionmaker[Session],
) -> tuple[Path, ...]:
    paths = []
    for index, result in enumerate(results):
        try:
            if set(result) != {"schema", "route", "step_id", "document_version_id", "execution_key", "metrics", "artifacts"}:
                raise ValueError
            route = result["route"]
            if result["schema"] != "native-artifacts-v1" or route not in ROUTES:
                raise ValueError
            manifest_ref = _artifact_reference(result["artifacts"]["manifest"], 64_000)  # type: ignore[index]
            candidate_ref = _artifact_reference(result["artifacts"]["candidate_tsv"], MAX_ROUTE_TSV_BYTES)  # type: ignore[index]
            with factory() as session:
                rows = {
                    row.id: row
                    for row in session.scalars(
                        select(JobArtifact).where(
                            JobArtifact.id.in_([manifest_ref[0], candidate_ref[0]])
                        )
                    )
                }
            for reference, kind in ((manifest_ref, "manifest"), (candidate_ref, "candidate_tsv")):
                row = rows.get(reference[0])
                if (
                    row is None
                    or row.step_id != UUID(str(result["step_id"]))
                    or row.artifact_kind != kind
                    or row.status not in {"referenced", "importing"}
                    or (row.storage_key, row.size_bytes, row.sha256) != reference[1:]
                ):
                    raise ValueError
        except (KeyError, TypeError, ValueError):
            raise PermanentAdapterError("persisted adapter artifacts are invalid") from None
        target = (work_dir / "rebuilt-routes" / f"{index:02d}-{route}").resolve()
        if work_dir.resolve() not in target.parents:
            raise PermanentAdapterError("persisted adapter artifacts are invalid")
        target.mkdir(parents=True, exist_ok=False)
        manifest_path = target / "manifest.json"
        candidate_path = target / "candidates.schema59.tsv"
        try:
            storage.copy_to(manifest_ref[1], manifest_path, expected_size=manifest_ref[2], expected_sha256=manifest_ref[3], max_bytes=64_000)
            storage.copy_to(candidate_ref[1], candidate_path, expected_size=candidate_ref[2], expected_sha256=candidate_ref[3], max_bytes=MAX_ROUTE_TSV_BYTES)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (
                not isinstance(manifest, dict)
                or manifest.get("route") != route
                or manifest.get("candidate_tsv") != candidate_path.name
                or manifest.get("execution_idempotency_key") != result["execution_key"]
                or manifest.get("schema") != "schema59-v1"
                or manifest.get("step_id") != result["step_id"]
                or manifest.get("document_version_id") != result["document_version_id"]
            ):
                raise ValueError
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            raise PermanentAdapterError("persisted adapter artifacts are invalid") from None
        paths.append(manifest_path)
    return tuple(paths)


def _validated_sibling_results(
    siblings: list[object], document_version_id: UUID
) -> list[dict[str, object]]:
    validated: list[dict[str, object]] = []
    try:
        for sibling in siblings:
            value = sibling.result_json  # type: ignore[attr-defined]
            if (
                not isinstance(value, dict)
                or set(value) != {"schema", "route", "step_id", "document_version_id", "execution_key", "metrics", "artifacts"}
                or value["schema"] != "native-artifacts-v1"
                or value["route"] != sibling.kind  # type: ignore[attr-defined]
                or value["route"] not in ROUTES
                or value["step_id"] != str(sibling.id)  # type: ignore[attr-defined]
                or value["document_version_id"] != str(document_version_id)
                or value["execution_key"] != sibling.idempotency_key  # type: ignore[attr-defined]
                or not isinstance(value["artifacts"], dict)
                # Legacy rule_text steps persist an extra shared_source_tsv
                # artifact so the sibling rule_table step can reuse the engine
                # output without re-running it.
                or not {"manifest", "candidate_tsv"} <= set(value["artifacts"])
                or not set(value["artifacts"]) <= {"manifest", "candidate_tsv", "shared_source_tsv"}
            ):
                raise ValueError
            validated.append(value)
    except (AttributeError, KeyError, TypeError, ValueError):
        raise PermanentAdapterError("persisted adapter artifacts are invalid") from None
    return validated


def _restore_shared_source_tsv(
    work_dir: Path,
    rule_text_result: dict[str, object],
    storage: Storage,
    factory: sessionmaker[Session],
) -> Path:
    """Stage the sibling rule_text step's combined engine TSV for rule_table reuse."""
    try:
        if rule_text_result.get("schema") != "native-artifacts-v1" or rule_text_result.get("route") != "rule_text":
            raise ValueError
        reference = _artifact_reference(
            rule_text_result["artifacts"]["shared_source_tsv"], MAX_ROUTE_TSV_BYTES  # type: ignore[index]
        )
        with factory() as session:
            row = session.get(JobArtifact, reference[0])
        if (
            row is None
            or str(row.step_id) != str(rule_text_result["step_id"])
            or row.artifact_kind != "shared_source_tsv"
            or row.status not in {"referenced", "importing"}
            or (row.storage_key, row.size_bytes, row.sha256) != reference[1:]
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise PermanentAdapterError("persisted adapter artifacts are invalid") from None
    target_dir = (work_dir / "shared-source").resolve()
    if work_dir.resolve() not in target_dir.parents:
        raise PermanentAdapterError("persisted adapter artifacts are invalid")
    target_dir.mkdir(parents=True, exist_ok=False)
    target = target_dir / "shared_source_tsv.tsv"
    try:
        storage.copy_to(
            reference[1], target,
            expected_size=reference[2], expected_sha256=reference[3],
            max_bytes=MAX_ROUTE_TSV_BYTES,
        )
    except Exception:
        raise PermanentAdapterError("persisted adapter artifacts are invalid") from None
    return target


def _stage_document_input(*, storage: Storage, storage_key: str, expected_size: int, expected_sha256: str, work_dir: Path) -> Path:
    try:
        if expected_size < 0 or expected_size > MAX_DOCUMENT_BYTES:
            raise ValueError
        destination_dir = work_dir / "input"
        destination_dir.mkdir(parents=True, exist_ok=False)
        suffix = Path(storage_key).suffix if len(Path(storage_key).suffix) <= 16 else ""
        destination = destination_dir / f"document{suffix}"
        storage.copy_to(
            storage_key,
            destination,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
            max_bytes=MAX_DOCUMENT_BYTES,
        )
        return destination
    except (OSError, ValueError):
        raise PermanentAdapterError("document storage object is invalid") from None


_MERGE_MANIFEST_FIELDS = {
    "candidate_tsv",
    "document_version_id",
    "execution_idempotency_key",
    "records",
    "route",
    "schema",
    "step_id",
}


def _read_claimed_merge_manifest(
    path: Path, claim: ImportClaim, candidate_path: Path
) -> int:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        records = payload["records"]
        if (
            not isinstance(payload, dict)
            or set(payload) != _MERGE_MANIFEST_FIELDS
            or payload["schema"] != "schema59-v1"
            or payload["route"] != "merge"
            or payload["step_id"] != str(claim.source_step_id)
            or payload["document_version_id"] != str(claim.document_version_id)
            or payload["execution_idempotency_key"] != claim.execution_key
            or payload["candidate_tsv"] != candidate_path.name
            or isinstance(records, bool)
            or not isinstance(records, int)
            or records < 0
            or records > MAX_CANDIDATE_ROWS
        ):
            raise ValueError
        return records
    except (KeyError, OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise PermanentAdapterError("merge artifact manifest is invalid") from None


def _reconcile_validate_commit(
    factory: sessionmaker[Session],
    *,
    lease: StepLease,
    merge_step_id: UUID,
    expected_result: dict[str, object] | None,
) -> Literal["committed", "uncommitted", "unknown"]:
    try:
        with factory() as session:
            if session.get_bind().dialect.name == "postgresql":
                session.execute(text("SET LOCAL lock_timeout = '5s'"))
            step = session.scalar(
                select(JobStep)
                .where(JobStep.id == lease.step_id)
                .with_for_update()
            )
            if (
                expected_result is not None
                and step is not None
                and step.status == "completed"
                and step.lease_token is None
                and step.result_json == expected_result
                and step.result_json.get("source_step_id") == str(merge_step_id)
            ):
                imported = session.scalar(
                    select(func.count())
                    .select_from(RawFact)
                    .where(
                        RawFact.import_step_id == lease.step_id,
                        RawFact.source_step_id == merge_step_id,
                    )
                ) or 0
                inserted = expected_result.get("inserted")
                if isinstance(inserted, int) and not isinstance(inserted, bool) and imported >= inserted:
                    return "committed"
            if (
                step is not None
                and step.status == "running"
                and step.lease_token == lease.token
                and step.version == lease.version
            ):
                return "uncommitted"
            return "unknown"
    except SQLAlchemyError:
        return "unknown"


def _run_validate_import(
    *,
    step_id: UUID,
    merge_step_id: UUID,
    lease: StepLease,
    factory: sessionmaker[Session],
    storage: Storage,
    work_dir: Path,
) -> dict[str, object]:
    """Import a merge candidate under artifact ownership and step fencing."""

    claim: ImportClaim | None = None
    committed = False
    abort_allowed = True
    result: dict[str, object] | None = None
    try:
        with session_scope(factory) as session:
            claim = claim_artifacts_for_import(session, merge_step_id)

        import_dir = work_dir / "fact-import"
        import_dir.mkdir(parents=True, exist_ok=False)
        manifest_path = import_dir / "manifest.json"
        candidate_path = import_dir / "candidates.schema59.tsv"
        manifest_ref = claim.reference("manifest")
        candidate_ref = claim.reference("candidate_tsv")
        with LeaseHeartbeat(factory, lease) as heartbeat:
            storage.copy_to(
                manifest_ref.storage_key,
                manifest_path,
                expected_size=manifest_ref.size_bytes,
                expected_sha256=manifest_ref.sha256,
                max_bytes=64_000,
            )
            storage.copy_to(
                candidate_ref.storage_key,
                candidate_path,
                expected_size=candidate_ref.size_bytes,
                expected_sha256=candidate_ref.sha256,
                max_bytes=MAX_ROUTE_TSV_BYTES,
            )
            expected_records = _read_claimed_merge_manifest(
                manifest_path, claim, candidate_path
            )
            if heartbeat.error is not None:
                raise LeaseLost("step heartbeat lost its lease")
            try:
                with session_scope(factory) as session:
                    summary = import_facts(session, step_id, candidate_path)
                    if summary.total != expected_records:
                        raise PermanentAdapterError(
                            "merge artifact record count is invalid"
                        )
                    result = {
                        "schema": "facts-import-v1",
                        "source_step_id": str(merge_step_id),
                        "inserted": summary.inserted,
                        "duplicates": summary.duplicates,
                        "total": summary.total,
                    }
                    finish_step_lease(
                        session, heartbeat.lease, "completed", result_json=result
                    )
            except SQLAlchemyError:
                outcome = _reconcile_validate_commit(
                    factory,
                    lease=heartbeat.lease,
                    merge_step_id=merge_step_id,
                    expected_result=result,
                )
                if outcome == "committed":
                    committed = True
                elif outcome == "unknown":
                    abort_allowed = False
                    raise
                else:
                    raise
            else:
                committed = True
        try:
            complete_artifact_import(factory, claim, storage)
        except Exception:
            raise PostCommitCleanupError(
                "validate committed but artifact cleanup is pending"
            ) from None
        assert result is not None
        return result
    except Exception:
        if claim is not None and not committed and abort_allowed:
            try:
                with session_scope(factory) as session:
                    abort_artifact_import(session, claim)
            except Exception:
                # Preserve the business failure. A stale import claim is
                # recoverable by the artifact state machine's claim timeout.
                pass
        raise


def _execute_step_once(
    step_id: UUID,
    *,
    factory: sessionmaker[Session],
    work_root: Path,
    adapters: Mapping[str, ExtractionAdapter] = ADAPTERS,
    expected_generation: int | None = None,
) -> None:
    with session_scope(factory) as session:
        lease = acquire_step_lease(
            session, step_id, expected_generation=expected_generation
        )
    adapter: ExtractionAdapter | None = None
    uses_native_artifacts = False
    storage: Storage | None = None
    work_dir: Path | None = None
    try:
        with factory() as session:
            step = session.scalar(
                select(JobStep)
                .options(
                    selectinload(JobStep.document_job).selectinload(
                        DocumentJob.document_version
                    ),
                    selectinload(JobStep.document_job)
                    .selectinload(DocumentJob.batch)
                    .selectinload(ExtractionBatch.profile_version),
                )
                .where(JobStep.id == step_id)
            )
            if step is None or step.lease_token != lease.token:
                raise LeaseLost("step lease was replaced before execution")
            document_version_id = step.document_job.document_version_id
            document_version = step.document_job.document_version
            storage_key = document_version.storage_key
            document_size = document_version.size_bytes
            document_sha256 = document_version.sha256
            profile_snapshot = step.document_job.batch.profile_version.snapshot_json
            preset = profile_snapshot.get("preset")
            kind = step.kind
            merge_step_id = None
            shared_source_tsv: Path | None = None
            shared_rule_text_result: dict[str, object] | None = None
            if kind == "validate":
                merge_step = session.scalar(
                    select(JobStep).where(
                        JobStep.document_job_id == step.document_job_id,
                        JobStep.kind == "merge",
                        JobStep.status == "completed",
                    )
                )
                if merge_step is None:
                    raise PermanentAdapterError("merge output is not importable")
                merge_step_id = merge_step.id
            from app.jobs.service import _uses_legacy_rule_engine

            legacy_rule = kind in {"rule_text", "rule_table"} and _uses_legacy_rule_engine(profile_snapshot)
            if legacy_rule:
                base = LEGACY_ADAPTERS.get("legacy_v105_rule")
                if base is None:
                    raise PermanentAdapterError("KGchouqu legacy engine is not configured")
                adapter = base.for_route(kind)
                if kind == "rule_table":
                    # Reuse the sibling rule_text step's combined engine output
                    # when it is available; the table step then only filters the
                    # script TSV instead of re-running the v105 engine.
                    sibling = session.scalar(
                        select(JobStep).where(
                            JobStep.document_job_id == step.document_job_id,
                            JobStep.kind == "rule_text",
                            JobStep.status == "completed",
                        )
                    )
                    if sibling is not None:
                        result = sibling.result_json
                        artifacts = result.get("artifacts") if isinstance(result, dict) else None
                        if isinstance(artifacts, dict) and "shared_source_tsv" in artifacts:
                            shared_rule_text_result = result
            elif preset == "hybrid" and kind == "llm_text":
                adapter = LEGACY_ADAPTERS.get("legacy_v106_hybrid")
            else:
                adapter = adapters.get(kind) or execution_adapter_for(kind)
            if adapter is None:
                raise PermanentAdapterError("KGchouqu legacy engine is not configured")
            if kind in {"llm_text", "llm_table"} and adapter is ADAPTERS.get(kind):
                adapter = native_registry(model_client=DatabaseModelClient(factory, step_id))[kind]
            uses_native_artifacts = adapter.__class__.__module__.startswith("app.extraction.native")
            uses_legacy_artifacts = adapter.__class__.__module__.startswith("app.extraction.legacy")
            if preset == "hybrid" and kind == "llm_text":
                model_ids = profile_snapshot.get("model_config_ids", ())
                if isinstance(model_ids, list) and model_ids:
                    try:
                        model_config = session.get(ModelConfig, UUID(str(model_ids[0])))
                        if model_config is None or not model_config.is_enabled:
                            raise PermanentAdapterError("model configuration is unavailable")
                        profile_snapshot = dict(profile_snapshot)
                        profile_snapshot["legacy_model_api_key"] = resolved_secret(model_config)
                        profile_snapshot["legacy_model_endpoint"] = model_config.endpoint
                        profile_snapshot["legacy_model_name"] = model_config.model_name
                    except PermanentAdapterError:
                        raise
                    except Exception:
                        raise PermanentAdapterError("model configuration is unavailable") from None
            execution_idempotency_key = step.idempotency_key
            sibling_results = []
            if kind == "merge" and uses_native_artifacts:
                sibling_steps = list(
                    session.scalars(
                        select(JobStep).where(
                            JobStep.document_job_id == step.document_job_id,
                            JobStep.stage == 0,
                            JobStep.status == "completed",
                        ).order_by(JobStep.position)
                    )
                )
                sibling_results = _validated_sibling_results(
                    sibling_steps, document_version_id
                )
        assert adapter is not None
        work_dir = _isolated_work_dir(work_root, lease)
        settings = get_settings()
        storage = LocalStorage(settings.storage_root, max_bytes=settings.max_upload_bytes)
        if kind == "validate":
            assert merge_step_id is not None
            _run_validate_import(
                step_id=step_id,
                merge_step_id=merge_step_id,
                lease=lease,
                factory=factory,
                storage=storage,
                work_dir=work_dir,
            )
            return
        document_path = None
        route_manifests: tuple[Path, ...] = ()
        if (uses_native_artifacts or uses_legacy_artifacts) and kind in ROUTES:
            document_path = _stage_document_input(
                storage=storage,
                storage_key=storage_key,
                expected_size=document_size,
                expected_sha256=document_sha256,
                work_dir=work_dir,
            )
        elif uses_native_artifacts and kind == "merge":
            route_manifests = _restore_route_artifacts(work_dir, sibling_results, storage, factory)
        if shared_rule_text_result is not None:
            shared_source_tsv = _restore_shared_source_tsv(
                work_dir, shared_rule_text_result, storage, factory
            )
        model_ids = profile_snapshot.get("model_config_ids", ())
        model_config_id = (
            model_ids[0]
            if kind in {"llm_text", "llm_table"}
            and isinstance(model_ids, list)
            and model_ids
            and isinstance(model_ids[0], str)
            else None
        )
        context = AdapterContext(
            work_dir=work_dir,
            execution_idempotency_key=execution_idempotency_key,
            step_id=step_id,
            document_version_id=document_version_id,
            profile_snapshot=profile_snapshot,
            document_path=document_path,
            model_config_id=model_config_id,
            route_result_manifests=route_manifests,
            persistent_cache_root=settings.storage_root / ".native-unit-cache",
            shared_source_tsv=shared_source_tsv,
        )
        with LeaseHeartbeat(factory, lease) as heartbeat:
            result = adapter.run(context, lambda _event: None)
        if heartbeat.error is not None:
            raise LeaseLost("step heartbeat lost its lease")
        structured = _structured_result(context, result, storage, factory)
        with session_scope(factory) as session:
            _mark_artifacts_referenced(session, step_id, structured)
            finish_step_lease(session, heartbeat.lease, "completed", result_json=structured)
    except (LeaseLost, PostCommitCleanupError):
        raise
    except SQLAlchemyError:
        raise
    except Exception as error:
        failure_kind, code, summary = _failure_fields(error)
        with session_scope(factory) as session:
            finish_step_lease(
                session,
                lease,
                "retryable_failed" if failure_kind == "retryable" else "permanent_failed",
                failure_code=code,
                failure_summary=summary,
            )
        raise StepExecutionFailed(failure_kind) from None
    finally:
        if work_dir is not None:
            try:
                shutil.rmtree(work_dir)
            except OSError:
                pass
            else:
                complete_cleanup_attempt(factory, lease)
                try:
                    work_dir.parent.rmdir()
                except OSError:
                    pass


def complete_cleanup_attempt(
    factory: sessionmaker[Session], lease: StepLease
) -> None:
    try:
        with session_scope(factory) as session:
            session.execute(
                update(JobCleanupOutbox)
                .where(
                    JobCleanupOutbox.step_id == lease.step_id,
                    JobCleanupOutbox.attempt_number == lease.attempt_number,
                    JobCleanupOutbox.work_token == lease.token,
                    JobCleanupOutbox.status == "pending",
                )
                .values(
                    status="completed",
                    claim_token=None,
                    claimed_at=None,
                )
            )
    except SQLAlchemyError:
        raise ControlPlaneUnavailable() from None


def execute_step_once(
    step_id: UUID,
    *,
    factory: sessionmaker[Session],
    work_root: Path,
    adapters: Mapping[str, ExtractionAdapter] = ADAPTERS,
    expected_generation: int | None = None,
) -> None:
    try:
        _execute_step_once(
            step_id,
            factory=factory,
            work_root=work_root,
            adapters=adapters,
            expected_generation=expected_generation,
        )
    except (ControlPlaneUnavailable, LeaseLost, LeaseNotAcquired, StepExecutionFailed):
        raise
    except SQLAlchemyError:
        raise ControlPlaneUnavailable() from None


def _claim_cleanup_row(
    factory: sessionmaker[Session], now: datetime, excluded_ids: set[UUID]
) -> tuple[UUID, str, UUID, str] | None:
    claim_before = now - OUTBOX_CLAIM_TIMEOUT
    try:
        with session_scope(factory) as session:
            statement = (
                select(JobCleanupOutbox)
                .join(JobStep, JobStep.id == JobCleanupOutbox.step_id)
                .where(
                    JobCleanupOutbox.status == "pending",
                    or_(
                        JobStep.status != "running",
                        JobStep.lease_token != JobCleanupOutbox.work_token,
                    ),
                    or_(
                        JobCleanupOutbox.claim_token.is_(None),
                        JobCleanupOutbox.claimed_at <= claim_before,
                    ),
                )
            )
            if excluded_ids:
                statement = statement.where(
                    JobCleanupOutbox.id.not_in(excluded_ids)
                )
            row = session.scalar(
                statement
                .order_by(JobCleanupOutbox.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return None
            token = uuid4().hex
            result = session.execute(
                update(JobCleanupOutbox)
                .where(
                    JobCleanupOutbox.id == row.id,
                    JobCleanupOutbox.status == "pending",
                    exists(
                        select(JobStep.id).where(
                            JobStep.id == JobCleanupOutbox.step_id,
                            or_(
                                JobStep.status != "running",
                                JobStep.lease_token
                                != JobCleanupOutbox.work_token,
                            ),
                        )
                    ),
                    or_(
                        JobCleanupOutbox.claim_token.is_(None),
                        JobCleanupOutbox.claimed_at <= claim_before,
                    ),
                )
                .values(claim_token=token, claimed_at=now)
            )
            if result.rowcount != 1:
                return None
            return row.id, token, row.step_id, row.work_token
    except SQLAlchemyError:
        raise ControlPlaneUnavailable() from None


def retry_pending_workdir_cleanup(
    factory: sessionmaker[Session],
    work_root: Path,
    *,
    now: datetime | None = None,
) -> int:
    now = _require_aware(now, "now") if now is not None else datetime.now(UTC)
    cleaned = 0
    attempted_ids: set[UUID] = set()
    while True:
        claimed = _claim_cleanup_row(
            factory, now, attempted_ids
        )
        if claimed is None:
            break
        row_id, claim_token, step_id, work_token = claimed
        attempted_ids.add(row_id)
        target = (work_root.resolve() / str(step_id) / work_token).resolve()
        if work_root.resolve() not in target.parents:
            raise ControlPlaneUnavailable() from None
        try:
            shutil.rmtree(target)
            try:
                target.parent.rmdir()
            except OSError:
                pass
        except FileNotFoundError:
            pass
        except OSError:
            try:
                with session_scope(factory) as session:
                    session.execute(
                        update(JobCleanupOutbox)
                        .where(
                            JobCleanupOutbox.id == row_id,
                            JobCleanupOutbox.claim_token == claim_token,
                        )
                        .values(
                            claim_token=None,
                            claimed_at=None,
                            retry_count=JobCleanupOutbox.retry_count + 1,
                        )
                    )
            except SQLAlchemyError:
                raise ControlPlaneUnavailable() from None
            continue
        try:
            with session_scope(factory) as session:
                session.execute(
                    update(JobCleanupOutbox)
                    .where(
                        JobCleanupOutbox.id == row_id,
                        JobCleanupOutbox.claim_token == claim_token,
                        JobCleanupOutbox.status == "pending",
                    )
                    .values(
                        status="completed",
                        claim_token=None,
                        claimed_at=None,
                    )
                )
        except SQLAlchemyError:
            raise ControlPlaneUnavailable() from None
        cleaned += 1
    return cleaned


def recover_step_lease(
    factory: sessionmaker[Session],
    observed: StepLease,
    *,
    recovery_now: datetime,
) -> bool:
    _require_aware(recovery_now, "recovery_now")
    try:
        with session_scope(factory) as session:
            finish_step_lease(
                session,
                observed,
                "retryable_failed",
                failure_code="worker_lost",
                failure_summary="worker lease expired",
                now=recovery_now,
                observed_expiry=observed.expires_at,
                expired_by=recovery_now,
            )
        return True
    except LeaseNotAcquired:
        raise
    except SQLAlchemyError:
        raise ControlPlaneUnavailable() from None


def recover_expired_leases(
    factory: sessionmaker[Session], *, now: datetime | None = None
) -> int:
    now = _require_aware(now, "now") if now is not None else datetime.now(UTC)
    try:
        with factory() as session:
            observed_rows = list(
                session.execute(
                    select(
                        JobStep.id,
                        JobStep.lease_token,
                        JobStep.version,
                        JobStep.attempt_count,
                        JobStep.lease_expires_at,
                    ).where(
                        JobStep.status == "running",
                        JobStep.lease_token.is_not(None),
                        JobStep.lease_expires_at.is_not(None),
                        JobStep.lease_expires_at <= now,
                    )
                )
            )
    except SQLAlchemyError:
        raise ControlPlaneUnavailable() from None
    observed_leases = []
    for step_id, token, version, attempt_number, expiry in observed_rows:
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        observed_leases.append(
            StepLease(step_id, token, version, attempt_number, expiry)
        )
    recovered = 0
    control_plane_failures = 0
    for observed in observed_leases:
        try:
            if recover_step_lease(factory, observed, recovery_now=now):
                recovered += 1
        except LeaseNotAcquired:
            continue
        except ControlPlaneUnavailable:
            control_plane_failures += 1
            continue
    if observed_leases and control_plane_failures == len(observed_leases):
        raise ControlPlaneUnavailable() from None
    return recovered


def _send_task(*, task_name: str, args: list[str], queue: str) -> None:
    celery_app.send_task(task_name, args=args, queue=queue)


def load_dispatch(
    factory: sessionmaker[Session], dispatch_id: UUID
) -> tuple[UUID, int] | None:
    try:
        with factory() as session:
            dispatch = session.get(JobDispatchOutbox, dispatch_id)
            if dispatch is None:
                return None
            return dispatch.step_id, dispatch.dispatch_generation
    except SQLAlchemyError:
        raise ControlPlaneUnavailable() from None


def _claim_outbox_row(
    factory: sessionmaker[Session], now: datetime
) -> tuple[UUID, str, UUID, str] | None:
    claim_before = now - OUTBOX_CLAIM_TIMEOUT
    with session_scope(factory) as session:
        row = session.scalar(
            select(JobDispatchOutbox)
            .where(
                JobDispatchOutbox.published_at.is_(None),
                JobDispatchOutbox.available_at <= now,
                or_(
                    JobDispatchOutbox.claim_token.is_(None),
                    JobDispatchOutbox.claimed_at <= claim_before,
                ),
            )
            .order_by(JobDispatchOutbox.available_at, JobDispatchOutbox.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return None
        token = uuid4().hex
        result = session.execute(
            update(JobDispatchOutbox)
            .where(
                JobDispatchOutbox.id == row.id,
                JobDispatchOutbox.published_at.is_(None),
                or_(
                    JobDispatchOutbox.claim_token.is_(None),
                    JobDispatchOutbox.claimed_at <= claim_before,
                ),
            )
            .values(
                claim_token=token,
                claimed_at=now,
                publish_attempts=JobDispatchOutbox.publish_attempts + 1,
                failure_code=None,
                failure_summary=None,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            return None
        return row.id, token, row.step_id, row.queue


def publish_outbox(
    factory: sessionmaker[Session],
    sender: Callable[..., object] = _send_task,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> int:
    now = now or datetime.now(UTC)
    published = 0
    for _ in range(limit):
        claimed = _claim_outbox_row(factory, now)
        if claimed is None:
            break
        row_id, token, step_id, queue = claimed
        try:
            sender(
                task_name="app.jobs.tasks.execute_step",
                args=[str(row_id)],
                queue=queue,
            )
        except Exception:
            with session_scope(factory) as session:
                session.execute(
                    update(JobDispatchOutbox)
                    .where(
                        JobDispatchOutbox.id == row_id,
                        JobDispatchOutbox.claim_token == token,
                        JobDispatchOutbox.published_at.is_(None),
                    )
                    .values(
                        claim_token=None,
                        claimed_at=None,
                        available_at=now + timedelta(seconds=30),
                        failure_code="broker_unavailable",
                        failure_summary="broker publish failed",
                    )
                )
            continue
        with session_scope(factory) as session:
            result = session.execute(
                update(JobDispatchOutbox)
                .where(
                    JobDispatchOutbox.id == row_id,
                    JobDispatchOutbox.claim_token == token,
                    JobDispatchOutbox.published_at.is_(None),
                )
                .values(
                    published_at=now,
                    claim_token=None,
                    claimed_at=None,
                    failure_code=None,
                    failure_summary=None,
                )
            )
            if result.rowcount == 1:
                published += 1
    return published


@celery_app.task(
    bind=True,
    name="app.jobs.tasks.execute_step",
    acks_late=True,
    reject_on_worker_lost=True,
    ignore_result=True,
)
def execute_step(self: Task, dispatch_id: str) -> None:
    del self
    try:
        factory = get_session_factory()
        dispatch = load_dispatch(factory, UUID(dispatch_id))
        if dispatch is None:
            return
        step_id, dispatch_generation = dispatch
        execute_step_once(
            step_id,
            factory=factory,
            work_root=Path(os.getenv("EXTRACTION_WORK_ROOT", "work")),
            expected_generation=dispatch_generation,
        )
    except (
        ControlPlaneUnavailable,
        LeaseNotAcquired,
        LeaseLost,
        StepExecutionFailed,
        PostCommitCleanupError,
        ValueError,
    ):
        return


@celery_app.task(
    bind=True,
    name="app.jobs.tasks.publish_dispatch_outbox",
    max_retries=2,
    ignore_result=True,
)
def publish_dispatch_outbox(self: Task) -> None:
    try:
        publish_outbox(get_session_factory())
    except Exception:
        raise self.retry(countdown=5) from None


@celery_app.task(
    name="app.jobs.tasks.recover_expired_step_leases", ignore_result=True
)
def recover_expired_step_leases() -> None:
    recover_expired_leases(get_session_factory())


@celery_app.task(
    name="app.jobs.tasks.retry_pending_workdir_cleanup", ignore_result=True
)
def retry_pending_workdir_cleanup_task() -> None:
    retry_pending_workdir_cleanup(
        get_session_factory(), Path(os.getenv("EXTRACTION_WORK_ROOT", "work"))
    )


@celery_app.task(name="app.jobs.tasks.sweep_job_artifacts", ignore_result=True)
def sweep_job_artifacts_task() -> None:
    settings = get_settings()
    sweep_job_artifacts(
        get_session_factory(),
        LocalStorage(settings.storage_root, max_bytes=settings.max_upload_bytes),
    )


@celery_app.task(name="app.jobs.tasks.retry_pending_storage_cleanup", ignore_result=True)
def retry_pending_storage_cleanup_task() -> None:
    settings = get_settings()
    LocalStorage(settings.storage_root, max_bytes=settings.max_upload_bytes).retry_pending_cleanup()
