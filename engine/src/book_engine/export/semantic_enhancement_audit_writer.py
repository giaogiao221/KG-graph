from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Sequence

from book_engine.document.semantic_block_splitter import BlockSplitAudit
from book_engine.routing.text_subject_anchor_resolver import SubjectAnchorAudit
from book_engine.text.process_step_extractor import ProcessStepAudit


def _write_tsv(path: Path, rows, columns) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, delimiter="\t", extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            safe = {}
            for key in columns:
                value = str(row.get(key, "") or "")
                safe[key] = value.replace("\t", "\\t").replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n")
            writer.writerow(safe)


def write_semantic_enhancement_outputs(
    output_dir: Path,
    split_audits: Sequence[BlockSplitAudit],
    anchor_audits: Sequence[SubjectAnchorAudit],
    process_audits: Sequence[ProcessStepAudit],
) -> dict:
    subject_root = output_dir / "step_subject_anchor"
    process_root = output_dir / "step_process_semantics"

    _write_tsv(
        subject_root / "semantic_block_splits.tsv",
        [
            {
                "parent_block_id": item.parent_block_id,
                "child_block_id": item.child_block_id,
                "action": item.action,
                "reason": item.reason,
                "subjects": " | ".join(item.subjects),
                "llm_used": "是" if item.llm_used else "否",
                "llm_status": item.llm_status,
                "llm_confidence": f"{item.llm_confidence:.4f}",
                "text": item.text,
            }
            for item in split_audits
        ],
        ["parent_block_id", "child_block_id", "action", "reason", "subjects", "llm_used", "llm_status", "llm_confidence", "text"],
    )
    _write_tsv(
        subject_root / "subject_anchor_decisions.tsv",
        [
            {
                "block_id": item.block_id,
                "candidate_subject_ids": " | ".join(item.candidate_subject_ids),
                "candidate_names": " | ".join(item.candidate_names),
                "deterministic_source": item.deterministic_source,
                "final_subject": item.final_subject,
                "final_status": item.final_status,
                "llm_used": "是" if item.llm_used else "否",
                "llm_status": item.llm_status,
                "llm_confidence": f"{item.llm_confidence:.4f}",
                "llm_evidence_span": item.llm_evidence_span,
                "llm_reason": item.llm_reason,
            }
            for item in anchor_audits
        ],
        [
            "block_id", "candidate_subject_ids", "candidate_names", "deterministic_source", "final_subject",
            "final_status", "llm_used", "llm_status", "llm_confidence", "llm_evidence_span", "llm_reason",
        ],
    )
    _write_tsv(
        process_root / "process_step_decisions.tsv",
        [
            {
                "block_id": item.block_id,
                "process_id": item.process_id,
                "step_id": item.step_id,
                "step_index": "" if item.step_index is None else item.step_index,
                "step_action": item.action,
                "step_object": item.object_text,
                "step_condition_text": item.condition_text,
                "step_result_text": item.result_text,
                "ordering_source": item.ordering_source,
                "status": item.status,
                "reasons": " | ".join(item.reasons),
                "step_text": item.step_text,
                "evidence": item.evidence,
            }
            for item in process_audits
        ],
        [
            "block_id", "process_id", "step_id", "step_index", "step_action", "step_object",
            "step_condition_text", "step_result_text", "ordering_source", "status", "reasons", "step_text", "evidence",
        ],
    )
    report = {
        "ok": True,
        "stage": "closed_subject_anchor_process_step_semantics_v2_phase91",
        "split_blocks": sum(1 for item in split_audits if item.action == "split"),
        "unsplit_ambiguous_blocks": sum(1 for item in split_audits if item.action == "hold_unsplit"),
        "subject_llm_decisions": sum(1 for item in anchor_audits if item.llm_used),
        "subject_llm_confirmed": sum(1 for item in anchor_audits if item.llm_used and item.final_status == "confirmed"),
        "process_steps": len(process_audits),
        "ready_process_steps": sum(1 for item in process_audits if item.status == "ready"),
        "process_llm_steps": sum(1 for item in process_audits if item.ordering_source == "closed_set_process_llm"),
    }
    (subject_root / "subject_anchor_validation_report.json").parent.mkdir(parents=True, exist_ok=True)
    (subject_root / "subject_anchor_validation_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (process_root / "process_semantic_validation_report.json").parent.mkdir(parents=True, exist_ok=True)
    (process_root / "process_semantic_validation_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


__all__ = ["write_semantic_enhancement_outputs"]
