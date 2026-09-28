from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from book_engine.gates.table_record_gate import RecordGateDecision
from book_engine.gates.text_fact_gate import TextGateDecision
from book_engine.routing.subject_name_normalizer import normalize_subject_name
from book_engine.routing.subject_registry_builder import SubjectRegistryEntry
from book_engine.routing.subject_type_inferer import infer_subject_type

_TYPE_PRIORITY = {
    "火工品器件": 100,
    "配方/材料体系": 95,
    "配方体系": 95,
    "高聚物": 90,
    "材料类别": 80,
    "样品": 70,
    "材料": 50,
    "未知类型": 0,
    "": 0,
}
_CANONICAL_TYPE = {
    "配方体系": "配方/材料体系",
    "材料体系": "配方/材料体系",
    "高分子材料": "高聚物",
}


@dataclass
class SubjectTypeAudit:
    source: str
    record_id: str
    canonical_subject: str
    old_type: str
    new_type: str
    changed: bool
    reason: str


def _canonical_type(value: str) -> str:
    return _CANONICAL_TYPE.get(value or "", value or "")


def _type_priority(value: str) -> int:
    if value in _TYPE_PRIORITY:
        return _TYPE_PRIORITY[value]
    # Preserve source-defined domain types such as 单质炸药、起爆药、危险化学品.
    if value and value not in {"材料", "未知类型"} and any(
        token in value for token in ("炸药", "推进剂", "发射药", "火药", "药剂", "聚合物", "化学品", "单体", "预聚物", "弹性体")
    ):
        return 88
    if value and value.endswith("材料") and value != "材料":
        return 82
    return 20


def _best_type(subject: str, candidates: Sequence[str]) -> str:
    inferred, _, _ = infer_subject_type(subject)
    values = [_canonical_type(item) for item in candidates if item]
    values.append(_canonical_type(inferred))
    return max(values, key=_type_priority) if values else "材料"


def harmonize_subject_types(
    accepted_table: Sequence[Tuple[object, RecordGateDecision, object]],
    accepted_text: Sequence[Tuple[object, TextGateDecision]],
    registry_entries: Sequence[SubjectRegistryEntry],
) -> Tuple[List[Tuple[object, RecordGateDecision, object]], List[Tuple[object, TextGateDecision]], List[SubjectTypeAudit]]:
    votes: Dict[str, List[str]] = {}
    for entry in registry_entries:
        key = normalize_subject_name(entry.canonical_name).normalized_key
        votes.setdefault(key, []).append(entry.subject_type)
    for _, decision, _ in accepted_table:
        key = normalize_subject_name(decision.canonical_subject).normalized_key
        votes.setdefault(key, []).append(decision.subject_type)
    for _, decision in accepted_text:
        key = normalize_subject_name(decision.canonical_subject).normalized_key
        votes.setdefault(key, []).append(decision.subject_type)

    best_by_key = {
        key: _best_type(subject="", candidates=types)
        for key, types in votes.items()
    }
    # Re-run inference with the actual subject to avoid losing subject-specific evidence.
    for _, decision, _ in accepted_table:
        key = normalize_subject_name(decision.canonical_subject).normalized_key
        best_by_key[key] = _best_type(decision.canonical_subject, votes.get(key, []))
    for _, decision in accepted_text:
        key = normalize_subject_name(decision.canonical_subject).normalized_key
        best_by_key[key] = _best_type(decision.canonical_subject, votes.get(key, []))

    audits: List[SubjectTypeAudit] = []
    table_result: List[Tuple[object, RecordGateDecision, object]] = []
    for record, decision, plan in accepted_table:
        key = normalize_subject_name(decision.canonical_subject).normalized_key
        old = decision.subject_type
        new = best_by_key.get(key, _canonical_type(old) or "材料")
        decision.subject_type = new
        audits.append(SubjectTypeAudit(
            source="table",
            record_id=decision.record_id,
            canonical_subject=decision.canonical_subject,
            old_type=old,
            new_type=new,
            changed=old != new,
            reason="document_level_specific_type_vote" if old != new else "type_already_consistent",
        ))
        table_result.append((record, decision, plan))

    text_result: List[Tuple[object, TextGateDecision]] = []
    for record, decision in accepted_text:
        key = normalize_subject_name(decision.canonical_subject).normalized_key
        old = decision.subject_type
        new = best_by_key.get(key, _canonical_type(old) or "材料")
        decision.subject_type = new
        audits.append(SubjectTypeAudit(
            source="text",
            record_id=decision.record_id,
            canonical_subject=decision.canonical_subject,
            old_type=old,
            new_type=new,
            changed=old != new,
            reason="document_level_specific_type_vote" if old != new else "type_already_consistent",
        ))
        text_result.append((record, decision))

    return table_result, text_result, audits


__all__ = ["SubjectTypeAudit", "harmonize_subject_types"]
