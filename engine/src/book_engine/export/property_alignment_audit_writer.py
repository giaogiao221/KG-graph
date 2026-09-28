from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from book_engine.gates.property_alignment_gate import summarize_property_gate
from book_engine.ontology.property_alignment_engine import PropertyAlignmentDecision


def _write_tsv(path: Path, fieldnames: Sequence[str], rows: Sequence[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_property_alignment_outputs(output_dir: Path, decisions: Sequence[PropertyAlignmentDecision]) -> dict[str, object]:
    stage_dir = output_dir / "step_property_alignment"
    stage_dir.mkdir(parents=True, exist_ok=True)

    decision_rows = []
    candidate_rows = []
    unresolved_rows = []
    llm_queue = []

    for decision in decisions:
        row = {
            "row_ref": decision.row_ref,
            "raw_property": decision.raw_property,
            "cleaned_property": decision.cleaned_property,
            "property_id": decision.property_id,
            "canonical_name": decision.canonical_name,
            "attribute_category": decision.attribute_category,
            "ontology_path": decision.ontology_path,
            "status": decision.status,
            "confidence": f"{decision.confidence:.6f}",
            "source": decision.source,
            "accepted": "1" if decision.accepted else "0",
            "llm_used": "1" if decision.llm_used else "0",
            "llm_status": decision.llm_status,
            "decision_reason": decision.decision_reason,
        }
        decision_rows.append(row)
        for rank, candidate in enumerate(decision.candidates, start=1):
            candidate_rows.append(
                {
                    "row_ref": decision.row_ref,
                    "rank": rank,
                    "raw_property": decision.raw_property,
                    "property_id": candidate.property_id,
                    "canonical_name": candidate.canonical_name,
                    "attribute_category": candidate.attribute_category,
                    "root_system": candidate.root_system,
                    "ontology_path": candidate.ontology_path,
                    "source": candidate.source,
                    "match_type": candidate.match_type,
                    "score": f"{candidate.score:.6f}",
                    "reasons": ";".join(candidate.reasons),
                }
            )
        if decision.status in {"ambiguous", "unmapped", "rejected_non_property_label"}:
            unresolved_rows.append(row)
        if decision.status in {"ambiguous", "unmapped"}:
            llm_queue.append(
                {
                    "row_ref": decision.row_ref,
                    "raw_property": decision.raw_property,
                    "cleaned_property": decision.cleaned_property,
                    "status": decision.status,
                    "candidates": [asdict(item) for item in decision.candidates],
                    "constraint": "select only supplied property_id or unresolved",
                }
            )

    _write_tsv(
        stage_dir / "property_alignment_decisions.tsv",
        [
            "row_ref", "raw_property", "cleaned_property", "property_id", "canonical_name",
            "attribute_category", "ontology_path", "status", "confidence", "source", "accepted",
            "llm_used", "llm_status", "decision_reason",
        ],
        decision_rows,
    )
    _write_tsv(
        stage_dir / "property_candidates.tsv",
        [
            "row_ref", "rank", "raw_property", "property_id", "canonical_name", "attribute_category",
            "root_system", "ontology_path", "source", "match_type", "score", "reasons",
        ],
        candidate_rows,
    )
    _write_tsv(
        stage_dir / "property_unresolved.tsv",
        [
            "row_ref", "raw_property", "cleaned_property", "property_id", "canonical_name",
            "attribute_category", "ontology_path", "status", "confidence", "source", "accepted",
            "llm_used", "llm_status", "decision_reason",
        ],
        unresolved_rows,
    )
    with (stage_dir / "property_llm_queue.jsonl").open("w", encoding="utf-8") as handle:
        for item in llm_queue:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")

    report = summarize_property_gate(decisions)
    report.update(
        {
            "stage": "property_alignment_v2_phase8a",
            "decision_file": str(stage_dir / "property_alignment_decisions.tsv"),
            "candidate_file": str(stage_dir / "property_candidates.tsv"),
            "unresolved_file": str(stage_dir / "property_unresolved.tsv"),
            "llm_queue_file": str(stage_dir / "property_llm_queue.jsonl"),
            "final_schema_columns_changed": False,
        }
    )
    (stage_dir / "property_alignment_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


__all__ = ["write_property_alignment_outputs"]
