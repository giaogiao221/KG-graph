from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Mapping, Sequence, Tuple

from book_engine.core.schemas import ConditionalFactRecord
from book_engine.document.block_segmenter import TextBlock
from book_engine.gates.table_record_gate import RecordGateDecision
from book_engine.gates.text_fact_gate import TextGateDecision
from book_engine.text.text_fact_extractor import TextFactRecord


def write_book_coverage_report(
    output_dir: Path,
    *,
    table_blocks: Sequence[object],
    condition_results: Sequence[Tuple],
    table_decisions: Sequence[RecordGateDecision],
    text_blocks: Sequence[TextBlock],
    text_records: Sequence[TextFactRecord],
    text_decisions: Sequence[TextGateDecision],
    schema59_report: Mapping[str, object],
) -> dict:
    root = output_dir / "step_coverage"
    root.mkdir(parents=True, exist_ok=True)

    table_record_counts = Counter()
    table_status_counts = Counter()
    for result in condition_results:
        table_id = result[0].table_id
        for record in result[4]:
            table_record_counts[table_id] += 1
            table_status_counts[record.record_status] += 1

    gate_accepted_table_ids = {
        decision.table_id for decision in table_decisions if decision.accepted
    }
    table_hold_reasons = Counter(
        reason for decision in table_decisions if not decision.accepted for reason in decision.reasons
    )
    text_gate_reasons = Counter(
        reason for decision in text_decisions if not decision.accepted for reason in decision.reasons
    )
    role_counts = Counter(block.role or "unknown" for block in text_blocks)
    candidate_role_counts = Counter()
    accepted_role_counts = Counter()
    block_by_id = {block.block_id: block for block in text_blocks}
    decision_by_id = {decision.record_id: decision for decision in text_decisions}
    for record in text_records:
        role = (block_by_id.get(record.block_id).role if block_by_id.get(record.block_id) else "unknown") or "unknown"
        candidate_role_counts[role] += 1
        if decision_by_id.get(record.record_id) and decision_by_id[record.record_id].accepted:
            accepted_role_counts[role] += 1

    text_source_counts = Counter(record.source_type for record in text_records)
    accepted_text_source_counts = Counter(
        record.source_type for record in text_records
        if decision_by_id.get(record.record_id) and decision_by_id[record.record_id].accepted
    )

    report = {
        "ok": True,
        "stage": "book_candidate_gate_and_final_release_coverage_v2_phase103",
        "table": {
            "tables_total": len(table_blocks),
            "tables_with_structured_candidates": len(table_record_counts),
            "tables_with_gate_accepted_facts": len(gate_accepted_table_ids),
            "tables_with_released_facts": int(schema59_report.get("released_table_ids", 0) or 0),
            "conditional_records": sum(table_record_counts.values()),
            "record_status_counts": dict(table_status_counts),
            "gate_accepted_records": sum(1 for d in table_decisions if d.accepted),
            "gate_held_records": sum(1 for d in table_decisions if not d.accepted),
            "top_hold_reasons": dict(table_hold_reasons.most_common(20)),
        },
        "text": {
            "blocks_total": len(text_blocks),
            "blocks_with_candidates": len({r.block_id for r in text_records}),
            "blocks_with_gate_accepted_facts": len({
                r.block_id for r in text_records
                if decision_by_id.get(r.record_id) and decision_by_id[r.record_id].accepted
            }),
            "fact_candidates": len(text_records),
            "gate_accepted_records": sum(1 for d in text_decisions if d.accepted),
            "gate_held_records": sum(1 for d in text_decisions if not d.accepted),
            "block_role_counts": dict(role_counts),
            "candidate_role_counts": dict(candidate_role_counts),
            "accepted_role_counts": dict(accepted_role_counts),
            "candidate_source_counts": dict(text_source_counts),
            "accepted_source_counts": dict(accepted_text_source_counts),
            "top_hold_reasons": dict(text_gate_reasons.most_common(20)),
        },
        "schema59": {
            "released_rows": int(schema59_report.get("exported_rows", 0) or 0),
            "released_table_rows": int(schema59_report.get("released_table_rows", 0) or 0),
            "gate_accepted_text_rows_before_dedup": int(schema59_report.get("accepted_text_rows_before_dedup", 0) or 0),
            "released_text_rows": int(schema59_report.get("released_text_rows", 0) or 0),
            "release_consistency_candidate_rows": int(schema59_report.get("release_consistency_candidate_rows", 0) or 0),
            "candidate_rows": int(schema59_report.get("candidate_rows", 0) or 0),
            "held_table_candidate_rows": int(schema59_report.get("held_table_candidate_rows", 0) or 0),
            "held_text_candidate_rows": int(schema59_report.get("held_text_candidate_rows", 0) or 0),
            "rejected_rows": int(schema59_report.get("rejected_rows", 0) or 0),
        },
        "interpretation": {
            "candidate_generation": "All structured candidates are counted before production release.",
            "candidate_layer": "Evidence-complete held facts are preserved in graph_candidate_review_59.tsv.",
            "release_layer": "Only facts that pass semantic consistency and production scoring enter graph_import_ready.tsv.",
        },
    }

    (root / "book_coverage_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    rows = [
        {"section": "table", "metric": key, "value": value}
        for key, value in report["table"].items() if not isinstance(value, dict)
    ] + [
        {"section": "text", "metric": key, "value": value}
        for key, value in report["text"].items() if not isinstance(value, dict)
    ] + [
        {"section": "schema59", "metric": key, "value": value}
        for key, value in report["schema59"].items()
    ]
    with (root / "book_coverage_metrics.tsv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["section", "metric", "value"], delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)
    return report


__all__ = ["write_book_coverage_report"]
