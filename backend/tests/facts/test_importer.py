from __future__ import annotations

import csv
import hashlib
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.documents.models import Document, DocumentVersion
from app.facts.importer import FactImportError, import_facts
from app.facts.models import RawFact
from app.facts.schema59 import FIELD, SCHEMA59_COLUMNS, evidence_hash
from app.jobs.models import JobStep
from app.jobs.service import create_batch
from app.profiles.models import ExtractionProfile, ProfileVersion


def _job(
    session, project_id: UUID, user_id: UUID, content: bytes = b"facts"
) -> dict[str, JobStep]:
    profile = ExtractionProfile(project_id=project_id, name=f"facts-{uuid4()}")
    profile_version = ProfileVersion(
        profile=profile,
        created_by_id=user_id,
        version_number=1,
        snapshot_json={
            "routes": {"text_rule": True},
            "rule_engine": "builtin",
            "rule_version": "rule-v1",
            "plugin_version": None,
            "prompt_version": None,
        },
        snapshot_sha256=hashlib.sha256(uuid4().bytes).hexdigest(),
    )
    document = Document(project_id=project_id, created_by_id=user_id)
    version = DocumentVersion(
        document=document,
        uploader_id=user_id,
        version_number=1,
        original_filename="facts.md",
        storage_key=uuid4().hex,
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        mime_type="text/markdown",
        is_extractable=True,
    )
    session.add_all([profile_version, version])
    session.flush()
    batch = create_batch(session, profile_version.id, [version.id])
    steps = {step.kind: step for step in batch.document_jobs[0].steps}
    merge = steps["merge"]
    merge.status = "completed"
    return steps


def _row(steps: dict[str, JobStep], **overrides: str) -> dict[str, str]:
    merge = steps["merge"]
    version = merge.document_job.document_version
    evidence = overrides.pop("evidence", "Alpha has mass 3 kg.")
    row = {column: "" for column in SCHEMA59_COLUMNS}
    row.update(
        {
            "fact_id": "untrusted-upstream-id",
            "graph_fact_key": " Alpha : Mass ",
            FIELD["document_id"]: str(version.id),
            FIELD["subject"]: "Alpha",
            FIELD["property"]: "mass",
            FIELD["value"]: "3",
            FIELD["unit"]: "kg",
            FIELD["condition"]: "ambient",
            FIELD["source_kind"]: "text",
            FIELD["route"]: "merge",
            FIELD["review_status"]: "candidate",
            FIELD["evidence_text"]: evidence,
            FIELD["evidence_hash"]: evidence_hash(evidence),
            FIELD["step_id"]: str(merge.id),
            FIELD["extraction_source"]: "merge",
            "文档哈希": version.sha256,
            "批次ID": str(merge.document_job.batch_id),
            "任务ID": str(merge.document_job_id),
        }
    )
    row.update(overrides)
    return row


def _write(path: Path, rows: list[dict[str, str]], header=SCHEMA59_COLUMNS) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_import_is_idempotent_and_preserves_untrusted_fact_id_in_row_json(
    app_session_factory, project, users, tmp_path: Path
) -> None:
    with app_session_factory.begin() as session:
        steps = _job(session, project.id, users["operator"].id)
        path = _write(tmp_path / "facts.tsv", [_row(steps)])
        first = import_facts(session, steps["validate"].id, path)
        second = import_facts(session, steps["validate"].id, path)

    assert (first.inserted, first.duplicates, first.total) == (1, 0, 1)
    assert (second.inserted, second.duplicates, second.total) == (0, 1, 1)
    with app_session_factory() as session:
        fact = session.scalar(select(RawFact))
        assert fact is not None
        assert str(fact.id) != "untrusted-upstream-id"
        assert fact.row_json["fact_id"] == "untrusted-upstream-id"
        assert fact.project_id == project.id


def test_internal_graph_and_fallback_duplicates_are_counted(
    app_session_factory, project, users, tmp_path: Path
) -> None:
    with app_session_factory.begin() as session:
        steps = _job(session, project.id, users["operator"].id)
        first = _row(steps)
        same_graph = _row(steps, graph_fact_key="alpha : mass")
        fallback_a = _row(
            steps,
            graph_fact_key="",
            evidence="Beta has mass 4 kg.",
            主体="Beta",
            数值="4",
        )
        fallback_b = dict(fallback_a)
        fallback_b[FIELD["subject"]] = "  Beta  "
        path = _write(tmp_path / "dedup.tsv", [first, same_graph, fallback_a, fallback_b])
        result = import_facts(session, steps["validate"].id, path)

    assert result.total == 4
    assert result.inserted == 2
    assert result.duplicates == 2


@pytest.mark.parametrize(
    "corruption",
    ["header", "extra", "duplicate_header", "link", "step", "evidence", "nul", "field"],
)
def test_invalid_artifact_is_atomic(
    app_session_factory, project, users, tmp_path: Path, corruption: str
) -> None:
    with app_session_factory.begin() as session:
        steps = _job(session, project.id, users["operator"].id)
        good = _row(steps)
        bad = _row(steps, graph_fact_key="second")
        header = SCHEMA59_COLUMNS
        if corruption == "header":
            header = tuple(reversed(SCHEMA59_COLUMNS))
        elif corruption == "extra":
            header = (*SCHEMA59_COLUMNS, "unexpected")
            good["unexpected"] = "data"
            bad["unexpected"] = "data"
        elif corruption == "duplicate_header":
            bad = dict(zip(SCHEMA59_COLUMNS, SCHEMA59_COLUMNS, strict=True))
        elif corruption == "link":
            bad[FIELD["document_id"]] = str(uuid4())
        elif corruption == "step":
            bad[FIELD["step_id"]] = str(uuid4())
        elif corruption == "evidence":
            bad[FIELD["evidence_hash"]] = "0" * 64
        elif corruption == "nul":
            bad[FIELD["subject"]] = "bad\x00value"
        elif corruption == "field":
            bad[FIELD["subject"]] = "x" * 32_001
        path = _write(tmp_path / f"{corruption}.tsv", [good, bad], header)
        with pytest.raises(FactImportError):
            import_facts(session, steps["validate"].id, path)
        assert session.scalar(select(func.count()).select_from(RawFact)) == 0


def test_invalid_utf8_and_non_pipeline_step_are_rejected(
    app_session_factory, project, users, tmp_path: Path
) -> None:
    path = tmp_path / "invalid.tsv"
    path.write_bytes(b"\xff\xfe")
    with app_session_factory.begin() as session:
        steps = _job(session, project.id, users["operator"].id)
        with pytest.raises(FactImportError):
            import_facts(session, steps["validate"].id, path)
        valid = _write(tmp_path / "valid.tsv", [_row(steps)])
        with pytest.raises(FactImportError):
            import_facts(session, steps["rule_text"].id, valid)


def test_cross_project_batch_job_and_source_links_are_rejected(
    app_session_factory, project, foreign_project, users, tmp_path: Path
) -> None:
    with app_session_factory.begin() as session:
        local = _job(session, project.id, users["operator"].id)
        foreign = _job(session, foreign_project.id, users["operator"].id)
        row = _row(local)
        row[FIELD["document_id"]] = str(
            foreign["merge"].document_job.document_version_id
        )
        row[FIELD["step_id"]] = str(foreign["merge"].id)
        row["任务ID"] = str(foreign["merge"].document_job_id)
        row["批次ID"] = str(foreign["merge"].document_job.batch_id)
        path = _write(tmp_path / "cross-project.tsv", [row])
        with pytest.raises(FactImportError):
            import_facts(session, local["validate"].id, path)
        assert session.scalar(select(func.count()).select_from(RawFact)) == 0


def test_database_prevents_raw_fact_update_and_delete(
    app_session_factory, project, users, tmp_path: Path
) -> None:
    with app_session_factory.begin() as session:
        steps = _job(session, project.id, users["operator"].id)
        import_facts(session, steps["validate"].id, _write(tmp_path / "facts.tsv", [_row(steps)]))
    with app_session_factory() as session:
        fact_id = session.scalar(select(RawFact.id))
        with pytest.raises(IntegrityError):
            session.execute(update(RawFact).where(RawFact.id == fact_id).values(subject="changed"))
            session.commit()
