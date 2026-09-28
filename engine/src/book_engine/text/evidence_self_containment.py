from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List

_SENTENCE_BOUNDARY_RE = re.compile(r"[。！？!?；;\n]")
_VISUAL_REFERENCE_RE = re.compile(
    r"(?:参见|见|如|由|从)?\s*(?:图|表|式)\s*[A-Za-z0-9一二三四五六七八九十]+"
    r"(?:\s*[-－—~～]\s*[A-Za-z0-9一二三四五六七八九十]+)?|"
    r"(?:图中|表中|式中|如图所示|见图|由图|从图|由表|从表|由式|从式)",
    re.I,
)
_DEICTIC_LIST_RE = re.compile(
    r"(?:由|包括|包含|含有|分为|列为)?\s*(?:下列|以下|上述|下述|如下|前述)"
    r"(?:若干|各|这些|几个|个)?(?:元件|部分|组分|成分|项目|内容|装置|材料)?|"
    r"由\s*(?:\d+\s*[、,，]\s*)+\d+\s*(?:个)?(?:元件|部分|容器|组件|部件)?|"
    r"由\s*[一二三四五六七八九十百零〇\d]+\s*个(?:主要)?(?:部分|部份|元件|组件|部件)",
    re.I,
)
_FALSE_COMPOSITION_RE = re.compile(
    r"(?:由|从)\s*(?:图|表|式).{0,120}?(?:看出|可见|表明|显示).{0,100}?(?:组成|成分)|"
    r"(?:随|与|对).{0,50}?组成(?:的)?(?:变化|影响|关系)|"
    r"组成(?:的)?(?:变化|影响|关系)",
    re.I | re.S,
)
_GENERIC_COMPONENT_ONLY_RE = re.compile(
    r"^(?:一个|一种|一类|若干|几个|各|这些|上述|下列|以下|主要)?"
    r"(?:部分|部份|元件|组件|部件|组分|成分|项目|内容|材料|物质|容器|系统|装置)$"
)
_DEICTIC_SUBJECT_RE = re.compile(
    r"^(?:它|其|该|此|上述|前述|本|这些|那些)(?:系统|装置|材料|物质|产品|产物|聚合物|"
    r"推进剂|火药|炸药|粘合剂|黏合剂|组件|设备|结构|方法|体系|样品|试样)?$"
)
_VISUAL_LEAD_RE = re.compile(
    r"^(?:由|从|见|参见|如)\s*(?:图|表|式)|^(?:图中|表中|式中|如图所示)", re.I
)


@dataclass(frozen=True)
class EvidenceAssessment:
    self_contained: bool
    dependency_type: str = ""
    reasons: List[str] = field(default_factory=list)
    visual_reference: str = ""
    fact_clause: str = ""


def clause_for_span(text: str, start: int, end: int) -> str:
    value = text or ""
    left = 0
    for match in _SENTENCE_BOUNDARY_RE.finditer(value, 0, max(0, start)):
        left = match.end()
    boundary = _SENTENCE_BOUNDARY_RE.search(value, max(end, start))
    right = boundary.start() if boundary else len(value)
    return value[left:right].strip(" \t\r\n，,。；;：:")


def iter_clauses(text: str) -> Iterable[tuple[int, int, str]]:
    value = text or ""
    start = 0
    for match in _SENTENCE_BOUNDARY_RE.finditer(value):
        clause = value[start:match.start()].strip(" \t\r\n，,。；;：:")
        if clause:
            yield start, match.start(), clause
        start = match.end()
    clause = value[start:].strip(" \t\r\n，,。；;：:")
    if clause:
        yield start, len(value), clause


def compact_ocr_numeric_text(value: str) -> str:
    """Compact OCR-spaced digits without joining separate numeric fields."""
    text = value or ""
    for _ in range(6):
        updated = re.sub(r"(?<=\d)\s+(?=\d)", "", text)
        updated = re.sub(r"(?<=\d)\s*([.,])\s*(?=\d)", r"\1", updated)
        updated = re.sub(r"(?<=\d)\s*(~|～|—|–|至|到|±)\s*(?=\d)", r"\1", updated)
        if updated == text:
            break
        text = updated
    return text.strip()


def is_deictic_subject(value: str) -> bool:
    return bool(_DEICTIC_SUBJECT_RE.fullmatch(re.sub(r"\s+", "", value or "")))


def is_false_composition_text(value: str) -> bool:
    return bool(_FALSE_COMPOSITION_RE.search(value or ""))


def _component_core(value: str) -> str:
    text = re.sub(r"^(?:是)?由", "", value or "").strip()
    text = re.sub(r"(?:组成|构成)(?:的.+)?$", "", text).strip(" ，,。；;：:")
    return text


def has_named_components(value: str) -> bool:
    core = _component_core(value)
    if not core or _DEICTIC_LIST_RE.search(core):
        return False
    core = re.sub(r"^(?:一个|一种|一类|若干|几个|主要|基本)", "", core).strip()
    if not core or _GENERIC_COMPONENT_ONLY_RE.fullmatch(core):
        return False
    if re.fullmatch(r"[\d一二三四五六七八九十百零〇、,，.．()（）\-—~～\s]+", core):
        return False
    tokens = [
        token.strip(" 的等及和或、,，()（）")
        for token in re.split(r"[、,，]|(?:以及|及|和|或)", core)
    ]
    for token in tokens:
        token = re.sub(r"^(?:一个|一种|一类|若干|几个|主要|基本)", "", token).strip()
        if not token or _GENERIC_COMPONENT_ONLY_RE.fullmatch(token):
            continue
        if re.search(r"[A-Za-z\u4e00-\u9fff]", token) and len(token) >= 2:
            return True
    return False


def assess_text_fact(record: object) -> EvidenceAssessment:
    evidence = str(getattr(record, "evidence", "") or "")
    value_text = str(getattr(record, "value_text", "") or "")
    subject = str(getattr(record, "subject", "") or "")
    relation_kind = str(getattr(record, "relation_kind", "") or "")
    source_type = str(getattr(record, "source_type", "") or "")
    property_name = str(getattr(record, "property_name", "") or "")
    fact_clause = str(getattr(record, "fact_clause", "") or value_text or evidence)

    reasons: List[str] = []
    dependency_type = ""
    visual_match = _VISUAL_REFERENCE_RE.search(fact_clause) or _VISUAL_REFERENCE_RE.search(evidence)
    visual_reference = visual_match.group(0) if visual_match else ""

    if is_deictic_subject(subject):
        reasons.append("unresolved_deictic_subject")
        dependency_type = "unresolved_reference"

    is_composition = relation_kind == "composition" or property_name in {"组成", "组分", "配方组成", "组成描述"}
    if is_composition:
        if is_false_composition_text(value_text) or is_false_composition_text(fact_clause):
            reasons.append("false_composition_trigger")
            dependency_type = dependency_type or "false_relation_trigger"
        if _DEICTIC_LIST_RE.search(value_text) or _DEICTIC_LIST_RE.search(fact_clause):
            reasons.append("external_visual_or_list_required")
            dependency_type = dependency_type or "missing_visual_or_list"
        if not has_named_components(value_text):
            reasons.append("incomplete_composition_description")
            dependency_type = dependency_type or "missing_named_components"
        if visual_reference and not has_named_components(value_text):
            reasons.append("visual_reference_without_self_contained_arguments")
            dependency_type = dependency_type or "missing_visual_asset"

    # A visual citation by itself is not a failure: an explicit scalar fact can
    # be self-contained even when the sentence says “见图…”.  Narrative facts
    # that begin with a visual pointer and have no independently usable value are
    # held for review when the image asset is absent.
    if visual_reference and source_type == "text_section_narrative":
        if _VISUAL_LEAD_RE.search(fact_clause) and not re.search(r"[-+−]?\d+(?:[.,]\d+)?", value_text):
            reasons.append("visual_dependent_narrative")
            dependency_type = dependency_type or "missing_visual_asset"

    reasons = sorted(set(reasons))
    return EvidenceAssessment(
        self_contained=not reasons,
        dependency_type=dependency_type,
        reasons=reasons,
        visual_reference=visual_reference,
        fact_clause=fact_clause,
    )


__all__ = [
    "EvidenceAssessment", "assess_text_fact", "clause_for_span", "compact_ocr_numeric_text",
    "has_named_components", "is_deictic_subject", "is_false_composition_text", "iter_clauses",
]
