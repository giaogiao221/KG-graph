from __future__ import annotations

import re
from dataclasses import replace
from typing import Dict, List, Mapping, Sequence, Tuple

from book_engine.document.block_segmenter import TextBlock
from book_engine.routing.heading_subject_resolver import is_entity_surface
from book_engine.routing.subject_name_normalizer import normalize_subject_name
from book_engine.routing.subject_type_inferer import infer_subject_type
from book_engine.text.evidence_self_containment import is_deictic_subject
from book_engine.text.text_fact_extractor import TextFactRecord

_ENTITY_TAIL = r"(?:粘合剂|黏合剂|聚合物|预聚物|共聚物|均聚物|推进剂|火药|炸药|药剂|材料|化合物|氧化物|树脂|橡胶|体系)"
_STRUCTURE_OWNER_RE = re.compile(
    rf"(?P<owner>[A-Za-z0-9+._/\-（）()\u4e00-\u9fff]{{2,48}}?{_ENTITY_TAIL}|"
    r"[A-Za-z0-9+._/\-（）()\u4e00-\u9fff]{2,36})\s*的?\s*"
    r"(?:分子结构(?:式)?|结构式|化学结构|分子式)\s*(?:为|如下|是|[:：])",
    re.I,
)
_NAMED_OWNER_RE = re.compile(
    rf"(?P<owner>[A-Za-z0-9+._/\-（）()\u4e00-\u9fff]{{2,48}}?{_ENTITY_TAIL})"
)
_BAD_OWNER_RE = re.compile(r"(?:本章|该章|如下|上述|其中|性质|性能|结构|组成|方法|结果|研究)$")


def _clean_owner(value: str) -> str:
    owner = normalize_subject_name(value or "").canonical_name.strip(" ，,。；;：:")
    owner = re.sub(r"^(?:其|该|上述|这种|此种)", "", owner).strip()
    owner = re.sub(r"^(?:典型的|一种|某种)", "", owner).strip()
    owner = re.sub(r"的?分子$", "", owner).strip()
    return owner


def _owner_from_text(text: str) -> str:
    value = re.sub(r"\s+", " ", text or "").strip()
    for pattern in (_STRUCTURE_OWNER_RE, _NAMED_OWNER_RE):
        matches = list(pattern.finditer(value))
        for match in reversed(matches):
            owner = _clean_owner(match.group("owner"))
            if not owner or len(owner) > 48 or _BAD_OWNER_RE.search(owner):
                continue
            if is_deictic_subject(owner):
                continue
            if is_entity_surface(owner) or re.search(_ENTITY_TAIL + r"$", owner, re.I):
                return owner
    return ""


def resolve_deictic_text_subjects(
    records: Sequence[TextFactRecord],
    blocks: Sequence[TextBlock],
    *,
    max_line_gap: int = 12,
    max_previous_blocks: int = 4,
) -> Tuple[List[TextFactRecord], List[Dict[str, object]]]:
    """Resolve local demonstrative subjects from the nearest explicit antecedent.

    The resolver is deliberately bounded to the same heading path and a short
    line window.  It never falls back to a book-level subject.  Ambiguous or
    missing antecedents remain deictic and are later held by the text gate.
    """
    block_map: Mapping[str, TextBlock] = {block.block_id: block for block in blocks}
    ordered = sorted(blocks, key=lambda b: (b.line_start, b.line_end, b.block_id))
    position = {block.block_id: index for index, block in enumerate(ordered)}
    resolved: List[TextFactRecord] = []
    audits: List[Dict[str, object]] = []

    for record in records:
        if not is_deictic_subject(record.subject):
            resolved.append(record)
            continue
        block = block_map.get(record.block_id)
        if block is None:
            resolved.append(record)
            continue
        idx = position.get(block.block_id, -1)
        candidates: List[Tuple[int, str, str]] = []
        for previous in reversed(ordered[max(0, idx - max_previous_blocks):idx]):
            gap = block.line_start - previous.line_end
            if gap < 0 or gap > max_line_gap:
                continue
            if list(previous.heading_path) != list(block.heading_path):
                continue
            owner = _owner_from_text(previous.text)
            if owner:
                candidates.append((gap, owner, previous.text))
        unique: List[Tuple[int, str, str]] = []
        seen = set()
        for item in sorted(candidates, key=lambda x: x[0]):
            key = normalize_subject_name(item[1]).normalized_key
            if key and key not in seen:
                unique.append(item)
                seen.add(key)
        if not unique:
            resolved.append(record)
            audits.append({
                "record_id": record.record_id,
                "block_id": record.block_id,
                "original_subject": record.subject,
                "resolved_subject": "",
                "decision": "hold",
                "reason": "no_unique_local_antecedent",
            })
            continue
        # The nearest explicit owner wins.  A second different candidate at the
        # same distance is treated as ambiguous rather than guessed.
        nearest_gap = unique[0][0]
        nearest = [item for item in unique if item[0] == nearest_gap]
        if len(nearest) != 1:
            resolved.append(record)
            audits.append({
                "record_id": record.record_id,
                "block_id": record.block_id,
                "original_subject": record.subject,
                "resolved_subject": "",
                "decision": "hold",
                "reason": "multiple_equal_distance_antecedents",
            })
            continue
        owner = nearest[0][1]
        subject_type, _, type_reasons = infer_subject_type(owner)
        updated = replace(
            record,
            subject=owner,
            subject_type=subject_type,
            owner_source="local_deictic_antecedent_phase99",
            owner_evidence=nearest[0][2],
            anchor_source="local_deictic_antecedent_phase99",
            confidence=max(record.confidence, 0.88),
            unresolved_reasons=[r for r in record.unresolved_reasons if r != "unresolved_deictic_subject"] + list(type_reasons),
        )
        resolved.append(updated)
        audits.append({
            "record_id": record.record_id,
            "block_id": record.block_id,
            "original_subject": record.subject,
            "resolved_subject": owner,
            "decision": "resolve",
            "reason": "nearest_explicit_owner_same_heading",
        })
    return resolved, audits


__all__ = ["resolve_deictic_text_subjects"]
