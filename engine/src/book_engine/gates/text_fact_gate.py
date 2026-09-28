from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Mapping, Sequence, Tuple

from book_engine.routing.heading_subject_resolver import classify_heading_subject, is_topic_only_title
from book_engine.routing.text_subject_anchor_resolver import TextSubjectAnchor
from book_engine.text.evidence_self_containment import assess_text_fact, is_deictic_subject
from book_engine.text.text_fact_extractor import TextFactRecord

_PROPERTY_LIKE_SUBJECT_RE = re.compile(r"^(?:密度|熔点|沸点|爆速|爆压|爆热|燃速|性能|性质|方法|结果|条件|参数|应用|用途)$")
_SENTENCE_FRAGMENT_SUBJECT_RE = re.compile(
    r"^(?:得到的|所得的|合成出的|制得的|制成的|得到|获得|这些|上述|最简单并|则|因此|其中|其|采用了|采用|我们在|我们|两个波段|测得的|于)"
    r"|(?:可以|能够|必须|通常|一般|即为|用于|得到|获得|合成|制备|储存|应用)"
)
_METHOD_SUBJECT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._/+\-]{0,30}(?:方法|模型|公式|定律|算法|准则|法)$", re.I)
_TOC_EVIDENCE_RE = re.compile(r"(?:第?\d+\s*[章节篇部].{0,80}?[：:]?\s*\d{1,4}\s*$|[.．·…]{2,}\s*\d{1,4}\s*$)")
_FIGURE_CAPTION_EVIDENCE_RE = re.compile(
    r"^\s*(?:图|表)\s*\d+(?:[-－—.]\d+)?[^。；;]{0,180}(?:关系|曲线|示意|结构|影响|变化|结果)?\s*$",
    re.I,
)
_UNIT_VARIABLE_SUBJECT_RE = re.compile(
    r"^(?:MPa|kPa|GPa|Pa|bar|mbar|K|℃|°C|mm|cm|m|s|ms|μs|ns|Hz|rpm|%|‰|[A-Za-z]\d*)$",
    re.I,
)
_STRONG_RECORD_OWNER_SOURCES = {
    "explicit_property_owner", "postposed_property_owner", "explicit_composition_owner",
    "ownerless_composition_safe_anchor", "explicit_record_local_owner",
    "explicit_clause_owner", "clause_pronoun_owner", "clause_carry_owner",
    "handbook_entry_scope", "specification_entry_scope",
    "leaf_entity_heading_field_owner", "explicit_relation_owner",
    "explicit_narrative_owner", "confirmed_block_anchor", "structured_method_heading",
}


_INVALID_BOUND_OWNER_RE = re.compile(
    r"(?:textcircled|circled|\\[A-Za-z]+)|"
    r"^(?:在|于|当)?\s*[-+−]?\d+(?:[.]\d+)?\s*(?:MPa|kPa|GPa|Pa|bar|℃|°C|K)?\s*(?:压力|温度|时间|含量|用量|浓度|粒度|粒径)|"
    r"^(?:制造|制备|合成|从事|要求)|(?:使得|使|令|表明|显示|说明|测定|报道).*(?:火药|推进剂|炸药|材料|物质|AP|AN|HMX|RDX|TNT)$",
    re.I,
)
_MULTI_VALUE_LABEL_RE = re.compile(r"(?:爆发点|分解温度|熔点|沸点|密度|燃速|爆速|粒度).{0,50}(?:爆发点|分解温度|熔点|沸点|密度|燃速|爆速|粒度)", re.S)
_NUMERIC_TOKEN_RE = re.compile(r"[-+−]?\d+(?:[.,]\d+)?")
_EXPERIMENTAL_CONDITION_PROPERTY = {"含量", "用量", "浓度", "压力", "温度", "时间", "粒度", "粒径"}
_TEMP_PROPERTIES = {"温度", "熔点", "沸点", "闪点", "自燃点", "爆发点", "分解温度", "峰顶温度", "玻璃化温度"}
_SPEED_PROPERTIES = {"燃速", "爆速"}
_LENGTH_PROPERTIES = {"粒度", "粒径"}
_DENSITY_PROPERTIES = {"密度"}
_VISCOSITY_PROPERTIES = {"黏度", "粘度"}
_CONTENT_PROPERTIES = {"含量", "质量分数", "体积分数", "纯度", "氮含量", "氧含量", "氯含量", "铝含量", "硼含量", "水分含量", "灰分含量", "挥发分含量", "固体含量", "有效成分含量", "金属含量", "氧化剂含量", "粘合剂含量", "黏合剂含量"}
_NUMERIC_EXPECTED_PROPERTIES = _TEMP_PROPERTIES | _SPEED_PROPERTIES | _LENGTH_PROPERTIES | _DENSITY_PROPERTIES | _VISCOSITY_PROPERTIES | _CONTENT_PROPERTIES | {"分子量", "相对分子质量", "爆热", "爆压", "比冲", "生成热", "生成焓", "燃烧热", "熔化热", "汽化热", "官能度", "压力指数", "燃速压力指数"}
_GENERIC_FALLBACK_SUBJECTS = {"复合火药", "火药", "推进剂", "炸药", "材料", "体系", "聚合物"}
_CONTEXTUAL_PRODUCT_SUBJECTS = {"产品", "该产品", "最终产品", "所得产品", "所得产物", "最终产物"}
_INSTITUTIONAL_VERBAL_OWNER_RE = re.compile(
    r"(?:公司|厂|研究所|实验室).{0,48}(?:生产|制造|研制|提供)的", re.I
)
_PARALLEL_VALUE_RE = re.compile(r"(?:分别为|依次为|分别是|依次是)")
_TEMP_UNITS = {"℃", "°C", "K"}
_SPEED_UNITS = {"mm/s", "cm/s", "m/s", "km/s"}
_LENGTH_UNITS = {"um", "μm", "nm", "mm", "cm", "目"}
_DENSITY_UNITS = {"g/cm3", "g/cm³", "kg/m3", "kg/m³"}
_CONTENT_UNITS = {"%", "‰", "ppm", "ppb", "vol%", "wt%"}
_VISCOSITY_UNITS = {"Pa·s", "Pa.s", "P"}


def _normalized_unit(value: str) -> str:
    unit = re.sub(r"\s+", "", (value or "").strip())
    unit = unit.replace("μ", "u").replace("⁻", "-").replace("−", "-")
    unit = re.sub(r"^g(?:·|\*)?cm\^?-?3$", "g/cm3", unit, flags=re.I)
    unit = re.sub(r"^kg(?:·|\*)?m\^?-?3$", "kg/m3", unit, flags=re.I)
    unit = re.sub(r"^(km|cm|mm|m)(?:·|\*)s\^?-?1$", r"\1/s", unit, flags=re.I)
    return unit


def _unit_incompatible(record: TextFactRecord) -> bool:
    unit = _normalized_unit(record.unit)
    prop = record.property_name
    if prop in _TEMP_PROPERTIES:
        return unit not in {_normalized_unit(x) for x in _TEMP_UNITS}
    if prop in _SPEED_PROPERTIES:
        return unit not in {_normalized_unit(x) for x in _SPEED_UNITS}
    if prop in _LENGTH_PROPERTIES:
        return unit not in {_normalized_unit(x) for x in _LENGTH_UNITS}
    if prop in _DENSITY_PROPERTIES:
        return unit not in {_normalized_unit(x) for x in _DENSITY_UNITS}
    if prop in _VISCOSITY_PROPERTIES:
        return unit not in {_normalized_unit(x) for x in _VISCOSITY_UNITS}
    if prop in _CONTENT_PROPERTIES:
        return unit not in {_normalized_unit(x) for x in _CONTENT_UNITS}
    return False


def _ambiguous_multi_value_label(record: TextFactRecord) -> bool:
    if record.source_type != "text_labeled_field":
        return False
    value = record.value_text or ""
    if _MULTI_VALUE_LABEL_RE.search(value):
        return True
    if re.search(r"(?:熔化热|熔融热|汽化热|生成热|密度|熔点|沸点|爆发点|分解温度|粒度|燃速|爆速)\s*[：:]", value):
        return True
    # Three or more values cannot be represented as one scalar attribute
    # without clause-level binding, even when OCR removed the field labels.
    return len(_NUMERIC_TOKEN_RE.findall(value)) >= 3


def _condition_projected_as_attribute(record: TextFactRecord) -> bool:
    if record.source_type != "text_clause_bound_numeric_property":
        return False
    if record.property_name not in _EXPERIMENTAL_CONDITION_PROPERTY and not record.property_name.endswith("含量"):
        return False
    clause = record.fact_clause or ""
    if re.search(r"(?:时|下|条件下).{0,80}(?:达到|为|是|可达|提高|降低)", clause):
        return True
    # ``铝粉含量为12%的丁羟复合火药中`` describes a formulation
    # condition of the host material, not an intrinsic property of aluminium.
    if re.search(
        r"(?:含量|用量|浓度|粒度|粒径)\s*(?:为|是)?\s*[-+−]?\d.{0,20}?的"
        r"[A-Za-z0-9+._/\-()\u4e00-\u9fff]{2,45}(?:火药|推进剂|炸药|配方|体系)(?:中|内)",
        clause,
        re.I,
    ):
        return True
    return False


def _definition_projected_as_material_property(record: TextFactRecord) -> bool:
    if record.source_type != "text_labeled_field":
        return False
    value = (record.value_text or "").strip()
    if re.search(r"^(?:是指|定义为|指的是|表示|由.{0,100}所需的(?:最低|最小|最大)?(?:能量|压力|温度|时间))", value):
        # Numeric standards may still be valid measurements; this guard only
        # holds prose definitions without a scalar/range result.
        return not bool(_NUMERIC_TOKEN_RE.search(value))
    return False


@dataclass
class TextGateDecision:
    record_id: str
    accepted: bool
    action: str
    canonical_subject: str
    subject_type: str
    confidence: float
    reasons: List[str] = field(default_factory=list)
    evidence_self_contained: bool = True
    dependency_type: str = ""
    visual_reference: str = ""
    fact_clause: str = ""


def gate_text_facts(
    records: Sequence[TextFactRecord],
    anchors_by_block: Mapping[str, TextSubjectAnchor],
) -> Tuple[List[Tuple[TextFactRecord, TextGateDecision]], List[TextGateDecision]]:
    accepted: List[Tuple[TextFactRecord, TextGateDecision]] = []
    decisions: List[TextGateDecision] = []

    for record in records:
        anchor = anchors_by_block.get(record.block_id)
        reasons: List[str] = []
        allow = True
        local_owner_confirmed = bool(record.owner_source in _STRONG_RECORD_OWNER_SOURCES and record.subject)
        if (anchor is None or anchor.status != "confirmed") and not local_owner_confirmed:
            allow = False
            reasons.append("subject_anchor_not_confirmed")
        if record.record_status != "ready":
            allow = False
            reasons.append(f"record_status_{record.record_status}")
        structured_method_step = record.relation_kind == "method_step" and record.source_type == "text_method_step"
        if (
            not record.subject
            or _PROPERTY_LIKE_SUBJECT_RE.match(record.subject)
            or (is_topic_only_title(record.subject) and not structured_method_step)
        ):
            allow = False
            reasons.append("invalid_topic_or_property_like_subject")
        if is_deictic_subject(record.subject):
            allow = False
            reasons.append("unresolved_deictic_subject")
        title_semantics = classify_heading_subject(record.subject)
        if title_semantics.kind == "multi_entity":
            allow = False
            reasons.append("coordinated_heading_used_as_single_subject")
        if title_semantics.is_entity and title_semantics.entity and title_semantics.entity != record.subject:
            allow = False
            reasons.append("descriptive_heading_used_as_subject")
        if re.fullmatch(r"[A-Za-z]", record.subject or ""):
            allow = False
            reasons.append("single_letter_variable_subject")
        if (
            (_METHOD_SUBJECT_RE.fullmatch(record.subject or "") and not structured_method_step)
            or _SENTENCE_FRAGMENT_SUBJECT_RE.search(record.subject or "")
        ):
            allow = False
            reasons.append("method_or_sentence_fragment_subject")
        if record.subject in _CONTEXTUAL_PRODUCT_SUBJECTS:
            allow = False
            reasons.append("contextual_product_subject_requires_process_binding")
        if _INSTITUTIONAL_VERBAL_OWNER_RE.search(record.subject or ""):
            allow = False
            reasons.append("institutional_verbal_phrase_used_as_subject")
        if (
            record.source_type == "text_clause_bound_numeric_property"
            and _PARALLEL_VALUE_RE.search(record.fact_clause or "")
        ):
            allow = False
            reasons.append("parallel_owner_value_alignment_unresolved")
        if (
            record.source_type == "text_clause_bound_numeric_property"
            and record.owner_source == "clause_carry_owner"
            and record.subject
            and record.subject not in (record.fact_clause or "")
            and re.search(r"[A-Za-z][A-Za-z0-9+._/()\-]{1,24}.*(?:粒径|粒度|密度|含量|燃速)", record.fact_clause or "", re.I)
        ):
            allow = False
            reasons.append("carried_owner_conflicts_with_explicit_local_owner")
        if _UNIT_VARIABLE_SUBJECT_RE.fullmatch(record.subject or ""):
            allow = False
            reasons.append("unit_or_variable_subject")
        if _INVALID_BOUND_OWNER_RE.search(record.subject or ""):
            allow = False
            reasons.append("invalid_clause_owner_surface")
        if re.match(r"^(?:大多数|多数|各种|几种|若干种)", record.subject or ""):
            allow = False
            reasons.append("quantified_generic_subject_surface")
        if (record.subject or "").count("(") != (record.subject or "").count(")") or (record.subject or "").count("（") != (record.subject or "").count("）"):
            allow = False
            reasons.append("unbalanced_parenthesis_in_subject")
        if (
            record.owner_source == "safe_heading_fallback"
            and record.subject in _GENERIC_FALLBACK_SUBJECTS
            and record.subject not in (record.fact_clause or "")
        ):
            allow = False
            reasons.append("generic_heading_fallback_without_clause_owner")
        if (
            record.owner_source in {"clause_carry_owner", "explicit_clause_owner"}
            and re.fullmatch(r"[A-Za-z0-9+._/\-()]{3,40}", record.subject or "")
            and "结构" in (record.evidence or "")
        ):
            clause = record.fact_clause or ""
            explicit_owner_pattern = re.compile(
                re.escape(record.subject or "")
                + r"\s*(?:的)?\s*"
                + re.escape(record.property_name or "")
            )
            if not explicit_owner_pattern.search(clause):
                allow = False
                reasons.append("structural_formula_fragment_used_as_owner")
        if (
            record.source_type == "text_labeled_field"
            and record.subject in _GENERIC_FALLBACK_SUBJECTS
            and record.heading_path
            and re.search(r"(?:和|与|及|、)", record.heading_path[-1])
        ):
            allow = False
            reasons.append("multi_entity_heading_field_owner_unresolved")
        if record.source_type == "text_clause_bound_numeric_property" and _FIGURE_CAPTION_EVIDENCE_RE.fullmatch((record.evidence or "").strip()):
            allow = False
            reasons.append("figure_or_table_caption_evidence")
        if _TOC_EVIDENCE_RE.search((record.evidence or "").strip()):
            allow = False
            reasons.append("table_of_contents_or_page_number_evidence")
        if not record.property_name or record.property_name in {"说明", "备注", "结果", "参数"}:
            allow = False
            reasons.append("invalid_property")
        if not record.value_text:
            allow = False
            reasons.append("empty_value")
        if (record.property_name in _NUMERIC_EXPECTED_PROPERTIES or record.property_name.endswith("含量")) and not _NUMERIC_TOKEN_RE.search(record.value_text or ""):
            allow = False
            reasons.append("numeric_property_without_numeric_value")
        if len(record.value_text) > 1600:
            allow = False
            reasons.append("value_too_long")
        if _ambiguous_multi_value_label(record):
            allow = False
            reasons.append("multi_value_labeled_field_requires_clause_binding")
        if _condition_projected_as_attribute(record):
            allow = False
            reasons.append("experimental_condition_projected_as_attribute")
        if _definition_projected_as_material_property(record):
            allow = False
            reasons.append("definition_text_projected_as_material_property")
        if record.source_type == "text_clause_bound_numeric_property" and _unit_incompatible(record):
            allow = False
            reasons.append("property_unit_dimension_mismatch")
        if structured_method_step and (not record.process_id or not record.step_id or not record.step_action):
            allow = False
            reasons.append("structured_method_step_incomplete")
        if record.confidence < 0.70:
            allow = False
            reasons.append("confidence_below_0_70")

        assessment = assess_text_fact(record)
        if not assessment.self_contained:
            allow = False
            reasons.extend(assessment.reasons)

        decision = TextGateDecision(
            record_id=record.record_id,
            accepted=allow,
            action="export" if allow else "hold",
            canonical_subject=record.subject,
            subject_type=record.subject_type,
            confidence=record.confidence,
            reasons=sorted(set(reasons)),
            evidence_self_contained=assessment.self_contained,
            dependency_type=assessment.dependency_type,
            visual_reference=assessment.visual_reference,
            fact_clause=assessment.fact_clause,
        )
        decisions.append(decision)
        if allow:
            accepted.append((record, decision))

    return accepted, decisions


__all__ = ["TextGateDecision", "gate_text_facts"]
