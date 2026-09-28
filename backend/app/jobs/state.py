from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TypeVar
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.jobs.models import DocumentJob, ExtractionBatch, JobStep


class InvalidTransition(ValueError):
    pass


class VersionConflict(RuntimeError):
    pass


class PrerequisiteNotMet(RuntimeError):
    pass


ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "cancelled"}),
    "running": frozenset(
        {
            "partial_success",
            "completed",
            "retryable_failed",
            "permanent_failed",
            "cancelled",
        }
    ),
    "partial_success": frozenset({"completed", "retryable_failed"}),
    "retryable_failed": frozenset({"queued", "permanent_failed"}),
    "completed": frozenset(),
    "permanent_failed": frozenset(),
    "cancelled": frozenset(),
}
ALLOWED_STEP_TRANSITIONS = ALLOWED_TRANSITIONS
ALLOWED_PARENT_AGGREGATE_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset(
        {
            "running",
            "retryable_failed",
            "partial_success",
            "completed",
            "permanent_failed",
            "cancelled",
        }
    ),
    "running": frozenset(
        {
            "queued",
            "retryable_failed",
            "partial_success",
            "completed",
            "permanent_failed",
            "cancelled",
        }
    ),
    "retryable_failed": frozenset(
        {
            "queued",
            "running",
            "partial_success",
            "completed",
            "permanent_failed",
            "cancelled",
        }
    ),
    "partial_success": frozenset(
        {
            "running",
            "retryable_failed",
            "completed",
            "permanent_failed",
            "cancelled",
        }
    ),
    "completed": frozenset(),
    "permanent_failed": frozenset(),
    "cancelled": frozenset(),
}
AGGREGATE_ONLY_PARENT_STATUSES = frozenset(
    {"partial_success", "completed", "permanent_failed", "cancelled"}
)
TERMINAL_PARENT_STATUSES = frozenset({"completed", "permanent_failed", "cancelled"})


def validate_transition(source: str, target: str, *, entity: str = "step") -> None:
    if source not in ALLOWED_TRANSITIONS:
        raise InvalidTransition(f"unknown source {entity} status: {source}")
    if target not in ALLOWED_TRANSITIONS:
        raise InvalidTransition(f"unknown target {entity} status: {target}")
    if target not in ALLOWED_TRANSITIONS[source]:
        raise InvalidTransition(f"cannot transition {entity} from {source} to {target}")


ModelT = TypeVar("ModelT", JobStep, DocumentJob, ExtractionBatch)


def _cas_transition(
    session: Session,
    model: type[ModelT],
    object_id: UUID,
    target: str,
    expected_version: int,
    *,
    entity: str,
) -> ModelT:
    if not isinstance(expected_version, int) or isinstance(expected_version, bool):
        raise VersionConflict("expected_version must be an integer")
    current = session.execute(
        select(model.status, model.version).where(model.id == object_id)
    ).one_or_none()
    if current is None or current.version != expected_version:
        raise VersionConflict(f"{entity} version conflict")
    validate_transition(current.status, target, entity=entity)
    values: dict[str, object] = {
        "status": target,
        "version": expected_version + 1,
        "updated_at": datetime.now(UTC),
    }
    if model is JobStep:
        if target == "running":
            values.update(
                lease_token=uuid4().hex,
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=15),
            )
        else:
            values.update(lease_token=None, lease_expires_at=None)
    result = session.execute(
        update(model)
        .where(
            model.id == object_id,
            model.version == expected_version,
            model.status == current.status,
        )
        .values(**values)
    )
    if result.rowcount != 1:
        raise VersionConflict(f"{entity} version conflict")
    session.expire_all()
    value = session.get(model, object_id)
    if value is None:  # pragma: no cover - the CAS rowcount proves existence
        raise VersionConflict(f"{entity} version conflict")
    return value


def aggregate_status(statuses: list[str]) -> str:
    if statuses and all(status == "completed" for status in statuses):
        return "completed"
    if statuses and all(status == "cancelled" for status in statuses):
        return "cancelled"
    if statuses and all(status == "queued" for status in statuses):
        return "queued"
    if "running" in statuses:
        return "running"
    if "retryable_failed" in statuses:
        return "retryable_failed"
    if "queued" in statuses:
        return "running"
    if "partial_success" in statuses:
        return "partial_success"
    if "permanent_failed" in statuses:
        if "completed" in statuses:
            return "partial_success"
        return "permanent_failed"
    if "completed" in statuses and "cancelled" in statuses:
        return "partial_success"
    return "queued"


def _set_aggregate_status(
    session: Session, model: type[ModelT], object_id: UUID, target: str
) -> None:
    current = session.execute(
        select(model.status, model.version).where(model.id == object_id)
    ).one()
    if current.status == target:
        return
    if current.status in TERMINAL_PARENT_STATUSES:
        raise VersionConflict("terminal parent state cannot regress")
    if target not in ALLOWED_PARENT_AGGREGATE_TRANSITIONS.get(
        current.status, frozenset()
    ):
        raise VersionConflict("parent aggregate transition conflict") from None
    result = session.execute(
        update(model)
        .where(model.id == object_id, model.version == current.version)
        .values(
            status=target,
            version=current.version + 1,
            updated_at=datetime.now(UTC),
        )
    )
    if result.rowcount != 1:
        raise VersionConflict("parent state changed during aggregation")


def _aggregate_batch(session: Session, batch_id: UUID) -> None:
    statuses = list(
        session.scalars(select(DocumentJob.status).where(DocumentJob.batch_id == batch_id))
    )
    _set_aggregate_status(
        session, ExtractionBatch, batch_id, aggregate_status(statuses)
    )


def lock_step_parents(
    session: Session, document_job_id: UUID
) -> tuple[DocumentJob, ExtractionBatch]:
    job = session.scalar(
        select(DocumentJob)
        .where(DocumentJob.id == document_job_id)
        .with_for_update()
    )
    if job is None:
        raise VersionConflict("document job no longer exists")
    batch = session.scalar(
        select(ExtractionBatch)
        .where(ExtractionBatch.id == job.batch_id)
        .with_for_update()
    )
    if batch is None:
        raise VersionConflict("batch no longer exists")
    return job, batch


def _aggregate_parents(session: Session, document_job_id: UUID) -> None:
    job, _batch = lock_step_parents(session, document_job_id)
    statuses = list(
        session.scalars(
            select(JobStep.status).where(JobStep.document_job_id == document_job_id)
        )
    )
    _set_aggregate_status(session, DocumentJob, job.id, aggregate_status(statuses))
    _aggregate_batch(session, job.batch_id)


def is_step_ready(session: Session, step_id: UUID) -> bool:
    step = session.get(JobStep, step_id)
    if step is None or step.status != "queued":
        return False
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


def transition_step(
    session: Session, step_id: UUID, target: str, expected_version: int
) -> JobStep:
    with session.begin_nested():
        if target == "running":
            current = session.execute(
                select(JobStep.status, JobStep.version).where(JobStep.id == step_id)
            ).one_or_none()
            if current is None or current.version != expected_version:
                raise VersionConflict("step version conflict")
            validate_transition(current.status, target)
            if not is_step_ready(session, step_id):
                raise PrerequisiteNotMet("step prerequisites are not completed")
        step = _cas_transition(
            session,
            JobStep,
            step_id,
            target,
            expected_version,
            entity="step",
        )
        _aggregate_parents(session, step.document_job_id)
    session.expire_all()
    refreshed = session.get(JobStep, step_id)
    if refreshed is None:  # pragma: no cover
        raise VersionConflict("step no longer exists")
    return refreshed


def transition_document_job(
    session: Session, job_id: UUID, target: str, expected_version: int
) -> DocumentJob:
    if target in AGGREGATE_ONLY_PARENT_STATUSES:
        raise InvalidTransition(f"document job status {target} is aggregate-only")
    with session.begin_nested():
        job = _cas_transition(
            session,
            DocumentJob,
            job_id,
            target,
            expected_version,
            entity="document job",
        )
        _aggregate_batch(session, job.batch_id)
    session.expire_all()
    refreshed = session.get(DocumentJob, job_id)
    if refreshed is None:  # pragma: no cover
        raise VersionConflict("document job no longer exists")
    return refreshed


def transition_batch(
    session: Session, batch_id: UUID, target: str, expected_version: int
) -> ExtractionBatch:
    if target in AGGREGATE_ONLY_PARENT_STATUSES:
        raise InvalidTransition(f"batch status {target} is aggregate-only")
    return _cas_transition(
        session,
        ExtractionBatch,
        batch_id,
        target,
        expected_version,
        entity="batch",
    )
