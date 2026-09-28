from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Mapping, Sequence

from app.extraction.contracts import (
    AdapterContext,
    AdapterResult,
    PermanentAdapterError,
    ProgressEvent,
    ProgressSink,
    QueueName,
)
from app.extraction.native.common import (
    MAX_CANDIDATE_ROWS,
    MAX_ROUTE_TSV_BYTES,
    MAX_FIELD_CHARS,
    FIELD,
    ROUTES,
    SCHEMA59_COLUMNS,
    canonical_evidence,
    candidate_fact_id,
    evidence_hash,
    _serialize_tsv,
    assert_no_links,
)


MAX_MERGE_TSV_BYTES = MAX_ROUTE_TSV_BYTES
MAX_MANIFEST_BYTES = 64_000
_ERROR = "native merge input is invalid"
_MANIFEST_FIELDS = {"candidate_tsv", "document_version_id", "execution_idempotency_key", "records", "route", "schema", "step_id"}
_PROVENANCE_FIELDS = {"route", "locator", "model_call", "model_config_id", "step_id", "document_id", "execution_key", "evidence"}
MAX_PROVENANCE_ITEMS = 16


@dataclass(frozen=True, slots=True)
class MergeSummary:
    input_routes: int
    input_rows: int
    output_rows: int
    deduplicated_rows: int
    conflict_rows: int


def _invalid() -> None:
    raise PermanentAdapterError(_ERROR)


def _safe_relative(value: object) -> tuple[str, ...]:
    if not isinstance(value, str) or not value:
        _invalid()
    posix, windows = PurePosixPath(value), PureWindowsPath(value)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        _invalid()
    parts = tuple(part for part in posix.parts if part not in ("", "."))
    if not parts or ".." in parts or ".." in windows.parts:
        _invalid()
    return parts


def _read_manifest(path: Path, allowed_root: Path | None = None) -> tuple[str, Path, int, str, str, str]:
    try:
        resolved = path.resolve(strict=True)
        assert_no_links(path)
        if allowed_root is not None:
            root = allowed_root.resolve(strict=True)
            if root not in resolved.parents:
                _invalid()
        if path.is_symlink() or not resolved.is_file() or resolved.stat().st_size > MAX_MANIFEST_BYTES:
            _invalid()
        raw = resolved.read_bytes()
        if len(raw) > MAX_MANIFEST_BYTES:
            _invalid()
        payload = json.loads(raw.decode("utf-8"))
        if (
            not isinstance(payload, dict)
            or set(payload) != _MANIFEST_FIELDS
            or payload["schema"] != "schema59-v1"
            or not isinstance(payload["execution_idempotency_key"], str)
            or not payload["execution_idempotency_key"].strip()
            or len(payload["execution_idempotency_key"]) > 256
            or (payload["step_id"] is not None and not isinstance(payload["step_id"], str))
            or (payload["document_version_id"] is not None and not isinstance(payload["document_version_id"], str))
            or isinstance(payload["records"], bool)
            or not isinstance(payload["records"], int)
            or payload["records"] < 0
            or payload["records"] > MAX_CANDIDATE_ROWS
        ):
            _invalid()
        route = payload["route"]
        if route not in ROUTES:
            _invalid()
        candidate_input = resolved.parent.joinpath(*_safe_relative(payload["candidate_tsv"]))
        assert_no_links(candidate_input)
        candidate = candidate_input.resolve(strict=True)
        if resolved.parent != candidate.parent or not candidate.is_file():
            _invalid()
        return route, candidate, payload["records"], payload["execution_idempotency_key"], payload["step_id"] or "", payload["document_version_id"] or ""
    except PermanentAdapterError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        raise PermanentAdapterError(_ERROR) from None


def _valid_provenance(value: str, route: str, evidence: str) -> bool:
    try:
        payload = json.loads(value)
        return (
            isinstance(payload, list)
            and 0 < len(payload) <= MAX_PROVENANCE_ITEMS
            and all(
                isinstance(item, dict)
                and set(item) == _PROVENANCE_FIELDS
                and item["route"] == route
                and item["evidence"] == evidence
                and all(isinstance(field, str) and len(field) <= MAX_FIELD_CHARS for field in item.values())
                for item in payload
            )
        )
    except (json.JSONDecodeError, TypeError):
        return False


def _has_forbidden_control(value: str) -> bool:
    return any((ord(character) < 32 and character not in "\t\r\n") or ord(character) == 127 for character in value)


def _read_rows(path: Path, route: str, execution_key: str, step_id: str, document_id: str) -> list[dict[str, str]]:
    try:
        if path.stat().st_size > MAX_MERGE_TSV_BYTES:
            _invalid()
        raw = path.read_bytes()
        if len(raw) > MAX_MERGE_TSV_BYTES:
            _invalid()
        text = raw.decode("utf-8")
        reader = csv.DictReader(io.StringIO(text, newline=""), delimiter="\t", strict=True)
        if tuple(reader.fieldnames or ()) != SCHEMA59_COLUMNS:
            _invalid()
        rows: list[dict[str, str]] = []
        for parsed in reader:
            if len(rows) >= MAX_CANDIDATE_ROWS or None in parsed or set(parsed) != set(SCHEMA59_COLUMNS):
                _invalid()
            row = {name: parsed[name] for name in SCHEMA59_COLUMNS}
            if any(len(value) > MAX_FIELD_CHARS or _has_forbidden_control(value) for value in row.values()):
                _invalid()
            if not row[FIELD["evidence_text"]].strip() or row[FIELD["route"]] != route:
                _invalid()
            if row[FIELD["execution_idempotency_key"]] != execution_key or row[FIELD["step_id"]] != step_id or row[FIELD["document_id"]] != document_id or row[FIELD["schema_version"]] != "schema59-v1" or row[FIELD["extraction_source"]] != route:
                _invalid()
            if row[FIELD["evidence_hash"]] != evidence_hash(row[FIELD["evidence_text"]]):
                _invalid()
            if row["fact_id"] != candidate_fact_id(row):
                _invalid()
            if not _valid_provenance(row[FIELD["provenance"]], route, row[FIELD["evidence_text"]]):
                _invalid()
            rows.append(row)
        return rows
    except PermanentAdapterError:
        raise
    except (OSError, UnicodeError, csv.Error, KeyError, TypeError):
        raise PermanentAdapterError(_ERROR) from None


def _fact_signature(row: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(row[FIELD[name]].strip().casefold() for name in ("subject", "property", "value", "unit", "condition"))


def _merge_duplicate(target: dict[str, str], incoming: Mapping[str, str]) -> None:
    routes = sorted(set(target[FIELD["route"]].split(",")) | set(incoming[FIELD["route"]].split(",")))
    target[FIELD["route"]] = ",".join(routes)
    for column in SCHEMA59_COLUMNS:
        if not target[column] and incoming[column]:
            target[column] = incoming[column]
    try:
        provenance = json.loads(target[FIELD["provenance"]]) + json.loads(incoming[FIELD["provenance"]])
        unique = {json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")): item for item in provenance}
        combined = [unique[key] for key in sorted(unique)]
        encoded = json.dumps(combined, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(combined) > MAX_PROVENANCE_ITEMS or len(encoded) > MAX_FIELD_CHARS:
            _invalid()
        target[FIELD["provenance"]] = encoded
    except (json.JSONDecodeError, TypeError):
        _invalid()
    choices = sorted((target[FIELD["evidence_text"]], incoming[FIELD["evidence_text"]]), key=lambda value:(canonical_evidence(value),value))
    target[FIELD["evidence_text"]] = choices[0]
    target[FIELD["evidence_hash"]] = evidence_hash(choices[0])


def _write_tsv(path: Path, rows: Sequence[Mapping[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_bytes(_serialize_tsv(rows))
        os.replace(temporary, path)
    except (OSError, csv.Error, ValueError):
        temporary.unlink(missing_ok=True)
        raise PermanentAdapterError("native merge output is invalid") from None


def merge_route_outputs(results: Sequence[AdapterResult], output: Path, *, allowed_root: Path | None = None, output_context: AdapterContext | None = None) -> MergeSummary:
    if not results:
        _invalid()
    loaded: list[dict[str, str]] = []
    routes: set[str] = set()
    for result in results:
        route, candidate, expected_records, execution_key, step_id, document_id = _read_manifest(result.manifest_path, allowed_root)
        if route in routes:
            _invalid()
        routes.add(route)
        rows = _read_rows(candidate, route, execution_key, step_id, document_id)
        if len(rows) != expected_records:
            _invalid()
        loaded.extend(rows)
        if len(loaded) > MAX_CANDIDATE_ROWS:
            _invalid()
    loaded.sort(key=lambda row: tuple(row[name] for name in SCHEMA59_COLUMNS))

    merged: list[dict[str, str]] = []
    graph_index: dict[tuple[str, tuple[str, ...]], dict[str, str]] = {}
    evidence_index: dict[tuple[str, tuple[str, ...]], dict[str, str]] = {}
    for row in loaded:
        signature = _fact_signature(row)
        graph_key = row["graph_fact_key"].strip().casefold()
        duplicate = graph_index.get((graph_key, signature)) if graph_key else None
        if duplicate is None:
            duplicate = evidence_index.get((row[FIELD["evidence_hash"]], signature))
        if duplicate is not None:
            _merge_duplicate(duplicate, row)
            if graph_key:
                graph_index[(graph_key, signature)] = duplicate
            evidence_index[(duplicate[FIELD["evidence_hash"]], signature)] = duplicate
            continue
        copy = dict(row)
        merged.append(copy)
        if graph_key:
            graph_index[(graph_key, signature)] = copy
        evidence_index[(copy[FIELD["evidence_hash"]], signature)] = copy

    by_graph: dict[str, list[dict[str, str]]] = {}
    for row in merged:
        if row["graph_fact_key"]:
            by_graph.setdefault(row["graph_fact_key"].strip().casefold(), []).append(row)
    conflict_rows = 0
    for graph_key, group in by_graph.items():
        if len({_fact_signature(row) for row in group}) <= 1:
            continue
        conflict_group = hashlib.sha256(graph_key.encode("utf-8")).hexdigest()[:24]
        for row in group:
            row[FIELD["review_status"]] = "candidate_review"
            row[FIELD["conflict_group"]] = conflict_group
            conflict_rows += 1

    for row in merged:
        if output_context is not None:
            row[FIELD["route"]] = "merge"
            row[FIELD["extraction_source"]] = "merge"
            row[FIELD["step_id"]] = str(output_context.step_id) if output_context.step_id else ""
            row[FIELD["document_id"]] = str(output_context.document_version_id) if output_context.document_version_id else ""
            row[FIELD["execution_idempotency_key"]] = output_context.execution_idempotency_key
            row[FIELD["model_config_id"]] = ""
            row[FIELD["model_call_id"]] = ""
            row[SCHEMA59_COLUMNS[37]] = "native"
            row[SCHEMA59_COLUMNS[38]] = "native-1"
            row[SCHEMA59_COLUMNS[56]] = __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat()
            row[FIELD["evidence_hash"]] = evidence_hash(row[FIELD["evidence_text"]])
        row["fact_id"] = candidate_fact_id(row)
        if row["fact_id"] != candidate_fact_id(row):
            _invalid()

    merged.sort(key=lambda row: tuple(row[name] for name in ("graph_fact_key", FIELD["subject"], FIELD["property"], FIELD["value"], FIELD["unit"], FIELD["evidence_hash"], FIELD["route"])))
    _write_tsv(output, merged)
    return MergeSummary(
        input_routes=len(routes),
        input_rows=len(loaded),
        output_rows=len(merged),
        deduplicated_rows=len(loaded) - len(merged),
        conflict_rows=conflict_rows,
    )


@dataclass(frozen=True, slots=True)
class MergeAdapter:
    name: str = "merge"
    version: str = "native-1"
    queue: QueueName = "rule"

    def run(self, context: AdapterContext, emit_progress: ProgressSink) -> AdapterResult:
        manifests = context.route_result_manifests
        if not manifests:
            raw = context.profile_snapshot.get("route_results", ())
            if isinstance(raw, (list, tuple)) and all(isinstance(value, str) for value in raw):
                manifests = tuple(Path(value).resolve() for value in raw)
        results = tuple(AdapterResult(path, {"records": 0}) for path in manifests)
        merge_dir = context.output_path("merge")
        merge_dir.mkdir(parents=True, exist_ok=True)
        candidate = merge_dir / "candidates.schema59.tsv"
        summary = merge_route_outputs(results, candidate, allowed_root=context.work_dir, output_context=context)
        manifest_path = merge_dir / "manifest.json"
        payload = {
            "candidate_tsv": candidate.name,
            "document_version_id": str(context.document_version_id) if context.document_version_id else None,
            "execution_idempotency_key": context.execution_idempotency_key,
            "records": summary.output_rows,
            "route": "merge",
            "schema": "schema59-v1",
            "step_id": str(context.step_id) if context.step_id else None,
        }
        temporary = manifest_path.with_name(manifest_path.name + ".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, manifest_path)
        emit_progress(ProgressEvent(stage="merge.completed", processed=summary.output_rows, total=summary.output_rows))
        return AdapterResult(manifest_path, {"records": summary.output_rows, "input_routes": summary.input_routes, "deduplicated_rows": summary.deduplicated_rows, "conflict_rows": summary.conflict_rows})
