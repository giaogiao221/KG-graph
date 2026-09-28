from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Mapping, Sequence

from book_engine.quality.production_release_gate import ProductionGateResult


_DECISION_COLUMNS = [
    "fact_id",
    "graph_fact_key",
    "action",
    "deterministic_action",
    "score",
    "hard_reject_reasons",
    "review_reasons",
    "positive_reasons",
    "llm_used",
    "llm_action",
    "llm_confidence",
    "llm_status",
    "llm_reason",
]


def _write_tsv(path: Path, rows: Sequence[Mapping[str, object]], columns: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n")


def write_production_release_outputs(output_dir: Path, result: ProductionGateResult) -> dict[str, object]:
    target = output_dir / "step_production_release"
    target.mkdir(parents=True, exist_ok=True)

    decision_rows = []
    for item in result.decisions:
        decision_rows.append(
            {
                "fact_id": item.fact_id,
                "graph_fact_key": item.graph_fact_key,
                "action": item.action,
                "deterministic_action": item.deterministic_action,
                "score": f"{item.score:.6f}",
                "hard_reject_reasons": "|".join(item.hard_reject_reasons),
                "review_reasons": "|".join(item.review_reasons),
                "positive_reasons": "|".join(item.positive_reasons),
                "llm_used": "1" if item.llm_used else "0",
                "llm_action": item.llm_action,
                "llm_confidence": f"{item.llm_confidence:.6f}" if item.llm_used else "",
                "llm_status": item.llm_status,
                "llm_reason": item.llm_reason,
            }
        )
    _write_tsv(target / "production_release_decisions.tsv", decision_rows, _DECISION_COLUMNS)
    _write_jsonl(target / "production_llm_queue.jsonl", result.llm_queue)
    _write_jsonl(target / "production_llm_responses.jsonl", result.llm_responses)

    action_counts = Counter(item.action for item in result.decisions)
    deterministic_counts = Counter(item.deterministic_action for item in result.decisions)
    hard_reason_counts = Counter(reason for item in result.decisions for reason in item.hard_reject_reasons)
    review_reason_counts = Counter(reason for item in result.decisions for reason in item.review_reasons)
    report = {
        "ok": True,
        "stage": "automatic_production_release_gate_v2_phase9",
        "profile": result.profile,
        "input_rows": len(result.decisions),
        "released_rows": len(result.released_rows),
        "candidate_rows": len(result.candidate_rows),
        "rejected_rows": len(result.rejected_rows),
        "action_counts": dict(action_counts),
        "deterministic_action_counts": dict(deterministic_counts),
        "hard_reject_reason_counts": dict(hard_reason_counts),
        "review_reason_counts": dict(review_reason_counts),
        "llm_available": result.llm_available,
        "llm_queue_rows": len(result.llm_queue),
        "llm_response_rows": len(result.llm_responses),
        "fail_closed": True,
        "final_schema_columns_changed": False,
        "note": (
            "Only release rows are written to graph_import_ready.tsv. Candidate and rejected rows remain in audit files. "
            "The final graph schema remains the captured legacy 59-column contract."
        ),
    }
    (target / "production_release_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


__all__ = ["write_production_release_outputs"]
