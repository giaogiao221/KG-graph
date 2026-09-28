from __future__ import annotations

import hashlib
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.documents.models import DocumentVersion
from app.jobs.models import DocumentJob, ExtractionBatch, JobStep
from app.jobs.tasks import stage_ready_dispatches_for_job
from app.profiles.models import ProfileVersion
from app.models.models import ModelConfig


class BatchValidationError(ValueError):
    pass


_ROUTE_STEPS = (
    ("text_rule", "rule_text"),
    ("text_llm", "llm_text"),
    ("table_rule", "rule_table"),
    ("table_llm", "llm_table"),
)


def batch_request_key(
    project_id: UUID,
    profile_version_id: UUID,
    document_version_ids: list[UUID],
) -> str:
    canonical_documents = sorted(str(value) for value in document_version_ids)
    canonical = "\0".join(
        (str(project_id), str(profile_version_id), *canonical_documents)
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def step_idempotency_key(
    *,
    document_sha256: str,
    profile_snapshot_sha256: str,
    adapter_version: str,
    step_kind: str,
) -> str:
    canonical = "\0".join(
        (document_sha256, profile_snapshot_sha256, adapter_version, step_kind)
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _adapter_version(snapshot: dict[str, object], step_kind: str) -> str:
    preset = snapshot.get("preset")
    if step_kind in {"rule_text", "rule_table"} and _uses_legacy_rule_engine(snapshot):
        return "kgchouqu-v105-script-bridge-v1"
    if preset == "hybrid" and step_kind == "llm_text":
        return "kgchouqu-v106-script-bridge-v1"
    if step_kind.startswith("rule_"):
        return str(snapshot.get("rule_version", ""))
    if step_kind.startswith("llm_"):
        return ":".join(
            (
                str(snapshot.get("plugin_version", "")),
                str(snapshot.get("prompt_version", "")),
            )
        )
    return str(snapshot.get("plugin_version", ""))


def _uses_legacy_rule_engine(snapshot: dict[str, object]) -> bool:
    """Rule routes run the real v105 script engine when a legacy engine is wired.

    The bundled v105 engine produces text and table facts in one combined run;
    the platform splits them into the rule_text/rule_table routes by the script
    TSV's 来源类型 column instead of running the engine twice.  The explicit
    ``rule_engine`` snapshot key lets a deployment fall back to the lightweight
    builtin engine (used in tests and engine-less development) while production
    defaults to the real script engine.
    """
    if snapshot.get("rule_engine") == "builtin":
        return False
    preset = snapshot.get("preset")
    if preset == "rule":
        return True
    routes = snapshot.get("routes")
    return bool(
        isinstance(routes, dict)
        and (routes.get("text_rule") is True or routes.get("table_rule") is True)
    )


def _selected_steps(snapshot: dict[str, object]) -> list[str]:
    preset = snapshot.get("preset")
    if preset == "rule":
        return ["rule_text", "rule_table", "merge", "validate"]
    if preset == "hybrid":
        return ["llm_text", "merge", "validate"]
    routes = snapshot.get("routes")
    if not isinstance(routes, dict):
        raise BatchValidationError("profile snapshot has invalid routes")
    selected = [step for route, step in _ROUTE_STEPS if routes.get(route) is True]
    if not selected:
        raise BatchValidationError("profile snapshot selects no extraction routes")
    return [*selected, "merge", "validate"]


def _existing_batch(session: Session, request_key: str) -> ExtractionBatch | None:
    return session.scalar(
        select(ExtractionBatch)
        .options(
            selectinload(ExtractionBatch.document_jobs).selectinload(
                DocumentJob.steps
            )
        )
        .where(ExtractionBatch.request_key == request_key)
    )


def create_batch(
    session: Session,
    profile_version_id: UUID,
    document_version_ids: list[UUID],
    created_by_id: UUID | None = None,
) -> ExtractionBatch:
    if not document_version_ids:
        raise BatchValidationError("at least one document version is required")
    if len(set(document_version_ids)) != len(document_version_ids):
        raise BatchValidationError("document version ids must be unique")

    profile = session.scalar(
        select(ProfileVersion)
        .options(selectinload(ProfileVersion.profile))
        .where(ProfileVersion.id == profile_version_id)
    )
    if profile is None:
        raise BatchValidationError("profile version not found")
    routes = profile.snapshot_json.get("routes")
    model_ids = profile.snapshot_json.get("model_config_ids")
    if (
        isinstance(routes, dict)
        and (routes.get("text_llm") is True or routes.get("table_llm") is True)
        and model_ids is not None
    ):
        if not isinstance(model_ids, list) or len(model_ids) != 1:
            raise BatchValidationError("LLM profile requires one model configuration")
        try:
            model_id = UUID(str(model_ids[0]))
        except (TypeError, ValueError):
            raise BatchValidationError("model configuration not found") from None
        enabled = session.scalar(
            select(ModelConfig.id)
            .where(ModelConfig.id == model_id, ModelConfig.is_enabled.is_(True))
            .with_for_update(read=True)
        )
        if enabled is None:
            raise BatchValidationError("model configuration not found")

    documents = list(
        session.scalars(
            select(DocumentVersion)
            .options(selectinload(DocumentVersion.document))
            .where(DocumentVersion.id.in_(document_version_ids))
        )
    )
    by_id = {document.id: document for document in documents}
    if len(by_id) != len(document_version_ids):
        raise BatchValidationError("document version not found")
    ordered = [by_id[document_id] for document_id in sorted(document_version_ids)]
    if any(document.document.project_id != profile.profile.project_id for document in ordered):
        raise BatchValidationError("document and profile must belong to the same project")
    if any(not document.is_extractable for document in ordered):
        raise BatchValidationError("document version is not extractable")

    kinds = _selected_steps(profile.snapshot_json)
    request_key = batch_request_key(
        profile.profile.project_id, profile.id, document_version_ids
    )
    existing = _existing_batch(session, request_key)
    if existing is not None:
        return existing
    batch = ExtractionBatch(
        project_id=profile.profile.project_id,
        profile_version_id=profile.id,
        request_key=request_key,
        created_by_id=created_by_id or profile.created_by_id,
    )
    for document in ordered:
        job = DocumentJob(document_version_id=document.id)
        job.steps = [
            JobStep(
                kind=kind,
                position=position,
                stage=0 if kind not in {"merge", "validate"} else (1 if kind == "merge" else 2),
                idempotency_key=step_idempotency_key(
                    document_sha256=document.sha256,
                    profile_snapshot_sha256=profile.snapshot_sha256,
                    adapter_version=_adapter_version(profile.snapshot_json, kind),
                    step_kind=kind,
                ),
            )
            for position, kind in enumerate(kinds)
        ]
        batch.document_jobs.append(job)
    try:
        with session.begin_nested():
            session.add(batch)
            session.flush()
            for job in batch.document_jobs:
                stage_ready_dispatches_for_job(session, job.id)
    except IntegrityError:
        session.expire_all()
        existing = _existing_batch(session, request_key)
        if existing is None:
            raise
        return existing
    return batch
