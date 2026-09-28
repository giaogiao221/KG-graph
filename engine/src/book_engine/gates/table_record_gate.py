from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple

from book_engine.core.schemas import ConditionalFactRecord
from book_engine.routing.subject_name_normalizer import normalize_subject_name
from book_engine.routing.subject_registry_builder import SubjectRegistryEntry

_PROPERTY_TERMS = (
    "密度", "熔点", "沸点", "爆速", "爆压", "爆热", "燃速", "比冲", "感度", "释能时间", "释能功率",
    "峰顶温度", "分解温度", "生成热", "氧平衡", "粒径", "粒度", "黏度", "粘度", "上限", "下限",
)
_IDENTIFIER_ONLY_RE = re.compile(
    r"^(?:[A-Za-z]|[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ]|\d+(?:[.．-]\d+)?|[一二三四五六七八九十百]+)(?:号|[#@*※①②③④⑤⑥⑦⑧⑨⑩])?$",
    re.IGNORECASE,
)
_MATERIAL_HINT_RE = re.compile(
    r"(?:HMX|RDX|TNT|PETN|CL-20|NTO|ADN|AP|AN|GAP|HTPB|BAMO|NIMMO|FOX-7|硝酸|硝基|叠氮|高氯酸|聚合物|推进剂|炸药|药剂)",
    re.I,
)
_COLLAPSED_SERIES_SOURCE = "collapsed_material_code_sequence_recovery_phase104"
_COLLAPSED_SERIES_MATERIAL_CODES = {
    "CL-20(HNIW)", "HMX(Β)", "HMX(Α)", "PBXN-109", "PBX-9501", "PBX-9502",
    "CL-20", "FOX-7", "TNAZ", "PETN", "TATB", "RDX", "HMX", "TNT", "NTO",
    "ADN", "ONC", "NIGU", "GAP", "HTPB", "BAMO", "AMMO", "NIMMO", "NEPE",
    "CMDB", "AP", "AN", "NG", "NC", "AL",
}


def _scoped_subject_id(key: str) -> str:
    return "subj:" + hashlib.sha1((key or "").encode("utf-8")).hexdigest()[:20]



@dataclass
class RecordGateDecision:
    record_id: str
    table_id: str
    accepted: bool
    action: str
    canonical_subject: str
    subject_id: str
    subject_type: str
    original_status: str
    final_status: str
    confidence: float
    reasons: List[str] = field(default_factory=list)


def _property_like(value: str) -> bool:
    return any(term in (value or "") for term in _PROPERTY_TERMS)


def _material_like(value: str) -> bool:
    return bool(_MATERIAL_HINT_RE.search(value or ""))


def _identifier_only(value: str) -> bool:
    surface = re.sub(r"\s+", "", value or "").strip(" ,，;；:：|[]()（）")
    surface = re.sub(r"^(?:编号|序号|批次|样品|试样|配方|聚合物|no\.?|id|#)", "", surface, flags=re.IGNORECASE)
    return bool(surface and _IDENTIFIER_ONLY_RE.fullmatch(surface))


def gate_table_records(
    condition_results: Sequence[Tuple],
    registry_by_key: Mapping[str, SubjectRegistryEntry],
) -> Tuple[List[Tuple[ConditionalFactRecord, RecordGateDecision, object]], List[RecordGateDecision]]:
    accepted: List[Tuple[ConditionalFactRecord, RecordGateDecision, object]] = []
    decisions: List[RecordGateDecision] = []

    for result in condition_results:
        block = result[0]
        plan = result[2]
        records: Sequence[ConditionalFactRecord] = result[4]
        for record in records:
            normalized = normalize_subject_name(record.subject)
            key = normalized.normalized_key or f"__empty__:{record.record_id}"
            entry = registry_by_key.get(key)
            reasons: List[str] = []
            allow = True
            final_status = record.record_status
            structural_row_formulation = (
                record.subject_source == "row_level_formulation_entity_phase100"
                and bool(re.fullmatch(r"配方:T\d+:R\d+", record.subject or ""))
                and (record.subject_type or "") == "配方/材料体系"
                and record.record_status == "ready"
                and record.confidence >= 0.90
                and any((atom.normalized_name or atom.name) in {"样品代号", "样品编号"} for atom in record.conditions)
            )
            scoped_collapsed_series = (
                record.subject_source == _COLLAPSED_SERIES_SOURCE
                and record.record_status == "ready"
                and record.confidence >= 0.82
                and (record.subject or "").upper() in _COLLAPSED_SERIES_MATERIAL_CODES
            )

            if scoped_collapsed_series:
                subject_id = entry.subject_id if entry else _scoped_subject_id(key)
                canonical = record.subject
                subject_type = "材料"
                reasons.append("scoped_count_bound_collapsed_series_material")
            elif structural_row_formulation:
                subject_id = entry.subject_id if entry else f"subj:{record.table_id}:R{record.row_index}"
                canonical = record.subject
                subject_type = "配方/材料体系"
                reasons.append("structurally_recovered_row_formulation_entity")
            elif entry is None:
                allow = False
                reasons.append("subject_not_in_registry")
                subject_id = ""
                canonical = normalized.canonical_name
                subject_type = record.subject_type or ""
            else:
                subject_id = entry.subject_id
                canonical = entry.canonical_name
                subject_type = entry.subject_type
                if entry.status != "confirmed":
                    allow = False
                    reasons.append(f"subject_registry_{entry.status}")

            if not record.property_name or record.property_name == "未命名属性":
                allow = False
                reasons.append("property_unresolved")
            if not record.value_text:
                allow = False
                reasons.append("empty_value")
            if record.record_status == "unresolved":
                allow = False
                reasons.append("record_unresolved")
            elif record.record_status == "candidate":
                removable = {"subject_from_context_requires_registry_confirmation"}
                residual = set(record.unresolved_reasons) - removable
                if residual:
                    allow = False
                    reasons.extend(sorted(f"record_reason:{item}" for item in residual))
                elif scoped_collapsed_series:
                    final_status = "ready_scoped_collapsed_series"
                    reasons.append("collapsed_series_promoted_by_scoped_table_gate")
                elif structural_row_formulation:
                    final_status = "ready_structural_row_formulation"
                    reasons.append("row_formulation_promoted_by_structure")
                elif entry and entry.status == "confirmed":
                    final_status = "ready_promoted_by_registry"
                    reasons.append("context_subject_confirmed_by_registry")
                else:
                    allow = False
                    reasons.append("candidate_not_promotable")

            # Detect common axis inversion: property/unit text as subject and material as property.
            if _property_like(canonical) and _material_like(record.property_name):
                allow = False
                reasons.append("suspected_subject_property_axis_inversion")
            if _identifier_only(canonical) and not structural_row_formulation and not scoped_collapsed_series:
                allow = False
                reasons.append("identifier_only_subject")
            if len(canonical) > 70:
                allow = False
                reasons.append("canonical_subject_too_long")
            if record.confidence < 0.50:
                allow = False
                reasons.append("record_confidence_below_0_50")
            if plan.confidence < 0.48 and not structural_row_formulation and not scoped_collapsed_series:
                allow = False
                reasons.append("table_plan_confidence_below_0_48")

            decision = RecordGateDecision(
                record_id=record.record_id,
                table_id=record.table_id,
                accepted=allow,
                action="export" if allow else "hold",
                canonical_subject=canonical,
                subject_id=subject_id,
                subject_type=subject_type,
                original_status=record.record_status,
                final_status=final_status if allow else "held",
                confidence=(record.confidence if (structural_row_formulation or scoped_collapsed_series) else min(record.confidence, entry.score if entry else 0.0)),
                reasons=sorted(set(reasons)),
            )
            decisions.append(decision)
            if allow:
                accepted.append((record, decision, plan))

    return accepted, decisions


__all__ = ["RecordGateDecision", "gate_table_records"]
