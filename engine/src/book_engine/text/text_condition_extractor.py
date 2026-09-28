from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple

from book_engine.core.schemas import ConditionAtom
from book_engine.document.block_segmenter import TextBlock
from book_engine.tables.condition_rules import CONDITION_RULES, METHOD_TOKENS, RESULT_ONLY_TERMS
from book_engine.tables.value_parser import parse_value
from book_engine.text.text_fact_extractor import TextFactRecord

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。；;！？!?])|\n+")
_VALUE_FRAGMENT = (
    r"(?:≥|≤|>|<|约|不小于|不大于)?\s*"
    r"[-+−]?\d+(?:[.,]\d+)?"
    r"(?:\s*(?:~|～|—|–|至|到|±|:)\s*[-+−]?\d+(?:[.,]\d+)?)?\s*"
    r"(?:%|‰|℃|°C|K|Pa|kPa|MPa|GPa|g/cm(?:3|³)|kg/m(?:3|³)|nm|μm|um|mm|cm|m|"
    r"ns|μs|us|ms|s|min|h|d|Hz|rpm|K/min|℃/min|mm/s|cm/s|m/s|km/s|J/g|kJ/kg|kJ/mol)?"
)
_PRESSURE_VALUE_FRAGMENT = (
    r"(?:≥|≤|>|<|约|不小于|不大于)?\s*"
    r"[-+−]?\d+(?:[.,]\d+)?"
    r"(?:\s*(?:~|～|—|–|至|到|±)\s*[-+−]?\d+(?:[.,]\d+)?)?\s*"
    r"(?:Pa|kPa|MPa|GPa)"
)


@dataclass
class TextConditionAudit:
    record_id: str
    block_id: str
    condition_name: str
    condition_value: str
    unit: str
    source_text: str
    action: str
    reason: str
    confidence: float


def _sentences(text: str) -> List[str]:
    return [item.strip() for item in _SENTENCE_SPLIT_RE.split(text or "") if item.strip()]


def _target_sentence(record: TextFactRecord, block: TextBlock) -> str:
    candidates = _sentences(block.text)
    compact_value = re.sub(r"\s+", "", record.value_text or "")
    for sentence in candidates:
        compact_sentence = re.sub(r"\s+", "", sentence)
        if compact_value and compact_value in compact_sentence:
            return sentence
    for sentence in candidates:
        if record.property_name and record.property_name in sentence:
            return sentence
    return block.text


def _condition_id(record: TextFactRecord, name: str, value_text: str) -> str:
    digest = hashlib.sha1(
        f"{record.record_id}|{name}|{value_text}".encode("utf-8")
    ).hexdigest()[:18]
    return f"txtcond:{digest}"


def _extract_named_conditions(record: TextFactRecord, sentence: str) -> Iterable[ConditionAtom]:
    seen = set()
    for rule in CONDITION_RULES:
        for alias in sorted(rule.aliases, key=len, reverse=True):
            if alias in RESULT_ONLY_TERMS:
                continue
            if alias == record.property_name or rule.canonical_name == record.property_name:
                continue
            patterns = (
                rf"(?:在|于|当|经|按|以)?\s*{re.escape(alias)}\s*(?:为|是|=|：|:|达到|保持在|控制在)?\s*(?P<value>{_VALUE_FRAGMENT})\s*(?:条件下|下|时)?",
                rf"(?:在|于|当)?\s*(?P<value>{_VALUE_FRAGMENT})\s*{re.escape(alias)}\s*(?:条件下|下|时)",
            )
            for pattern in patterns:
                for match in re.finditer(pattern, sentence, re.I):
                    value_text = match.group("value").strip()
                    if not value_text or re.sub(r"\s+", "", value_text) == re.sub(r"\s+", "", record.value_text or ""):
                        continue
                    parsed = parse_value(value_text)
                    key = (rule.canonical_name, parsed.normalized_text or value_text)
                    if key in seen:
                        continue
                    seen.add(key)
                    confidence = 0.88 if alias != "温度" else 0.80
                    yield ConditionAtom(
                        name=alias,
                        normalized_name=rule.canonical_name,
                        condition_type=rule.condition_type,
                        value_text=value_text,
                        unit=parsed.unit,
                        value_num=parsed.value_num,
                        lower_bound=parsed.lower_bound,
                        upper_bound=parsed.upper_bound,
                        comparator=parsed.comparator,
                        scope="text_sentence",
                        priority=420,
                        confidence=confidence,
                        condition_id=_condition_id(record, rule.canonical_name, value_text),
                        source_kind="text_sentence_condition",
                        source_text=sentence,
                        binding_target=record.record_id,
                        reasons=["explicit_named_condition_in_same_sentence"],
                    )


def _extract_unit_driven_pressure(record: TextFactRecord, sentence: str) -> Iterable[ConditionAtom]:
    if record.property_name not in {"燃速", "爆速", "比冲", "力学性能", "拉伸强度", "应变"}:
        return []
    pattern = re.compile(
        rf"(?:在|于|当)\s*(?P<value>{_PRESSURE_VALUE_FRAGMENT})\s*(?:条件下|下|时)",
        re.I,
    )
    results: List[ConditionAtom] = []
    for match in pattern.finditer(sentence):
        value_text = match.group("value").strip()
        if not re.search(r"(?:Pa|kPa|MPa|GPa)\b", value_text, re.I):
            continue
        parsed = parse_value(value_text)
        results.append(
            ConditionAtom(
                name="压力",
                normalized_name="压力",
                condition_type="environment",
                value_text=value_text,
                unit=parsed.unit,
                value_num=parsed.value_num,
                lower_bound=parsed.lower_bound,
                upper_bound=parsed.upper_bound,
                comparator=parsed.comparator,
                scope="text_sentence",
                priority=400,
                confidence=0.78,
                condition_id=_condition_id(record, "压力", value_text),
                source_kind="text_unit_driven_condition",
                source_text=sentence,
                binding_target=record.record_id,
                reasons=["pressure_unit_with_condition_cue"],
            )
        )
    return results


def _method_from_text(text: str) -> str:
    hits = [token for token in METHOD_TOKENS if token.lower() in (text or "").lower()]
    return "；".join(dict.fromkeys(hits))


def attach_text_conditions(
    records: Sequence[TextFactRecord],
    blocks: Sequence[TextBlock],
) -> Tuple[List[TextFactRecord], List[TextConditionAudit]]:
    block_by_id: Mapping[str, TextBlock] = {block.block_id: block for block in blocks}
    audits: List[TextConditionAudit] = []

    for record in records:
        block = block_by_id.get(record.block_id)
        if block is None:
            continue
        sentence = _target_sentence(record, block)
        atoms = list(_extract_named_conditions(record, sentence))
        atoms.extend(_extract_unit_driven_pressure(record, sentence))

        unique: Dict[tuple, ConditionAtom] = {}
        for atom in list(record.conditions) + atoms:
            numeric_signature = (
                atom.value_num,
                atom.lower_bound,
                atom.upper_bound,
                (atom.unit or "").casefold(),
            )
            key = (
                atom.normalized_name or atom.name,
                numeric_signature if any(item is not None and item != "" for item in numeric_signature[:3])
                else re.sub(r"\s+", "", atom.value_text or "").casefold(),
            )
            old = unique.get(key)
            if old is None or atom.confidence > old.confidence:
                unique[key] = atom
        record.conditions = sorted(
            unique.values(),
            key=lambda item: (-(item.priority or 0), item.normalized_name or item.name, item.value_text),
        )
        if not record.method:
            record.method = _method_from_text(sentence)
        for atom in atoms:
            audits.append(
                TextConditionAudit(
                    record_id=record.record_id,
                    block_id=record.block_id,
                    condition_name=atom.normalized_name or atom.name,
                    condition_value=atom.value_text,
                    unit=atom.unit,
                    source_text=atom.source_text,
                    action="bound",
                    reason="same_sentence_explicit_condition",
                    confidence=atom.confidence,
                )
            )
    return list(records), audits


__all__ = ["TextConditionAudit", "attach_text_conditions"]
