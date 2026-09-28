from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Mapping, Sequence

from book_engine.document.block_segmenter import TextBlock
from book_engine.gates.text_fact_gate import TextGateDecision
from book_engine.routing.text_subject_anchor_resolver import TextSubjectAnchor
from book_engine.text.text_fact_extractor import TextFactRecord


def _write_tsv(path: Path, rows, columns) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_text_outputs(
    output_dir: Path,
    blocks: Sequence[TextBlock],
    anchors: Sequence[TextSubjectAnchor],
    records: Sequence[TextFactRecord],
    decisions: Sequence[TextGateDecision],
) -> dict:
    root = output_dir / "step_text_semantics"
    root.mkdir(parents=True, exist_ok=True)

    _write_tsv(
        root / "text_blocks.tsv",
        [
            {
                "block_id": b.block_id,
                "line_start": b.line_start,
                "line_end": b.line_end,
                "heading_path": " > ".join(b.heading_path),
                "heading_title": b.heading_title,
                "heading_level": b.heading_level,
                "role": b.role,
                "role_confidence": f"{b.role_confidence:.4f}",
                "role_reasons": ";".join(b.role_reasons),
                "text": b.text,
            }
            for b in blocks
        ],
        ["block_id", "line_start", "line_end", "heading_path", "heading_title", "heading_level", "role", "role_confidence", "role_reasons", "text"],
    )
    _write_tsv(
        root / "text_subject_anchors.tsv",
        [
            {
                "block_id": a.block_id,
                "subject": a.subject,
                "subject_type": a.subject_type,
                "source": a.source,
                "confidence": f"{a.confidence:.4f}",
                "status": a.status,
                "reasons": ";".join(a.reasons),
            }
            for a in anchors
        ],
        ["block_id", "subject", "subject_type", "source", "confidence", "status", "reasons"],
    )
    _write_tsv(
        root / "text_fact_candidates.tsv",
        [
            {
                "record_id": r.record_id,
                "block_id": r.block_id,
                "subject": r.subject,
                "subject_type": r.subject_type,
                "property_name": r.property_name,
                "value_text": r.value_text,
                "unit": r.unit,
                "value_num": "" if r.value_num is None else r.value_num,
                "lower_bound": "" if r.lower_bound is None else r.lower_bound,
                "upper_bound": "" if r.upper_bound is None else r.upper_bound,
                "source_type": r.source_type,
                "record_status": r.record_status,
                "confidence": f"{r.confidence:.4f}",
                "heading_path": " > ".join(r.heading_path),
                "line_start": r.line_start,
                "line_end": r.line_end,
                "unresolved_reasons": ";".join(r.unresolved_reasons),
                "fact_clause": r.fact_clause,
                "owner_evidence": r.owner_evidence,
                "owner_source": r.owner_source,
                "evidence": r.evidence,
            }
            for r in records
        ],
        ["record_id", "block_id", "subject", "subject_type", "property_name", "value_text", "unit", "value_num", "lower_bound", "upper_bound", "source_type", "record_status", "confidence", "heading_path", "line_start", "line_end", "unresolved_reasons", "fact_clause", "owner_evidence", "owner_source", "evidence"],
    )
    decision_by_id = {d.record_id: d for d in decisions}
    _write_tsv(
        root / "text_fact_gate.tsv",
        [
            {
                "record_id": d.record_id,
                "accepted": "是" if d.accepted else "否",
                "action": d.action,
                "canonical_subject": d.canonical_subject,
                "subject_type": d.subject_type,
                "confidence": f"{d.confidence:.4f}",
                "evidence_self_contained": "是" if d.evidence_self_contained else "否",
                "dependency_type": d.dependency_type,
                "visual_reference": d.visual_reference,
                "fact_clause": d.fact_clause,
                "reasons": ";".join(d.reasons),
            }
            for d in decisions
        ],
        ["record_id", "accepted", "action", "canonical_subject", "subject_type", "confidence", "evidence_self_contained", "dependency_type", "visual_reference", "fact_clause", "reasons"],
    )
    unresolved = [r for r in records if not decision_by_id.get(r.record_id) or not decision_by_id[r.record_id].accepted]
    _write_tsv(
        root / "text_unresolved.tsv",
        [
            {
                "record_id": r.record_id,
                "subject": r.subject,
                "property_name": r.property_name,
                "value_text": r.value_text,
                "record_status": r.record_status,
                "reasons": ";".join((decision_by_id.get(r.record_id).reasons if decision_by_id.get(r.record_id) else r.unresolved_reasons)),
                "evidence": r.evidence,
            }
            for r in unresolved
        ],
        ["record_id", "subject", "property_name", "value_text", "record_status", "reasons", "evidence"],
    )

    record_by_id = {r.record_id: r for r in records}
    visual_root = output_dir / "step_visual_dependency"
    visual_rows = []
    for decision in decisions:
        record = record_by_id.get(decision.record_id)
        if record is None:
            continue
        dependency_reasons = {
            "external_visual_or_list_required", "visual_reference_without_self_contained_arguments",
            "visual_dependent_narrative", "incomplete_composition_description",
            "false_composition_trigger", "unresolved_deictic_subject",
        }
        if not decision.visual_reference and not dependency_reasons.intersection(decision.reasons):
            continue
        visual_rows.append({
            "record_id": record.record_id,
            "subject": record.subject,
            "property_name": record.property_name,
            "value_text": record.value_text,
            "source_type": record.source_type,
            "visual_reference": decision.visual_reference,
            "dependency_type": decision.dependency_type,
            "evidence_self_contained": "是" if decision.evidence_self_contained else "否",
            "decision": "release" if decision.accepted else "hold",
            "reasons": ";".join(decision.reasons),
            "fact_clause": decision.fact_clause,
            "evidence": record.evidence,
        })
    _write_tsv(
        visual_root / "visual_dependency_audit.tsv",
        visual_rows,
        ["record_id", "subject", "property_name", "value_text", "source_type", "visual_reference", "dependency_type", "evidence_self_contained", "decision", "reasons", "fact_clause", "evidence"],
    )

    binding_root = output_dir / "step_owner_value_binding"
    binding_rows = []
    for record in records:
        if record.source_type != "text_clause_bound_numeric_property":
            continue
        decision = decision_by_id.get(record.record_id)
        binding_rows.append({
            "record_id": record.record_id,
            "heading_anchor": (next((a.subject for a in anchors if a.block_id == record.block_id), "")),
            "selected_owner": record.subject,
            "owner_source": record.owner_source,
            "property_name": record.property_name,
            "selected_value": record.value_text,
            "unit": record.unit,
            "conditions": "；".join(f"{c.normalized_name or c.name}={c.value_text}" for c in record.conditions),
            "fact_clause": record.fact_clause,
            "decision": "release" if decision and decision.accepted else "hold",
            "reasons": ";".join(decision.reasons if decision else record.unresolved_reasons),
            "evidence": record.evidence,
        })
    _write_tsv(
        binding_root / "text_owner_value_binding_audit.tsv",
        binding_rows,
        ["record_id", "heading_anchor", "selected_owner", "owner_source", "property_name", "selected_value", "unit", "conditions", "fact_clause", "decision", "reasons", "evidence"],
    )

    report = {
        "ok": True,
        "stage": "text_owner_value_binding_and_evidence_gate_v2_phase97",
        "blocks": len(blocks),
        "confirmed_anchors": sum(1 for a in anchors if a.status == "confirmed"),
        "candidate_anchors": sum(1 for a in anchors if a.status == "candidate"),
        "unresolved_anchors": sum(1 for a in anchors if a.status == "unresolved"),
        "fact_candidates": len(records),
        "accepted_text_facts": sum(1 for d in decisions if d.accepted),
        "held_text_facts": sum(1 for d in decisions if not d.accepted),
        "visual_reference_records": sum(1 for d in decisions if d.visual_reference),
        "visual_or_list_dependency_held": sum(1 for d in decisions if not d.accepted and d.dependency_type in {"missing_visual_or_list", "missing_visual_asset", "missing_named_components"}),
        "evidence_not_self_contained": sum(1 for d in decisions if not d.evidence_self_contained),
        "visual_dependency_audit": str(visual_root / "visual_dependency_audit.tsv"),
        "owner_value_binding_records": len(binding_rows),
        "owner_value_binding_audit": str(binding_root / "text_owner_value_binding_audit.tsv"),
    }
    (root / "text_semantic_validation_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


__all__ = ["write_text_outputs"]
