from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List, Sequence

from book_engine.gates.table_record_gate import RecordGateDecision
from book_engine.routing.subject_registry_builder import SubjectRegistryEntry, SubjectResolution


def _write_tsv(path: Path, fieldnames: Sequence[str], rows: Iterable[Dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_subject_and_gate_outputs(
    output_dir: Path,
    entries: Sequence[SubjectRegistryEntry],
    resolutions: Sequence[SubjectResolution],
    gate_decisions: Sequence[RecordGateDecision],
) -> Dict[str, object]:
    registry_dir = output_dir / "step_subject_registry"
    release_dir = output_dir / "step_release"
    graph_dir = output_dir / "step_graph_guard"
    registry_dir.mkdir(parents=True, exist_ok=True)
    release_dir.mkdir(parents=True, exist_ok=True)
    graph_dir.mkdir(parents=True, exist_ok=True)

    entry_rows = []
    for item in entries:
        entry_rows.append({
            "subject_id": item.subject_id,
            "canonical_name": item.canonical_name,
            "normalized_key": item.normalized_key,
            "subject_type": item.subject_type,
            "status": item.status,
            "score": f"{item.score:.4f}",
            "type_confidence": f"{item.type_confidence:.4f}",
            "aliases": " | ".join(item.aliases),
            "evidence_count": item.evidence_count,
            "table_count": item.table_count,
            "source_counts": json.dumps(item.source_counts, ensure_ascii=False, sort_keys=True),
            "reasons": " | ".join(item.reasons),
            "negative_reasons": " | ".join(item.negative_reasons),
        })
    _write_tsv(
        registry_dir / "subject_candidates.tsv",
        ["subject_id", "canonical_name", "normalized_key", "subject_type", "status", "score", "type_confidence",
         "aliases", "evidence_count", "table_count", "source_counts", "reasons", "negative_reasons"],
        entry_rows,
    )

    confirmed_rows = [row for row, item in zip(entry_rows, entries) if item.status == "confirmed"]
    _write_tsv(
        release_dir / "subject_registry.tsv",
        ["subject_id", "canonical_name", "normalized_key", "subject_type", "status", "score", "type_confidence",
         "aliases", "evidence_count", "table_count", "source_counts", "reasons", "negative_reasons"],
        confirmed_rows,
    )

    _write_tsv(
        registry_dir / "subject_resolution.tsv",
        ["record_id", "table_id", "raw_subject", "canonical_name", "normalized_key", "subject_id", "subject_type",
         "registry_status", "action", "score", "reasons"],
        ({
            "record_id": item.record_id,
            "table_id": item.table_id,
            "raw_subject": item.raw_subject,
            "canonical_name": item.canonical_name,
            "normalized_key": item.normalized_key,
            "subject_id": item.subject_id,
            "subject_type": item.subject_type,
            "registry_status": item.registry_status,
            "action": item.action,
            "score": f"{item.score:.4f}",
            "reasons": " | ".join(item.reasons),
        } for item in resolutions),
    )

    _write_tsv(
        graph_dir / "table_record_gate.tsv",
        ["record_id", "table_id", "accepted", "action", "canonical_subject", "subject_id", "subject_type",
         "original_status", "final_status", "confidence", "reasons"],
        ({
            "record_id": item.record_id,
            "table_id": item.table_id,
            "accepted": "是" if item.accepted else "否",
            "action": item.action,
            "canonical_subject": item.canonical_subject,
            "subject_id": item.subject_id,
            "subject_type": item.subject_type,
            "original_status": item.original_status,
            "final_status": item.final_status,
            "confidence": f"{item.confidence:.4f}",
            "reasons": " | ".join(item.reasons),
        } for item in gate_decisions),
    )

    status_counts = Counter(item.status for item in entries)
    type_counts = Counter(item.subject_type for item in entries if item.status == "confirmed")
    gate_counts = Counter("accepted" if item.accepted else "held" for item in gate_decisions)
    hold_reasons = Counter(reason for item in gate_decisions if not item.accepted for reason in item.reasons)
    report = {
        "ok": True,
        "stage": "subject_registry_and_record_gate_v2_phase4",
        "subject_candidates": len(entries),
        "subject_status_counts": dict(sorted(status_counts.items())),
        "confirmed_subject_type_counts": dict(sorted(type_counts.items())),
        "record_gate_counts": dict(sorted(gate_counts.items())),
        "top_hold_reasons": dict(hold_reasons.most_common(30)),
        "note": "Only records linked to confirmed subjects and passing the hard gate are exported.",
    }
    (registry_dir / "subject_registry_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


__all__ = ["write_subject_and_gate_outputs"]
