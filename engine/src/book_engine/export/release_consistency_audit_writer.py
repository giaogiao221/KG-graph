from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Dict

from book_engine.quality.semantic_release_consistency import SemanticReleaseGuardResult


def write_release_consistency_outputs(output_dir: Path, result: SemanticReleaseGuardResult) -> Dict[str, object]:
    root = output_dir / "step_release_consistency"
    root.mkdir(parents=True, exist_ok=True)
    audit_path = root / "release_consistency_audit.tsv"
    fields = [
        "fact_id", "graph_fact_key", "source_type", "table_id", "subject",
        "property_name", "value_text", "action", "reasons",
    ]
    with audit_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for item in result.audits:
            writer.writerow({
                "fact_id": item.fact_id,
                "graph_fact_key": item.graph_fact_key,
                "source_type": item.source_type,
                "table_id": item.table_id,
                "subject": item.subject,
                "property_name": item.property_name,
                "value_text": item.value_text,
                "action": item.action,
                "reasons": "|".join(item.reasons),
            })
    reason_counts = Counter(reason for item in result.audits for reason in item.reasons)
    report = {
        "ok": True,
        "stage": "narrative_semantic_compilers_release_v2_phase103",
        "input_rows": len(result.audits),
        "pass_rows": len(result.releasable_rows),
        "candidate_rows": len(result.candidate_rows),
        "rejected_rows": len(result.rejected_rows),
        "tables_quarantined": len(result.table_candidate_reasons),
        "table_candidate_reasons": {key: list(value) for key, value in result.table_candidate_reasons.items()},
        "reason_counts": dict(reason_counts),
        "audit_file": str(audit_path),
        "policy": "Unsafe rows are preserved in the 59-column candidate layer; the guard never invents corrected values.",
    }
    report_path = root / "release_consistency_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


__all__ = ["write_release_consistency_outputs"]
