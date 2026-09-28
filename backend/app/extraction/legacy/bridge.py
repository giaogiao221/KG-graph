from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Mapping

from app.extraction.contracts import AdapterContext, AdapterResult, PermanentAdapterError, ProgressEvent, ProgressSink
from app.extraction.native.common import MAX_CANDIDATE_ROWS, MAX_FIELD_CHARS, candidate_fact_id
from app.facts.schema59 import FIELD, SCHEMA59_COLUMNS, evidence_hash
from app.facts.script_schema59 import SCRIPT_SCHEMA59_COLUMNS, script_to_platform_values


def _read_script_rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t", strict=True)
            if tuple(reader.fieldnames or ()) != SCRIPT_SCHEMA59_COLUMNS:
                raise ValueError
            rows: list[dict[str, str]] = []
            for raw in reader:
                if len(rows) >= MAX_CANDIDATE_ROWS or None in raw or set(raw) != set(SCRIPT_SCHEMA59_COLUMNS):
                    raise ValueError
                row = {column: str(raw[column]) for column in SCRIPT_SCHEMA59_COLUMNS}
                if any("\x00" in value or len(value) > MAX_FIELD_CHARS for value in row.values()):
                    raise ValueError
                rows.append(row)
            return rows
    except (OSError, UnicodeError, csv.Error, TypeError, ValueError):
        raise PermanentAdapterError("KGchouqu script output is invalid") from None


def write_script_route_output(
    context: AdapterContext,
    route: str,
    source_tsv: Path,
    emit_progress: ProgressSink,
    *,
    executor: str,
    executor_version: str,
    metrics: Mapping[str, int | float] | None = None,
    source_kind: str | None = None,
    shared_artifact_name: str | None = None,
) -> AdapterResult:
    rows: list[dict[str, str]] = []
    source_rows = 0
    for source in _read_script_rows(source_tsv):
        if source_kind is not None:
            # The v105 engine emits one combined TSV; its 来源类型 column marks
            # 文本 (text) vs 表格 (table) facts.  Route-scoped legacy steps filter
            # the shared engine output so text and table facts stay on their own
            # platform route without re-running the engine.
            if source["来源类型"].strip() != source_kind:
                continue
            source_rows += 1
        row = script_to_platform_values(source)
        evidence = row[FIELD["evidence_text"]].strip()
        if not evidence:
            continue
        row[FIELD["document_id"]] = str(context.document_version_id or "")
        row[FIELD["step_id"]] = str(context.step_id or "")
        row[FIELD["route"]] = route
        row[FIELD["extraction_source"]] = route
        row[FIELD["execution_idempotency_key"]] = context.execution_idempotency_key
        row[FIELD["schema_version"]] = "schema59-v1"
        row[FIELD["model_config_id"]] = str(context.model_config_id or "")
        row[FIELD["review_status"]] = "candidate"
        row[FIELD["evidence_hash"]] = evidence_hash(evidence)
        row[SCHEMA59_COLUMNS[37]] = executor
        row[SCHEMA59_COLUMNS[38]] = executor_version
        row[FIELD["provenance"]] = json.dumps([{
            "route": route,
            "locator": row[FIELD["source_locator"]],
            "model_call": row[FIELD["model_call_id"]],
            "model_config_id": row[FIELD["model_config_id"]],
            "step_id": row[FIELD["step_id"]],
            "document_id": row[FIELD["document_id"]],
            "execution_key": context.execution_idempotency_key,
            "evidence": evidence,
        }], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        row["fact_id"] = candidate_fact_id(row)
        rows.append(row)

    route_dir = context.output_path(route)
    route_dir.mkdir(parents=True, exist_ok=True)
    candidate = route_dir / "candidates.schema59.tsv"
    try:
        with candidate.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=SCHEMA59_COLUMNS, delimiter="\t", lineterminator="\n", extrasaction="raise")
            writer.writeheader()
            writer.writerows(sorted(rows, key=lambda row: tuple(row[column] for column in SCHEMA59_COLUMNS)))
    except (OSError, csv.Error, ValueError):
        raise PermanentAdapterError("KGchouqu script output is invalid") from None

    shared_name: str | None = None
    if shared_artifact_name is not None:
        # Persist the combined engine TSV next to the route output so the
        # orchestrator can store it and sibling route steps can reuse it
        # without re-running the engine.
        shared_name = f"{shared_artifact_name}.tsv"
        shared_path = route_dir / shared_name
        try:
            shared_path.write_bytes(source_tsv.read_bytes())
        except OSError:
            raise PermanentAdapterError("KGchouqu script output is invalid") from None

    manifest = route_dir / "manifest.json"
    payload: dict[str, object] = {
        "candidate_tsv": candidate.name,
        "document_version_id": str(context.document_version_id or ""),
        "execution_idempotency_key": context.execution_idempotency_key,
        "records": len(rows),
        "route": route,
        "schema": "schema59-v1",
        "step_id": str(context.step_id or ""),
    }
    if shared_name is not None:
        payload["shared_source_tsv"] = shared_name
    manifest.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    emit_progress(ProgressEvent(stage=f"{executor}.completed", processed=len(rows), total=len(rows)))
    route_metrics: dict[str, int | float] = {"records": len(rows), **dict(metrics or {})}
    if source_kind is not None:
        route_metrics["source_rows"] = source_rows
    return AdapterResult(manifest, route_metrics)
