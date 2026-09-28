from __future__ import annotations

import csv
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Dict, Mapping, Sequence

from book_engine.export.cross_source_deduplicator import DedupAudit
from book_engine.routing.subject_type_harmonizer import SubjectTypeAudit
from book_engine.text.text_condition_extractor import TextConditionAudit


def _write_tsv(path: Path, rows: Sequence[Mapping[str, object]], columns: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_phase7_quality_outputs(
    output_dir: Path,
    final_rows: Sequence[Mapping[str, object]],
    dedup_audits: Sequence[DedupAudit],
    type_audits: Sequence[SubjectTypeAudit],
    text_condition_audits: Sequence[TextConditionAudit],
    conflicts: Sequence[Mapping[str, object]],
) -> Dict[str, object]:
    quality_dir = output_dir / "step_quality"
    quality_dir.mkdir(parents=True, exist_ok=True)

    _write_tsv(
        quality_dir / "cross_source_dedup.tsv",
        [asdict(item) for item in dedup_audits],
        ("cluster_id", "kept_fact_id", "dropped_fact_id", "kept_source", "dropped_source", "reason", "semantic_key"),
    )
    _write_tsv(
        quality_dir / "subject_type_harmonization.tsv",
        [asdict(item) for item in type_audits],
        ("source", "record_id", "canonical_subject", "old_type", "new_type", "changed", "reason"),
    )
    _write_tsv(
        quality_dir / "text_condition_binding.tsv",
        [asdict(item) for item in text_condition_audits],
        ("record_id", "block_id", "condition_name", "condition_value", "unit", "source_text", "action", "reason", "confidence"),
    )
    _write_tsv(
        quality_dir / "semantic_conflicts.tsv",
        list(conflicts),
        ("conflict_id", "semantic_base", "distinct_value_count", "values_json"),
    )

    source_counts = Counter(str(row.get("来源类型", "")) for row in final_rows)
    subject_type_counts = Counter(str(row.get("主体类型", "")) for row in final_rows)
    def is_numeric_row(row: Mapping[str, object]) -> bool:
        return bool(
            str(row.get("数值", "")).strip()
            or str(row.get("范围下限", "")).strip()
            or str(row.get("范围上限", "")).strip()
        )

    numeric_rows = [row for row in final_rows if is_numeric_row(row)]
    conditioned_numeric = [row for row in numeric_rows if str(row.get("条件文本", "")).strip()]
    missing_required = [
        str(row.get("fact_id", ""))
        for row in final_rows
        if not str(row.get("主体名称", "")).strip()
        or not str(row.get("attribute_name", "")).strip()
        or not str(row.get("尾实体/取值文本", "")).strip()
    ]
    visibility_errors = [
        str(row.get("fact_id", ""))
        for row in final_rows
        if (is_numeric_row(row) and str(row.get("value_hidden", "")) != "是")
        or ((not is_numeric_row(row)) and str(row.get("value_hidden", "")) == "是")
    ]
    duplicate_fact_ids = len(final_rows) - len({str(row.get("fact_id", "")) for row in final_rows})
    duplicate_graph_keys = len(final_rows) - len({str(row.get("graph_fact_key", "")) for row in final_rows})

    hard_errors = []
    if missing_required:
        hard_errors.append(f"missing_required_rows:{len(missing_required)}")
    if visibility_errors:
        hard_errors.append(f"value_visibility_errors:{len(visibility_errors)}")
    if duplicate_fact_ids:
        hard_errors.append(f"duplicate_fact_ids:{duplicate_fact_ids}")
    if duplicate_graph_keys:
        hard_errors.append(f"duplicate_graph_fact_keys:{duplicate_graph_keys}")

    report = {
        "ok": not hard_errors,
        "stage": "phase7_cross_source_quality",
        "final_rows": len(final_rows),
        "source_counts": dict(sorted(source_counts.items())),
        "subject_type_counts": dict(sorted(subject_type_counts.items())),
        "numeric_rows": len(numeric_rows),
        "conditioned_numeric_rows": len(conditioned_numeric),
        "condition_coverage_ratio": round(len(conditioned_numeric) / len(numeric_rows), 6) if numeric_rows else 0.0,
        "cross_source_duplicates_removed": len(dedup_audits),
        "subject_type_changes": sum(1 for item in type_audits if item.changed),
        "text_conditions_bound": len(text_condition_audits),
        "semantic_conflict_groups": len(conflicts),
        "missing_required_rows": len(missing_required),
        "value_visibility_errors": len(visibility_errors),
        "duplicate_fact_ids": duplicate_fact_ids,
        "duplicate_graph_fact_keys": duplicate_graph_keys,
        "hard_errors": hard_errors,
        "warning": "Semantic conflicts are retained for audit because different values may represent different experiments or source contexts.",
    }
    (quality_dir / "semantic_quality_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


__all__ = ["write_phase7_quality_outputs"]
