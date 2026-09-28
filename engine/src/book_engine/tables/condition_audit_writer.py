from __future__ import annotations

import csv
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

from book_engine.core.schemas import (
    ConditionBinding,
    ConditionCandidate,
    ConditionalFactRecord,
    TableBlock,
    TableGrid,
    TableSemanticPlan,
)
from book_engine.tables.context_metadata_extractor import ContextMetadata


def _write_tsv(path: Path, fieldnames: List[str], rows: Iterable[Dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _condition_text(record: ConditionalFactRecord) -> str:
    parts = []
    for atom in record.conditions:
        label = atom.normalized_name or atom.name
        parts.append(f"{label}={atom.value_text}")
    return "；".join(parts)


def write_table_condition_outputs(
    output_dir: Path,
    results: Sequence[
        Tuple[
            TableBlock,
            TableGrid,
            TableSemanticPlan,
            Sequence[ConditionCandidate],
            Sequence[ConditionalFactRecord],
            Sequence[ConditionBinding],
            Sequence[Dict[str, object]],
            ContextMetadata,
        ]
    ],
    errors: Sequence[Dict[str, object]],
) -> Dict[str, object]:
    step_dir = output_dir / "step_table_semantics"
    step_dir.mkdir(parents=True, exist_ok=True)

    candidate_rows: List[Dict[str, object]] = []
    binding_rows: List[Dict[str, object]] = []
    record_rows: List[Dict[str, object]] = []
    unresolved_rows: List[Dict[str, object]] = []
    candidate_source_counts: Counter[str] = Counter()
    condition_type_counts: Counter[str] = Counter()
    scope_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    topology_record_counts: Counter[str] = Counter()

    records_jsonl = step_dir / "table_conditional_records.jsonl"
    with records_jsonl.open("w", encoding="utf-8") as jsonl_handle:
        for block, grid, plan, candidates, records, bindings, unresolved, metadata in results:
            for candidate in candidates:
                candidate_source_counts[candidate.source_kind] += 1
                condition_type_counts[candidate.condition_type] += 1
                scope_counts[candidate.scope] += 1
                candidate_rows.append(
                    {
                        "condition_id": candidate.condition_id,
                        "table_id": candidate.table_id,
                        "name": candidate.name,
                        "normalized_name": candidate.normalized_name,
                        "condition_type": candidate.condition_type,
                        "value_text": candidate.value_text,
                        "value_num": candidate.value_num,
                        "lower_bound": candidate.lower_bound,
                        "upper_bound": candidate.upper_bound,
                        "comparator": candidate.comparator,
                        "unit": candidate.unit,
                        "scope": candidate.scope,
                        "target_row": candidate.target_row,
                        "target_column": candidate.target_column,
                        "source_kind": candidate.source_kind,
                        "source_text": candidate.source_text,
                        "priority": candidate.priority,
                        "confidence": f"{candidate.confidence:.4f}",
                        "reasons": " | ".join(candidate.reasons),
                    }
                )

            for binding in bindings:
                binding_rows.append(
                    {
                        "record_id": binding.record_id,
                        "condition_id": binding.condition_id,
                        "table_id": binding.table_id,
                        "row_index": binding.row_index,
                        "column_index": binding.column_index,
                        "binding_target": binding.binding_target,
                        "applied": "是" if binding.applied else "否",
                        "overridden": "是" if binding.overridden else "否",
                        "conflict": "是" if binding.conflict else "否",
                        "reason": binding.reason,
                    }
                )

            for record in records:
                status_counts[record.record_status] += 1
                topology_record_counts[plan.topology] += 1
                record_rows.append(
                    {
                        "record_id": record.record_id,
                        "table_id": record.table_id,
                        "row_index": record.row_index,
                        "column_index": record.column_index,
                        "topology": plan.topology,
                        "subject": record.subject,
                        "subject_type": record.subject_type,
                        "subject_source": record.subject_source,
                        "sample_id": record.sample_id,
                        "property_name": record.property_name,
                        "property_role": record.property_role,
                        "value_text": record.value_text,
                        "normalized_value_text": record.normalized_value_text,
                        "value_num": record.value_num,
                        "lower_bound": record.lower_bound,
                        "upper_bound": record.upper_bound,
                        "comparator": record.comparator,
                        "unit": record.unit,
                        "value_role": record.value_role,
                        "method": record.method,
                        "instrument": record.instrument,
                        "condition_text": _condition_text(record),
                        "condition_count": len(record.conditions),
                        "row_header_path": " > ".join(record.row_header_path),
                        "column_header_path": " > ".join(record.column_header_path),
                        "record_status": record.record_status,
                        "confidence": f"{record.confidence:.4f}",
                        "unresolved_reasons": " | ".join(record.unresolved_reasons),
                        "evidence": record.evidence,
                    }
                )
                jsonl_handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

            for item in unresolved:
                unresolved_rows.append(
                    {
                        "table_id": item.get("table_id", block.table_id),
                        "row_index": item.get("row_index", ""),
                        "column_index": item.get("column_index", ""),
                        "topology": plan.topology,
                        "reason": item.get("reason", ""),
                        "value": item.get("value", ""),
                        "heading": block.heading,
                    }
                )

    for error in errors:
        unresolved_rows.append(
            {
                "table_id": error.get("table_id", ""),
                "row_index": "",
                "column_index": "",
                "topology": "error",
                "reason": error.get("error", ""),
                "value": "",
                "heading": error.get("heading", ""),
            }
        )

    _write_tsv(
        step_dir / "table_condition_candidates.tsv",
        [
            "condition_id", "table_id", "name", "normalized_name", "condition_type", "value_text",
            "value_num", "lower_bound", "upper_bound", "comparator", "unit", "scope", "target_row",
            "target_column", "source_kind", "source_text", "priority", "confidence", "reasons",
        ],
        candidate_rows,
    )
    _write_tsv(
        step_dir / "table_condition_binding.tsv",
        [
            "record_id", "condition_id", "table_id", "row_index", "column_index", "binding_target",
            "applied", "overridden", "conflict", "reason",
        ],
        binding_rows,
    )
    _write_tsv(
        step_dir / "table_conditional_records.tsv",
        [
            "record_id", "table_id", "row_index", "column_index", "topology", "subject", "subject_type",
            "subject_source", "sample_id", "property_name", "property_role", "value_text",
            "normalized_value_text", "value_num", "lower_bound", "upper_bound", "comparator", "unit",
            "value_role", "method", "instrument", "condition_text", "condition_count", "row_header_path",
            "column_header_path", "record_status", "confidence", "unresolved_reasons", "evidence",
        ],
        record_rows,
    )
    _write_tsv(
        step_dir / "table_record_unresolved.tsv",
        ["table_id", "row_index", "column_index", "topology", "reason", "value", "heading"],
        unresolved_rows,
    )

    applied_bindings = sum(binding.applied for *_, bindings, _, _ in results for binding in bindings)
    conflict_bindings = sum(binding.conflict for *_, bindings, _, _ in results for binding in bindings)
    unique_record_ids = len({row["record_id"] for row in record_rows})
    ready_records = status_counts.get("ready", 0)
    report = {
        "ok": len(errors) == 0 and unique_record_ids == len(record_rows),
        "stage": "table_conditions_v2_phase3",
        "tables_processed": len(results),
        "tables_failed": len(errors),
        "condition_candidates": len(candidate_rows),
        "condition_bindings": len(binding_rows),
        "applied_condition_bindings": applied_bindings,
        "conflicting_condition_bindings": conflict_bindings,
        "conditional_records": len(record_rows),
        "unique_record_ids": unique_record_ids,
        "ready_records": ready_records,
        "candidate_records": status_counts.get("candidate", 0),
        "unresolved_records": status_counts.get("unresolved", 0),
        "record_status_counts": dict(sorted(status_counts.items())),
        "candidate_source_counts": dict(sorted(candidate_source_counts.items())),
        "condition_type_counts": dict(sorted(condition_type_counts.items())),
        "condition_scope_counts": dict(sorted(scope_counts.items())),
        "topology_record_counts": dict(sorted(topology_record_counts.items())),
        "unresolved_items": len(unresolved_rows),
        "errors": list(errors),
        "note": (
            "Phase 3 compiles auditable conditional records. Candidate and unresolved records are not ready for final graph import."
        ),
    }
    (step_dir / "table_condition_validation_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


__all__ = ["write_table_condition_outputs"]
