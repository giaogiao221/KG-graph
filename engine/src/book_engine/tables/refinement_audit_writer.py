from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List

from book_engine.tables.record_refiner import RefinementSummary


def _write_tsv(path: Path, fieldnames: List[str], rows: Iterable[Dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_table_refinement_outputs(output_dir: Path, summary: RefinementSummary) -> Dict[str, object]:
    step_dir = output_dir / "step_table_refinement"
    step_dir.mkdir(parents=True, exist_ok=True)

    event_rows = []
    action_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    for event in summary.events:
        action_counts[event.action] += 1
        reason_counts.update(event.reasons)
        event_rows.append({
            "old_record_id": event.old_record_id,
            "new_record_id": event.new_record_id,
            "table_id": event.table_id,
            "action": event.action,
            "original_subject": event.original_subject,
            "refined_subject": event.refined_subject,
            "original_property": event.original_property,
            "refined_property": event.refined_property,
            "original_value": event.original_value,
            "refined_value": event.refined_value,
            "original_unit": event.original_unit,
            "refined_unit": event.refined_unit,
            "original_status": event.original_status,
            "refined_status": event.refined_status,
            "reasons": " | ".join(event.reasons),
        })

    unresolved_rows = []
    unresolved_counts: Counter[str] = Counter()
    for item in summary.unresolved:
        reason = str(item.get("reason", ""))
        unresolved_counts[reason] += 1
        unresolved_rows.append({
            "record_id": item.get("record_id", ""),
            "table_id": item.get("table_id", ""),
            "row_index": item.get("row_index", ""),
            "column_index": item.get("column_index", ""),
            "reason": reason,
            "value": item.get("value", ""),
        })

    _write_tsv(
        step_dir / "table_record_refinement.tsv",
        [
            "old_record_id", "new_record_id", "table_id", "action", "original_subject", "refined_subject",
            "original_property", "refined_property", "original_value", "refined_value", "original_unit",
            "refined_unit", "original_status", "refined_status", "reasons",
        ],
        event_rows,
    )
    _write_tsv(
        step_dir / "table_refinement_unresolved.tsv",
        ["record_id", "table_id", "row_index", "column_index", "reason", "value"],
        unresolved_rows,
    )

    report = {
        "ok": True,
        "stage": "table_record_refinement_v2_phase5",
        "refinement_events": len(event_rows),
        "unresolved_items": len(unresolved_rows),
        "action_counts": dict(sorted(action_counts.items())),
        "reason_counts": dict(sorted(reason_counts.items())),
        "unresolved_reason_counts": dict(sorted(unresolved_counts.items())),
        "policy": {
            "axis_inversion": "repair_only_when_subject_is_exact_property_and_property_is_material_like",
            "multi_subject": "split_only_explicit_enumerations; slash_formulations_are_preserved",
            "numeric_series": "quarantine_ambiguous_concatenated_series",
            "property_unit": "separate_unit_from_attribute_name_before_export",
        },
    }
    (step_dir / "table_refinement_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


__all__ = ["write_table_refinement_outputs"]
