from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Sequence

from .property_ontology_loader import PropertyOntology, PropertyOntologyEntry, normalize_key

_UNIT_TRAILER_RE = re.compile(
    r"\s*(?:/|（|\()\s*(?:%|‰|℃|K|Pa|kPa|MPa|GPa|m/s|km/s|mm/s|g/cm(?:3|³)|kg/m(?:3|³)|"
    r"J/g|kJ/kg|kJ/mol|J/mol|N·s/kg|Ns/kg|nm|μm|um|mm|cm|m|ns|μs|us|ms|s|min|h|d)\s*(?:）|\))?\s*$",
    re.I,
)
_PREFIX_RE = re.compile(r"^\s*(?:第?\d+(?:[\.．]\d+)*[、.．]?|项目|指标|参数|属性|性能|性质)\s*[:：]?\s*")


def clean_property_name(text: object) -> str:
    value = str(text or "").strip()
    value = _PREFIX_RE.sub("", value)

    # Recover labels embedded in a sentence-like field, e.g. "NG的分子式为".
    possessive = re.match(
        r"^.+?的(中文名称|英文名称|中文别称|英文别称|分子式|化学式|分子量|相对分子质量|密度|熔点|沸点|爆速|爆压|爆热|燃速|用途|组成)(?:为|是)?$",
        value,
    )
    if possessive:
        value = possessive.group(1)

    # Remove parenthetical test conditions from a metric label: 燃速(6.86MPa) -> 燃速.
    value = re.sub(
        r"[（(]\s*[0-9.+\-−~～]+\s*(?:Pa|kPa|MPa|GPa|℃|K|%|s|min|h)?\s*[）)]",
        "",
        value,
        flags=re.I,
    )
    value = _UNIT_TRAILER_RE.sub("", value)
    # Remove trailing state/test qualifiers while the raw predicate remains in the audit trail.
    value = re.sub(r"(?<=[\u4e00-\u9fff])[（(][^）)]{1,24}[）)]$", "", value)
    value = re.sub(r"(?:实验值|实测值|计算值|理论值)$", "", value)
    # OCR often leaves a truncated unit fragment after a slash, e.g. "晶体密度 / (g".
    value = re.sub(r"\s*/\s*\(?[A-Za-zμµ°%‰][A-Za-z0-9μµ°%‰·.^()\-]*\s*$", "", value)

    # Interpret hierarchical table labels without confusing canonical slash names.
    if value not in {"腐蚀性/刺激性", "燃烧热/爆热"} and "/" in value:
        parts = [part.strip() for part in value.split("/") if part.strip()]
        parts = [
            part
            for part in parts
            if not re.fullmatch(r"(?:计算值|实测值|理论值|相对误差|相对偏差|偏差|单位)", part)
        ]
        parts = [part for part in parts if not re.fullmatch(r"[A-Za-zμµ°%‰·.^()0-9\-]+", part)]
        if parts:
            if re.fullmatch(r"(?:配方组成|组成|性能|性质|指标|参数)", parts[0]) and len(parts) > 1:
                value = parts[-1]
            else:
                value = parts[0]

    value = re.sub(r"(?:性能|性质)\s*[/>＞]\s*", "", value)
    value = re.sub(r"\s+", "", value)
    # Remove a trailing formula symbol used only as a column variable: 密度p、峰顶温度T、爆热Q.
    value = re.sub(r"(?<=[\u4e00-\u9fff）)])(?:△?[A-Za-z](?:e|m|v)?)$", "", value)
    return value.strip("：:;；,，/（(")


@dataclass(frozen=True)
class CandidateMatch:
    entry: PropertyOntologyEntry
    match_type: str
    lexical_score: float
    matched_text: str


class PropertyCandidateGenerator:
    def __init__(self, ontology: PropertyOntology, *, max_candidates: int = 12):
        self.ontology = ontology
        self.max_candidates = max_candidates

    def generate(self, raw_property: object) -> List[CandidateMatch]:
        cleaned = clean_property_name(raw_property)
        key = normalize_key(cleaned)
        if not key:
            return []

        candidates: List[CandidateMatch] = []
        seen = set()

        def add(entries: Sequence[PropertyOntologyEntry], match_type: str, lexical_score: float, matched: str) -> None:
            for entry in entries:
                marker = entry.property_id
                if marker in seen:
                    continue
                seen.add(marker)
                candidates.append(CandidateMatch(entry, match_type, lexical_score, matched))

        add(self.ontology.by_alias.get(key, ()), "exact_alias", 1.0, cleaned)
        add(self.ontology.by_canonical.get(key, ()), "exact_canonical", 0.88, cleaned)

        if len(candidates) < self.max_candidates:
            # Conservative fuzzy matching. Require at least two Chinese/Latin characters and a high overlap.
            for alias_key, entries in self.ontology.by_alias.items():
                if len(alias_key) < 2:
                    continue
                if alias_key in key or key in alias_key:
                    overlap = min(len(alias_key), len(key)) / max(len(alias_key), len(key))
                    minimum = 0.40 if len(alias_key) >= 4 else 0.55
                    if overlap < minimum:
                        continue
                    add(entries, "substring", 0.62 + 0.20 * overlap, alias_key)
                    if len(candidates) >= self.max_candidates:
                        break

        return sorted(
            candidates,
            key=lambda item: (-item.lexical_score, -item.entry.priority, item.entry.property_id),
        )[: self.max_candidates]


__all__ = ["CandidateMatch", "PropertyCandidateGenerator", "clean_property_name"]
