from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from book_engine.routing.heading_subject_resolver import classify_heading_subject, is_topic_only_title


_GENERIC_SUBJECTS = {
    "本章", "本文", "该方法", "这种方法", "上述方法", "结果", "研究", "分析", "讨论",
    "方法", "模型", "性能", "性质", "指标", "项目", "材料", "样品", "试样", "配方",
    "产品", "物质", "物料", "实验", "试验", "工艺", "条件", "温度", "压力", "时间",
    "密度", "粒度", "粒径", "爆速", "燃速", "含量", "数据", "表格", "体系",
}
_GENERIC_PROPERTIES = {"属性", "指标", "项目", "结果", "数值", "数据", "内容", "说明", "描述", "其他属性", "名称"}
_IDENTITY_PROPERTIES = {
    "中文名称", "英文名称", "中文别称", "英文别称", "别名", "分子式", "分子量",
    "CAS号", "CAS登记号", "化学式", "结构式", "简称", "代号",
}
_GRADE_SUBJECT_RE = re.compile(
    r"^(?:优等品|优级品|一等品|二等品|三等品|合格品|一级品|二级品|三级品|优级纯|分析纯|化学纯|工业级|试剂级|[一二三四五六七八九十]+级|[A-DＡ-Ｄ]级)$",
    re.I,
)
_PROPERTY_LIKE_SUBJECT_RE = re.compile(
    r"^(?:温度|压力|密度|粒度|粒径|爆速|爆压|爆热|燃速|燃烧速度|感度|稳定性|安定性|相容性|含量|分子量|熔点|沸点|闪点|黏度|粘度|强度|模量|应力|应变|时间|速度|距离|直径|热值|热容|系数|指数|性能|性质|指标|方法)(?:[/／].*)?$"
)
_SENTENCE_SUBJECT_RE = re.compile(
    r"[。；：!?！？]|(?:可以|能够|采用|通过|影响|导致|提高|降低|增加|减少|研究表明|结果表明|分别为|主要是|由.+构成)"
)
_LATEX_NOISE_RE = re.compile(r"\\(?:mathrm|begin|end|prime|cdot)|\^\s*\{|\{\s*\\")
_LATEX_PROCESS_SUBJECT_RE = re.compile(r"(?:引发|聚合|反应|合成|制备|处理|测试|测定|研究|体系)")
_REACTION_SENTENCE_SUBJECT_RE = re.compile(r"(?:对.{1,40}进行.{1,30}(?:反应|处理).{0,20}(?:得到|生成|制得)(?:产物)?|经.{1,40}(?:得到|生成|制得)(?:产物)?)")
_BARE_SAMPLE_CODE_RE = re.compile(r"^[A-Za-z]$")
_LEADING_ORPHAN_PUNCT_RE = re.compile(r"^[)）\]】}〉》、，,;；:：]+")
_GLUED_NUMERIC_RE = re.compile(r"(?<!\d)(?:[-+]?\d+\.\d+){2,}(?!\d)")
_MULTIPLE_DECIMAL_RE = re.compile(r"\d+\.\d+\.\d+")
_HEADER_VALUE_RE = re.compile(r"^[^\d]{1,30}[/／]\s*(?:%|℃|K|MPa|kPa|Pa|m/s|mm/s|g/cm(?:\^?3|³)|kg/m(?:\^?3|³)|μm|um|mm|cm|m|s|min|h|度|个/g)$", re.I)
_NUMERIC_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")
_IDENTIFIER_ONLY_SUBJECT_RE = re.compile(
    r"^(?:[A-Za-z]|[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ]|\d+(?:[.．-]\d+)?|[一二三四五六七八九十百]+)(?:号|[#@*※①②③④⑤⑥⑦⑧⑨⑩])?$",
    re.IGNORECASE,
)


def compact_text(value: object) -> str:
    text = str(value or "").casefold()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[，,。；;：:（）()\[\]{}<>≥≤~～—\-_/\\$^'\"·•]", "", text)
    return text


def semantic_key(row: Mapping[str, object]) -> tuple[str, str, str, str]:
    return (
        compact_text(row.get("主体名称")),
        compact_text(row.get("attribute_name") or row.get("predicate_raw")),
        compact_text(row.get("尾实体/取值文本")),
        compact_text(row.get("条件文本")),
    )


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except Exception:
        return default


def _has_numeric_projection(row: Mapping[str, object]) -> bool:
    for name in ("数值", "范围下限", "范围上限", "normalized_value_num"):
        value = row.get(name, "")
        if value is None:
            continue
        if str(value).strip() != "":
            return True
    return False


def _value_supported(value: str, context: str) -> bool:
    cv = compact_text(value)
    cc = compact_text(context)
    if cv and cv in cc:
        return True
    # Numeric values may be normalized while the source uses a different dash or comparator.
    value_numbers = _NUMERIC_RE.findall(value)
    if value_numbers and all(number in context for number in value_numbers):
        return True
    return False


@dataclass(frozen=True)
class EvidenceAssessment:
    score: float
    hard_reject_reasons: Sequence[str] = field(default_factory=tuple)
    review_reasons: Sequence[str] = field(default_factory=tuple)
    positive_reasons: Sequence[str] = field(default_factory=tuple)

    @property
    def has_hard_reject(self) -> bool:
        return bool(self.hard_reject_reasons)


def assess_row(
    row: Mapping[str, object],
    *,
    corroboration_count: int = 1,
    source_adjustments: Mapping[str, float] | None = None,
) -> EvidenceAssessment:
    source_adjustments = source_adjustments or {}
    score = _as_float(row.get("置信度"), 0.0)
    hard: list[str] = []
    review: list[str] = []
    positive: list[str] = []

    source_type = str(row.get("来源类型", "") or "").strip()
    default_adjustments = {
        "table_conditional_record": 0.06,
        "text_labeled_field": 0.08,
        "text_explicit_numeric_property": 0.05,
        "text_process_step": 0.07,
        "text_method_step": 0.07,
        "text_narrative_classification": 0.06,
        "text_narrative_definition": 0.06,
        "text_narrative_application": 0.06,
        "text_narrative_function": 0.06,
        "text_narrative_part_whole": 0.05,
        "text_narrative_connection": 0.05,
        "text_narrative_location": 0.05,
        "text_narrative_method": 0.05,
        "text_narrative_comparison": 0.05,
        "text_narrative_effect": 0.05,
        "text_narrative_causal": 0.04,
        "text_narrative_experiment_result": 0.05,
        "text_narrative_safety": 0.06,
        "text_composition_description": -0.03,
    }
    score += float(source_adjustments.get(source_type, default_adjustments.get(source_type, 0.0)))
    if source_type == "table_conditional_record":
        positive.append("table_source_prior")
    elif source_type == "text_labeled_field":
        positive.append("labeled_field_prior")
    elif source_type in {"text_process_step", "text_method_step"}:
        positive.append("structured_process_step_prior")

    subject = str(row.get("主体名称", "") or "").strip()
    property_name = str(row.get("attribute_name") or row.get("predicate_raw") or "").strip()
    raw_property = str(row.get("predicate_raw", "") or "").strip()
    value = str(row.get("尾实体/取值文本", "") or "").strip()
    evidence = str(row.get("证据文本", "") or "").strip()
    chapter = str(row.get("章节路径", "") or "").strip()
    table_title = str(row.get("所属表格标题", "") or "").strip()
    context = " ".join(item for item in (evidence, chapter, table_title) if item)

    if not subject or not property_name or not value or not evidence:
        hard.append("missing_required_field")

    structured_method_step = bool(
        source_type == "text_method_step"
        and str(row.get("process_id", "") or "").strip()
        and str(row.get("step_id", "") or "").strip()
    )
    if (
        subject in _GENERIC_SUBJECTS
        or _PROPERTY_LIKE_SUBJECT_RE.fullmatch(subject)
        or (is_topic_only_title(subject) and not structured_method_step)
    ):
        hard.append("generic_topic_or_property_like_subject")
    subject_title_semantics = classify_heading_subject(subject)
    if subject_title_semantics.kind == "multi_entity":
        hard.append("coordinated_heading_used_as_single_subject")
    if subject_title_semantics.is_entity and subject_title_semantics.entity and subject_title_semantics.entity != subject:
        hard.append("descriptive_heading_used_as_subject")
    if _BARE_SAMPLE_CODE_RE.fullmatch(subject):
        hard.append("bare_sample_code_subject")
    if _LEADING_ORPHAN_PUNCT_RE.search(subject):
        hard.append("leading_orphan_punctuation_in_subject")
    subject_identifier_surface = re.sub(r"\s+", "", subject).strip(" ,，;；:：|[]()（）")
    subject_identifier_surface = re.sub(
        r"^(?:编号|序号|批次|样品|试样|配方|聚合物|no\.?|id|#)",
        "",
        subject_identifier_surface,
        flags=re.IGNORECASE,
    )
    if subject_identifier_surface and _IDENTIFIER_ONLY_SUBJECT_RE.fullmatch(subject_identifier_surface):
        hard.append("identifier_only_subject")
    if _GRADE_SUBJECT_RE.fullmatch(subject):
        hard.append("grade_only_subject")
    if _REACTION_SENTENCE_SUBJECT_RE.search(subject):
        hard.append("reaction_sentence_used_as_subject")
    if len(subject) > 80:
        hard.append("overlong_subject")
    elif len(subject) > 35 and _SENTENCE_SUBJECT_RE.search(subject):
        hard.append("sentence_like_subject")
    elif len(subject) > 48:
        score -= 0.08
        review.append("long_subject")

    if _LATEX_NOISE_RE.search(subject):
        if len(subject) > 28 and _LATEX_PROCESS_SUBJECT_RE.search(subject):
            hard.append("latex_process_title_used_as_subject")
        else:
            score -= 0.05
            review.append("latex_noise_in_subject")

    if property_name in _GENERIC_PROPERTIES or len(property_name) > 50:
        hard.append("generic_or_overlong_property")
    if compact_text(subject) == compact_text(property_name) and property_name not in _IDENTITY_PROPERTIES:
        hard.append("subject_property_collision")

    if _GLUED_NUMERIC_RE.search(value.replace(" ", "")) or _MULTIPLE_DECIMAL_RE.search(value.replace(" ", "")):
        hard.append("suspected_glued_numeric_value")
    if len(value) > 600:
        hard.append("overlong_value")
    elif len(value) > 250:
        score -= 0.08
        review.append("long_value")

    if _HEADER_VALUE_RE.fullmatch(value) or (
        compact_text(value) in {compact_text(property_name), compact_text(raw_property)}
        and re.search(r"[/／]", value)
    ):
        hard.append("header_text_used_as_value")

    if _value_supported(value, context):
        score += 0.08
        positive.append("value_supported_by_evidence")
    elif source_type.startswith("text_"):
        score -= 0.16
        review.append("text_value_not_found_in_evidence")
    else:
        score -= 0.04
        review.append("table_value_not_found_in_evidence")

    property_compact = compact_text(property_name)
    raw_compact = compact_text(raw_property)
    context_compact = compact_text(context)
    if property_compact and property_compact in context_compact:
        score += 0.04
        positive.append("property_supported_by_context")
    elif raw_compact and raw_compact in context_compact:
        score += 0.04
        positive.append("raw_property_supported_by_context")
    else:
        score -= 0.02
        review.append("property_not_found_in_context")

    subject_compact = compact_text(subject)
    if subject_compact and subject_compact in context_compact:
        score += 0.05
        positive.append("subject_supported_by_context")
    elif source_type.startswith("text_"):
        score -= 0.12
        review.append("text_subject_not_found_in_context")

    category = str(row.get("attribute_category", "") or "").strip()
    alignment_status = str(row.get("_property_alignment_status", "") or "").strip()
    if category in {"", "其他属性"} or alignment_status == "unmapped":
        score -= 0.12
        review.append("unmapped_property")
    elif alignment_status == "aligned_exact":
        score += 0.02
        positive.append("exact_property_alignment")
    elif alignment_status == "aligned_legacy_default":
        score -= 0.04
        review.append("legacy_default_property_alignment")
    elif alignment_status == "ambiguous":
        score -= 0.18
        review.append("ambiguous_property_alignment")

    if source_type in {"text_process_step", "text_method_step"}:
        if not str(row.get("process_id", "") or "").strip() or not str(row.get("step_id", "") or "").strip():
            hard.append("process_step_missing_stable_id")
        if not str(row.get("step_action", "") or "").strip():
            hard.append("process_step_missing_action")
        if str(row.get("step_label", "") or "").strip() and str(row.get("step_label", "") or "").strip() not in evidence:
            hard.append("process_step_not_contiguous_source_span")

    numeric = _has_numeric_projection(row)
    hidden = str(row.get("value_hidden", "") or "").strip()
    if numeric and hidden != "是":
        hard.append("numeric_visibility_policy_mismatch")
    if not numeric and hidden == "是":
        hard.append("text_visibility_policy_mismatch")

    if str(row.get("条件文本", "") or "").strip():
        score += 0.02
        positive.append("condition_bound")
    if str(row.get("方法名称", "") or "").strip():
        score += 0.02
        positive.append("method_bound")

    if corroboration_count > 1:
        bonus = min(0.08, 0.03 * (corroboration_count - 1))
        score += bonus
        positive.append(f"cross_record_corroboration:{corroboration_count}")

    score = max(0.0, min(score, 1.0))
    return EvidenceAssessment(
        score=score,
        hard_reject_reasons=tuple(sorted(set(hard))),
        review_reasons=tuple(sorted(set(review))),
        positive_reasons=tuple(sorted(set(positive))),
    )


__all__ = ["EvidenceAssessment", "assess_row", "compact_text", "semantic_key"]
