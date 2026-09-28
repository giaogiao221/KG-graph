from __future__ import annotations

import csv
import hashlib
import io
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.extraction.contracts import PermanentAdapterError
from app.facts.models import RawFact
from app.facts.schema59 import FIELD, SCHEMA59_COLUMNS, evidence_hash as contract_evidence_hash
from app.jobs.models import DocumentJob, JobStep


MAX_FACT_TSV_BYTES = 32_000_000
MAX_FACT_ROWS = 10_000
MAX_FIELD_CHARS = 32_000
_HEX64 = re.compile(r"[0-9a-f]{64}")
_REVIEW_STATUSES = frozenset(
    {"candidate", "candidate_review", "approved", "rejected"}
)


class FactImportError(PermanentAdapterError):
    """A fixed, non-sensitive error for an invalid fact artifact."""

    def __init__(self) -> None:
        super().__init__("schema59 fact artifact is invalid")


@dataclass(frozen=True, slots=True)
class ImportSummary:
    inserted: int
    duplicates: int
    total: int


@dataclass(frozen=True, slots=True)
class _ImportContext:
    import_step: JobStep
    source_step: JobStep
    job: DocumentJob
    project_id: UUID
    document_id: UUID
    document_version_id: UUID
    batch_id: UUID
    document_sha256: str


def _context(session: Session, step_id: UUID) -> _ImportContext:
    step = session.scalar(
        select(JobStep)
        .options(
            selectinload(JobStep.document_job).selectinload(
                DocumentJob.document_version
            ),
            selectinload(JobStep.document_job).selectinload(DocumentJob.batch),
        )
        .where(JobStep.id == step_id)
    )
    if step is None or step.kind not in {"merge", "validate"}:
        raise FactImportError()
    source = step
    if step.kind == "validate":
        source = session.scalar(
            select(JobStep).where(
                JobStep.document_job_id == step.document_job_id,
                JobStep.kind == "merge",
                JobStep.status == "completed",
            )
        )
        if source is None:
            raise FactImportError()
    job = step.document_job
    version = job.document_version
    if source.document_job_id != job.id:
        raise FactImportError()
    return _ImportContext(
        import_step=step,
        source_step=source,
        job=job,
        project_id=job.batch.project_id,
        document_id=version.document_id,
        document_version_id=version.id,
        batch_id=job.batch_id,
        document_sha256=version.sha256,
    )


def _canonical(value: str, *, fold: bool = True) -> str:
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    return normalized.casefold() if fold else normalized


def _dedup_key(row: dict[str, str]) -> str:
    graph_key = _canonical(row["graph_fact_key"])
    if graph_key:
        payload = graph_key
        prefix = "g:"
    else:
        payload = "\0".join(
            _canonical(row[column])
            for column in (
                FIELD["subject"],
                FIELD["property"],
                FIELD["value"],
                FIELD["unit"],
                FIELD["condition"],
                FIELD["evidence_text"],
            )
        )
        prefix = "f:"
    return prefix + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _linked(value: str, expected: UUID, *, required: bool = False) -> bool:
    if not value:
        return not required
    try:
        return UUID(value) == expected
    except ValueError:
        return False


def _confidence(value: str) -> float | None:
    if not value:
        return None
    try:
        parsed = float(value)
    except ValueError:
        raise FactImportError() from None
    if not 0.0 <= parsed <= 1.0:
        raise FactImportError()
    return parsed


def validate_schema59_row(row: dict[str, str]) -> None:
    """Validate the content-only part of a complete canonical fact row.

    Pipeline linkage is intentionally validated by ``_fact_from_row`` because
    that requires a job context. Review versions call this same validator and
    preserve all linkage columns from the immutable raw row.
    """
    if set(row) != set(SCHEMA59_COLUMNS) or len(row) != len(SCHEMA59_COLUMNS):
        raise FactImportError()
    if any(
        not isinstance(value, str)
        or len(value) > MAX_FIELD_CHARS
        or "\x00" in value
        for value in row.values()
    ):
        raise FactImportError()
    evidence = row[FIELD["evidence_text"]]
    supplied_hash = row[FIELD["evidence_hash"]].casefold()
    if (
        not evidence.strip()
        or not _HEX64.fullmatch(supplied_hash)
        or supplied_hash != contract_evidence_hash(evidence)
        or row[FIELD["review_status"]] not in _REVIEW_STATUSES
    ):
        raise FactImportError()
    _confidence(row["置信度"])
    for value, limit in (
        (row[FIELD["subject"]], 512),
        (row[FIELD["property"]], 512),
        (row[FIELD["unit"]], 512),
        (row[FIELD["source_kind"]], 128),
        (row[FIELD["extraction_source"]], 128),
        (row[FIELD["route"]], 128),
    ):
        if len(value) > limit:
            raise FactImportError()


def _load_rows(path: Path) -> list[dict[str, str]]:
    try:
        info = path.stat()
        if not path.is_file() or path.is_symlink() or info.st_size > MAX_FACT_TSV_BYTES:
            raise FactImportError()
        data = path.read_bytes()
        if len(data) > MAX_FACT_TSV_BYTES or b"\x00" in data:
            raise FactImportError()
        text = data.decode("utf-8", errors="strict")
        reader = csv.DictReader(io.StringIO(text, newline=""), delimiter="\t")
        if tuple(reader.fieldnames or ()) != SCHEMA59_COLUMNS:
            raise FactImportError()
        rows: list[dict[str, str]] = []
        for parsed in reader:
            if (
                len(rows) >= MAX_FACT_ROWS
                or None in parsed
                or set(parsed) != set(SCHEMA59_COLUMNS)
                or tuple(parsed.get(name, "") for name in SCHEMA59_COLUMNS)
                == SCHEMA59_COLUMNS
            ):
                raise FactImportError()
            row = {name: parsed[name] for name in SCHEMA59_COLUMNS}
            if any(
                not isinstance(value, str)
                or len(value) > MAX_FIELD_CHARS
                or "\x00" in value
                for value in row.values()
            ):
                raise FactImportError()
            rows.append(row)
        return rows
    except FactImportError:
        raise
    except (OSError, UnicodeError, csv.Error):
        raise FactImportError() from None


def _fact_from_row(context: _ImportContext, row: dict[str, str], key: str) -> RawFact:
    validate_schema59_row(row)
    evidence = row[FIELD["evidence_text"]]
    evidence_hash = row[FIELD["evidence_hash"]].casefold()
    review_status = row[FIELD["review_status"]]
    if (
        not _linked(
            row[FIELD["document_id"]], context.document_version_id, required=True
        )
        or not _linked(row[FIELD["step_id"]], context.source_step.id, required=True)
        or not _linked(row["任务ID"], context.job.id)
        or not _linked(row["批次ID"], context.batch_id)
        or (row["文档哈希"] and row["文档哈希"] != context.document_sha256)
    ):
        raise FactImportError()
    graph_key = _canonical(row["graph_fact_key"], fold=False) or None
    if graph_key is not None and len(graph_key) > 512:
        raise FactImportError()
    subject = row[FIELD["subject"]]
    property_value = row[FIELD["property"]]
    unit = row[FIELD["unit"]]
    source_type = row[FIELD["source_kind"]]
    extraction_source = row[FIELD["extraction_source"]]
    route = row[FIELD["route"]]
    stable_id = uuid5(NAMESPACE_URL, f"raw-fact:{context.job.id}:{key}")
    return RawFact(
        id=stable_id,
        project_id=context.project_id,
        document_id=context.document_id,
        document_version_id=context.document_version_id,
        document_job_id=context.job.id,
        import_step_id=context.import_step.id,
        source_step_id=context.source_step.id,
        graph_fact_key=graph_key,
        dedup_key=key,
        subject=subject,
        property=property_value,
        value=row[FIELD["value"]],
        unit=unit,
        condition=row[FIELD["condition"]],
        source_type=source_type,
        confidence=_confidence(row["置信度"]),
        review_status=review_status,
        evidence_text=evidence,
        evidence_hash=evidence_hash,
        extraction_source=extraction_source,
        route=route,
        row_json=dict(row),
    )


def import_facts(session: Session, step_id: UUID, tsv_path: Path) -> ImportSummary:
    """Validate and stage an immutable, idempotent schema59 import.

    The caller owns the surrounding transaction. This function flushes so all
    database constraints are checked before it returns, but never commits.
    """

    context = _context(session, step_id)
    rows = _load_rows(Path(tsv_path))
    unique: dict[str, RawFact] = {}
    for row in rows:
        key = _dedup_key(row)
        fact = _fact_from_row(context, row, key)
        unique.setdefault(key, fact)

    facts = list(unique.values())
    inserted = 0
    if facts:
        values = [
            {
                column.name: getattr(fact, column.name)
                for column in RawFact.__table__.columns
                if column.name not in {"created_at", "updated_at", "version"}
            }
            for fact in facts
        ]
        dialect = session.get_bind().dialect.name
        if dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert

            statement = insert(RawFact).values(values).on_conflict_do_nothing(
                index_elements=["document_job_id", "dedup_key"]
            )
            inserted = session.execute(statement).rowcount
        elif dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert

            statement = insert(RawFact).values(values).on_conflict_do_nothing(
                index_elements=["document_job_id", "dedup_key"]
            )
            inserted = session.execute(statement).rowcount
        else:  # pragma: no cover - production and tests use PostgreSQL/SQLite.
            existing = set(
                session.scalars(
                    select(RawFact.dedup_key).where(
                        RawFact.document_job_id == context.job.id,
                        RawFact.dedup_key.in_(tuple(unique)),
                    )
                )
            )
            pending = [fact for key, fact in unique.items() if key not in existing]
            session.add_all(pending)
            session.flush()
            inserted = len(pending)
        _enqueue_review_tasks(session, context, facts)
    return ImportSummary(inserted=inserted, duplicates=len(rows) - inserted, total=len(rows))


def _enqueue_review_tasks(session: Session, context: _ImportContext, facts: list[RawFact]) -> None:
    """Create one immutable review root/task for each newly imported raw fact.

    Existing roots and completed review tasks are left untouched so an idempotent
    validate retry never reopens a finished review.
    """
    actor_id = context.job.batch.created_by_id
    if actor_id is None or not facts:
        return
    from app.reviews.models import ReviewFactRoot, ReviewTask

    raw_ids = [fact.id for fact in facts]
    roots = list(session.scalars(select(ReviewFactRoot).where(ReviewFactRoot.raw_fact_id.in_(raw_ids))))
    roots_by_raw = {root.raw_fact_id: root for root in roots}
    for fact in facts:
        if fact.id not in roots_by_raw:
            root = ReviewFactRoot(
                project_id=context.project_id,
                raw_fact_id=fact.id,
                document_id=context.document_id,
                document_version_id=context.document_version_id,
                source="raw",
                created_by_id=actor_id,
            )
            session.add(root)
            roots_by_raw[fact.id] = root
    session.flush()
    root_ids = [root.id for root in roots_by_raw.values()]
    existing_task_roots = set(session.scalars(select(ReviewTask.root_id).where(ReviewTask.root_id.in_(root_ids))))
    session.add_all(
        ReviewTask(project_id=context.project_id, root_id=root.id)
        for root in roots_by_raw.values()
        if root.id not in existing_task_roots
    )
