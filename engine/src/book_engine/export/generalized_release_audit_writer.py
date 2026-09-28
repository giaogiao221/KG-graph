from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Sequence

from book_engine.quality.generalized_release_gate import GeneralizedReleaseResult


def write_generalized_release_outputs(output_dir: Path, result: GeneralizedReleaseResult) -> dict[str, object]:
    audit_dir = output_dir / "step_generalized_release"
    audit_dir.mkdir(parents=True, exist_ok=True)
    decisions_path = audit_dir / "generalized_release_decisions.tsv"
    fields = [
        "fact_id", "graph_fact_key", "action", "score", "threshold", "source_type", "origin_stream",
        "strict_released", "gate_reasons", "semantic_reasons", "hard_reasons", "soft_reasons", "positive_reasons",
    ]
    with decisions_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for item in result.decisions:
            writer.writerow({
                "fact_id": item.fact_id,
                "graph_fact_key": item.graph_fact_key,
                "action": item.action,
                "score": f"{item.score:.6f}",
                "threshold": f"{item.threshold:.4f}",
                "source_type": item.source_type,
                "origin_stream": item.origin_stream,
                "strict_released": "是" if item.strict_released else "否",
                "gate_reasons": ";".join(item.gate_reasons),
                "semantic_reasons": ";".join(item.semantic_reasons),
                "hard_reasons": ";".join(item.hard_reasons),
                "soft_reasons": ";".join(item.soft_reasons),
                "positive_reasons": ";".join(item.positive_reasons),
            })
    action_counts = Counter(item.action for item in result.decisions)
    source_release_counts = Counter(
        item.source_type for item in result.decisions if item.action == "release"
    )
    hard_reason_counts = Counter(
        reason for item in result.decisions for reason in item.hard_reasons
    )
    soft_reason_counts = Counter(
        reason for item in result.decisions for reason in item.soft_reasons
    )
    report = {
        "ok": True,
        "stage": "phase105_generalized_release",
        "profile": result.profile,
        "input_rows": len(result.decisions),
        "released_rows": len(result.released_rows),
        "candidate_rows": len(result.candidate_rows),
        "rejected_rows": len(result.rejected_rows),
        "action_counts": dict(action_counts),
        "released_by_source": dict(source_release_counts),
        "hard_reason_counts": dict(hard_reason_counts.most_common()),
        "soft_reason_counts": dict(soft_reason_counts.most_common()),
        "decisions_path": str(decisions_path),
    }
    report_path = audit_dir / "generalized_release_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8-sig")
    report["report_path"] = str(report_path)
    return report


__all__ = ["write_generalized_release_outputs"]
