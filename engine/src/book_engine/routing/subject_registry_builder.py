from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple

from book_engine.core.schemas import ConditionalFactRecord, TableBlock
from book_engine.routing.subject_name_normalizer import normalize_subject_name
from book_engine.routing.heading_subject_resolver import classify_heading_subject, is_topic_only_title
from book_engine.routing.subject_type_inferer import infer_subject_type

_GENERIC_SUBJECTS = {
    "材料", "含能材料", "炸药", "推进剂", "固体推进剂", "高能推进剂", "火药", "烟火药", "性能",
    "试验", "实验", "结果", "参数", "条件", "样品", "试样", "配方", "体系", "组分", "项目",
    "不同材料", "不同样品", "不同配方", "几种材料", "几种炸药", "几种推进剂", "备注", "方法", "原材料", "用途", "作用", "结构", "位置",
    "分子几何构型", "几何构型", "计算值", "实测值", "理论值", "性能参数", "工艺参数", "实验结果",
}
_PROPERTY_TERMS = (
    "密度", "熔点", "沸点", "爆速", "爆压", "爆热", "燃速", "比冲", "感度", "释能时间", "释能功率",
    "峰顶温度", "分解温度", "生成热", "生成焓", "氧平衡", "粒径", "粒度", "黏度", "粘度", "相容性",
    "安定性", "试验结果", "测试结果", "实验结果", "性能", "上限", "下限", "平均值", "标准差",
    "几何构型", "分子构型", "计算值", "实测值", "理论值",
)
_UNIT_TOKENS = ("℃", "°C", "MPa", "GPa", "kPa", "g/cm", "kg/m", "mm/s", "m/s", "km/s", "J/g", "kJ", "%", "‰")
_IDENTIFIER_ONLY_RE = re.compile(
    r"^(?:[A-Za-z]|[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ]|\d+(?:[.．-]\d+)?|[一二三四五六七八九十百]+)(?:号|[#@*※①②③④⑤⑥⑦⑧⑨⑩])?$",
    re.IGNORECASE,
)

_MATERIAL_HINTS = (
    "酸", "盐", "酯", "醚", "胺", "酚", "烷", "烯", "醇", "酮", "硝", "叠氮", "高氯酸", "硝酸",
    "聚", "橡胶", "树脂", "纤维素", "铝", "银", "铅", "铜", "铁", "镍", "钴", "HMX", "RDX", "TNT",
    "PETN", "CL-20", "NTO", "ADN", "AP", "AN", "GAP", "HTPB", "BAMO", "NIMMO", "FOX-7",
)

# This source is emitted only by the phase104 count-bound rowspan recovery in
# ``tables.record_compiler``.  Registry promotion remains closed-set as a
# second line of defence: an arbitrary Latin token cannot become a confirmed
# entity merely by carrying the source label.
_COLLAPSED_SERIES_SOURCE = "collapsed_material_code_sequence_recovery_phase104"
_COLLAPSED_SERIES_MATERIAL_CODES = {
    "CL-20(HNIW)", "HMX(Β)", "HMX(Α)", "PBXN-109", "PBX-9501", "PBX-9502",
    "CL-20", "FOX-7", "TNAZ", "PETN", "TATB", "RDX", "HMX", "TNT", "NTO",
    "ADN", "ONC", "NIGU", "GAP", "HTPB", "BAMO", "AMMO", "NIMMO", "NEPE",
    "CMDB", "AP", "AN", "NG", "NC", "AL",
}


@dataclass
class SubjectEvidence:
    record_id: str
    table_id: str
    raw_name: str
    source: str
    heading: str
    preceding_text: str
    following_text: str
    record_status: str
    confidence: float


@dataclass
class SubjectRegistryEntry:
    subject_id: str
    canonical_name: str
    normalized_key: str
    subject_type: str
    status: str
    score: float
    type_confidence: float
    aliases: List[str] = field(default_factory=list)
    evidence_count: int = 0
    table_count: int = 0
    source_counts: Dict[str, int] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    negative_reasons: List[str] = field(default_factory=list)


@dataclass
class SubjectResolution:
    record_id: str
    table_id: str
    raw_subject: str
    canonical_name: str
    normalized_key: str
    subject_id: str
    subject_type: str
    registry_status: str
    action: str
    score: float
    reasons: List[str] = field(default_factory=list)


def _stable_subject_id(key: str) -> str:
    return "subj:" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _looks_identifier_only(value: str) -> bool:
    surface = value or ""
    surface = re.sub(r"\s+", "", surface).strip(" ,，;；:：|[]()（）")
    surface = re.sub(r"^(?:编号|序号|批次|样品|试样|配方|聚合物|no\.?|id|#)", "", surface, flags=re.IGNORECASE)
    return bool(surface and _IDENTIFIER_ONLY_RE.fullmatch(surface))


def _looks_property_like(value: str) -> bool:
    if not value:
        return False
    if any(token in value for token in _UNIT_TOKENS):
        return True
    if any(value == term or value.startswith(term + "/") or value.endswith(term) for term in _PROPERTY_TERMS):
        return True
    return False


def _looks_concatenated(value: str) -> bool:
    if len(value) > 90:
        return True
    known_abbreviations = ("POLYNIMMO", "NIMMO", "PGN", "GAP", "BAMO", "AMMO", "HTPB", "CL20", "RDX", "HMX", "PETN")
    upper_compact = re.sub(r"[^A-Z0-9]", "", value.upper())
    abbreviation_hits = [token for token in known_abbreviations if token in upper_compact]
    if len(set(abbreviation_hits)) >= 2 and not re.search(r"[/+,&，、\s-]", value):
        return True
    if len(value) > 55 and value.count("-") >= 4:
        return True
    abbreviation_hits = len(re.findall(r"\b(?:HMX|RDX|TNT|PETN|CL-20|NTO|ADN|AP|AN|NG|NC)\b", value, re.I))
    if len(value) > 45 and abbreviation_hits >= 4:
        return True
    chinese_material_hits = sum(value.count(token) for token in ("硝酸", "硝基", "三硝", "四硝", "高氯酸"))
    return len(value) > 55 and chinese_material_hits >= 4


def _material_hint(value: str) -> bool:
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9+._/()\-]{1,40}", value):
        return True
    # Latin abbreviations embedded in a Chinese material name (PECH二醇,
    # BAMO基弹性体) are strong material hints without requiring a book-specific
    # abbreviation whitelist.
    if re.search(r"[A-Z]{2,}", value):
        return True
    return any(token in value for token in _MATERIAL_HINTS)


def _score_group(name: str, evidences: Sequence[SubjectEvidence]) -> Tuple[float, List[str], List[str], bool]:
    score = 0.18
    reasons: List[str] = []
    negatives: List[str] = []
    hard_reject = False
    source_counts = Counter(item.source for item in evidences)
    table_count = len({item.table_id for item in evidences})

    if source_counts.get("column_header"):
        score += 0.34
        reasons.append("explicit_column_header")
    if source_counts.get("table_entity_axis"):
        score += 0.28
        reasons.append("explicit_table_entity_axis")
    if source_counts.get("axis_inversion_repair"):
        score += 0.30
        reasons.append("deterministic_axis_inversion_repair")
    if source_counts.get("multi_subject_split"):
        score += 0.26
        reasons.append("explicit_multi_subject_enumeration")
    if any(item.source.startswith(("context:", "context_identifier_rebind:", "context_sample_code_rebind:", "context_sample_variant_rebind:")) for item in evidences):
        score += 0.12
        reasons.append("context_subject")
    # A numeric sample/batch key rebound to a specific caption or heading
    # subject is stronger than generic context inheritance.  This boost is
    # deliberately limited to the deterministic identifier-repair path.
    if any(item.source.startswith("context_identifier_rebind:") for item in evidences):
        score += 0.35
        reasons.append("explicit_identifier_context_rebind")
    if any(item.source.startswith(("context_sample_code_rebind:", "context_sample_variant_rebind:")) for item in evidences):
        score += 0.35
        reasons.append("explicit_sample_code_context_rebind")
    if any(item.source == "row_header" for item in evidences):
        score += 0.34
        reasons.append("explicit_row_header")
    if any(item.source == "row_level_formulation_entity_phase99" for item in evidences):
        score += 0.62
        reasons.append("deterministic_row_level_formulation_entity")

    # Phase104.1: collapsed-series subjects are intentionally *not* promoted
    # in the global registry.  Their table records are admitted by a scoped
    # table-gate exception instead.  Global promotion changed text-anchor
    # ambiguity and caused unrelated narrative facts to disappear/appear.

    count_boost = min(0.22, math.log2(len(evidences) + 1) * 0.05)
    score += count_boost
    reasons.append(f"evidence_count:{len(evidences)}")
    if table_count >= 2:
        score += min(0.14, 0.04 * table_count)
        reasons.append(f"multi_table:{table_count}")

    context_hits = 0
    for item in evidences:
        context = " ".join((item.heading, item.preceding_text, item.following_text))
        if name and name in context:
            context_hits += 1
    if context_hits:
        score += min(0.16, context_hits * 0.04)
        reasons.append(f"context_name_hits:{context_hits}")
    if _material_hint(name) or name.startswith("配方:"):
        score += 0.12
        reasons.append("material_name_hint")

    if not name:
        score -= 1.0
        hard_reject = True
        negatives.append("empty_subject")
    if _looks_identifier_only(name):
        score -= 1.0
        hard_reject = True
        negatives.append("identifier_only_subject")
    if not name.startswith("配方:") and (name in _GENERIC_SUBJECTS or is_topic_only_title(name)):
        score -= 0.90
        hard_reject = True
        negatives.append("generic_or_topic_subject")
    title_semantics = classify_heading_subject(name)
    if title_semantics.kind == "multi_entity":
        score -= 0.90
        hard_reject = True
        negatives.append("coordinated_heading_used_as_single_subject")
    if title_semantics.is_entity and title_semantics.entity and title_semantics.entity != name:
        score -= 0.82
        hard_reject = True
        negatives.append("descriptive_heading_used_as_subject")
    if _looks_property_like(name):
        score -= 0.70
        hard_reject = True
        negatives.append("property_or_unit_like_subject")
    if _looks_concatenated(name):
        score -= 0.80
        hard_reject = True
        negatives.append("concatenated_or_oversized_subject")
    if len(name) > 60:
        score -= 0.35
        negatives.append("subject_too_long")
    if " / " in name or name.count("/") >= 2:
        score -= 0.35
        negatives.append("header_path_like_subject")
    if not re.search(r"[A-Za-z0-9\u4e00-\u9fff]", name):
        score -= 0.7
        hard_reject = True
        negatives.append("no_lexical_content")

    return max(0.0, min(1.0, score)), reasons, negatives, hard_reject


def build_subject_registry(
    condition_results: Sequence[Tuple],
) -> Tuple[List[SubjectRegistryEntry], List[SubjectResolution], Dict[str, SubjectRegistryEntry]]:
    grouped: Dict[str, List[SubjectEvidence]] = defaultdict(list)
    aliases: Dict[str, set] = defaultdict(set)
    canonical_by_key: Dict[str, str] = {}
    records: List[ConditionalFactRecord] = []

    for result in condition_results:
        block: TableBlock = result[0]
        result_records: Sequence[ConditionalFactRecord] = result[4]
        for record in result_records:
            records.append(record)
            normalized = normalize_subject_name(record.subject)
            if not normalized.normalized_key:
                key = f"__empty__:{record.record_id}"
            else:
                key = normalized.normalized_key
            canonical_by_key.setdefault(key, normalized.canonical_name)
            aliases[key].add(record.subject)
            aliases[key].update(normalized.aliases)
            grouped[key].append(
                SubjectEvidence(
                    record_id=record.record_id,
                    table_id=record.table_id,
                    raw_name=record.subject,
                    source=record.subject_source,
                    heading=block.heading,
                    preceding_text=block.preceding_text,
                    following_text=block.following_text,
                    record_status=record.record_status,
                    confidence=record.confidence,
                )
            )

    entries: List[SubjectRegistryEntry] = []
    entry_map: Dict[str, SubjectRegistryEntry] = {}
    for key, evidences in grouped.items():
        canonical = canonical_by_key[key]
        score, reasons, negatives, hard_reject = _score_group(canonical, evidences)
        if hard_reject or score < 0.42:
            status = "rejected"
        elif score >= 0.70:
            status = "confirmed"
        else:
            status = "candidate"
        subject_type, type_confidence, type_reasons = infer_subject_type(canonical)
        source_counts = Counter(item.source for item in evidences)
        entry = SubjectRegistryEntry(
            subject_id=_stable_subject_id(key),
            canonical_name=canonical,
            normalized_key=key,
            subject_type=subject_type,
            status=status,
            score=score,
            type_confidence=type_confidence,
            aliases=sorted(x for x in aliases[key] if x and x != canonical),
            evidence_count=len(evidences),
            table_count=len({item.table_id for item in evidences}),
            source_counts=dict(sorted(source_counts.items())),
            reasons=sorted(set(reasons + type_reasons)),
            negative_reasons=sorted(set(negatives)),
        )
        entries.append(entry)
        entry_map[key] = entry

    resolutions: List[SubjectResolution] = []
    for record in records:
        normalized = normalize_subject_name(record.subject)
        key = normalized.normalized_key or f"__empty__:{record.record_id}"
        entry = entry_map[key]
        action = "accept" if entry.status == "confirmed" else ("hold" if entry.status == "candidate" else "reject")
        resolutions.append(
            SubjectResolution(
                record_id=record.record_id,
                table_id=record.table_id,
                raw_subject=record.subject,
                canonical_name=entry.canonical_name,
                normalized_key=entry.normalized_key,
                subject_id=entry.subject_id,
                subject_type=entry.subject_type,
                registry_status=entry.status,
                action=action,
                score=entry.score,
                reasons=entry.reasons + entry.negative_reasons,
            )
        )

    entries.sort(key=lambda item: (item.status != "confirmed", -item.score, item.canonical_name))
    resolutions.sort(key=lambda item: (item.table_id, item.record_id))
    return entries, resolutions, entry_map


__all__ = [
    "SubjectEvidence", "SubjectRegistryEntry", "SubjectResolution", "build_subject_registry",
]
