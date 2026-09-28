from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Sequence, Tuple


@dataclass(frozen=True)
class GeneralizedReleaseDecision:
    fact_id: str
    graph_fact_key: str
    action: str
    score: float
    threshold: float
    source_type: str
    origin_stream: str
    strict_released: bool
    gate_reasons: Tuple[str, ...] = field(default_factory=tuple)
    semantic_reasons: Tuple[str, ...] = field(default_factory=tuple)
    hard_reasons: Tuple[str, ...] = field(default_factory=tuple)
    soft_reasons: Tuple[str, ...] = field(default_factory=tuple)
    positive_reasons: Tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class GeneralizedReleaseResult:
    released_rows: Sequence[Dict[str, object]]
    candidate_rows: Sequence[Dict[str, object]]
    rejected_rows: Sequence[Dict[str, object]]
    decisions: Sequence[GeneralizedReleaseDecision]
    profile: str


def _default_policy_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "generalized_release_policy_v105.json"


def _load_policy(path: Path | None = None) -> Dict[str, object]:
    target = path or _default_policy_path()
    return json.loads(target.read_text(encoding="utf-8-sig"))


def _float(value: object, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except Exception:
        return default


def _split_reasons(value: object) -> Tuple[str, ...]:
    if isinstance(value, (list, tuple, set)):
        return tuple(sorted({str(item).strip() for item in value if str(item).strip()}))
    text = str(value or "")
    return tuple(sorted({item.strip() for item in re.split(r"[;|]", text) if item.strip()}))


def _compact(value: object) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _source_threshold(source_type: str, thresholds: Mapping[str, object]) -> float:
    if source_type in thresholds:
        return float(thresholds[source_type])
    if source_type.startswith("text_narrative_"):
        return float(thresholds.get("text_narrative_default", 0.58))
    if source_type.startswith("text_generalized_"):
        return float(thresholds.get("default", 0.60))
    if source_type.startswith("text_"):
        return float(thresholds.get("default", 0.60))
    return float(thresholds.get("default", 0.60))


def _surface_hard_reasons(row: Mapping[str, object]) -> List[str]:
    reasons: List[str] = []
    subject = str(row.get("主体名称", "") or "").strip()
    prop = str(row.get("attribute_name") or row.get("predicate_raw") or "").strip()
    value = str(row.get("尾实体/取值文本", "") or "").strip()
    evidence = str(row.get("证据文本", "") or "").replace("\\n", "\n").strip()
    compact_subject = _compact(subject)
    compact_value = _compact(value)
    if not subject or not prop or not value or not evidence:
        reasons.append("missing_required_field")
    if len(compact_subject) > 80 or re.search(r"[。；;：:]", subject):
        reasons.append("overlong_or_sentence_subject")
    if re.fullmatch(r"[-+−]?\d+(?:[.,]\d+)?|[A-Za-z]|(?:MPa|kPa|Pa|K|℃|°C|mm|cm|m|s|ms|μs|%|‰|ppm)", compact_subject, re.I):
        reasons.append("numeric_unit_or_variable_subject")
    if re.search(r"(?:^|\n)\s*(?:第?\d+章|\d+(?:[.．]\d+)+).{0,80}?(?:[.．·…]{2,}|[：:]\s*)?\d{1,4}\s*$", evidence):
        reasons.append("toc_or_page_index_evidence")
    if re.search(r"(?:\d+\.\d+\.\d+|\.\d+\.\d+)", compact_value):
        reasons.append("glued_numeric_surface")
    if len(value) > 1600:
        reasons.append("overlong_value")
    if subject.count("(") != subject.count(")") or subject.count("（") != subject.count("）"):
        reasons.append("unbalanced_subject")
    if prop in {"参考文献", "页码", "序号", "图号", "表号"}:
        reasons.append("bibliographic_or_index_property")
    if compact_value in {"总计", "合计", "小计", "见图", "见表", "同上", "—", "-"}:
        reasons.append("summary_or_placeholder_value")
    if re.fullmatch(r"(?:数目|长度|面积|体积|质量|温度|压力){2,}", compact_subject):
        reasons.append("concatenated_axis_subject")
    if len(compact_subject) >= 24 and re.search(r"(?:的|了|为|是|在|由|可|并|即|将|使)", compact_subject):
        reasons.append("long_verbal_subject")
    if re.match(r"^(?:并|且|而|则|因而|从而|因此|所以|实际|仅|不|可见|由此|这|该|其|它)", compact_subject):
        reasons.append("discourse_or_deictic_subject")
    if source := str(row.get("来源类型", "") or ""):
        if source.startswith("table_") and prop in {"组成", "组成描述"} and len(compact_value) > 36 and not re.search(r"[、,，;/；]", value):
            reasons.append("concatenated_table_component_value")
    return reasons


def _support_adjustments(row: Mapping[str, object]) -> tuple[float, List[str]]:
    score = 0.0
    positive: List[str] = []
    source = str(row.get("来源类型", "") or "")
    subject = _compact(row.get("主体名称"))
    prop = _compact(row.get("attribute_name") or row.get("predicate_raw"))
    raw_prop = _compact(row.get("predicate_raw"))
    value = str(row.get("尾实体/取值文本", "") or "")
    evidence = str(row.get("证据文本", "") or "")
    context = _compact(evidence + str(row.get("章节路径", "") or "") + str(row.get("所属表格标题", "") or ""))
    if subject and subject in context:
        score += 0.05
        positive.append("subject_supported_by_context")
    elif source.startswith("text_"):
        score -= 0.05
    if (prop and prop in context) or (raw_prop and raw_prop in context):
        score += 0.03
        positive.append("property_supported_by_context")
    value_compact = _compact(value)
    if value_compact and value_compact in context:
        score += 0.07
        positive.append("value_supported_by_evidence")
    else:
        numbers = re.findall(r"[-+−]?\d+(?:[.,]\d+)?", value)
        if numbers and all(number.replace(",", ".") in evidence.replace(",", ".") for number in numbers):
            score += 0.05
            positive.append("numeric_value_supported_by_evidence")
        elif source.startswith("text_"):
            score -= 0.06
    if str(row.get("条件文本", "") or "").strip():
        score += 0.02
        positive.append("condition_bound")
    if str(row.get("方法名称", "") or "").strip():
        score += 0.02
        positive.append("method_bound")
    if str(row.get("_evidence_self_contained", "1")).lower() in {"1", "true", "yes", "是"}:
        score += 0.03
        positive.append("evidence_self_contained")
    else:
        score -= 0.10
    if str(row.get("_gate_accepted", "0")).lower() in {"1", "true", "yes", "是"}:
        score += 0.03
        positive.append("passed_source_gate")
    return score, positive


def apply_generalized_release_gate(
    rows: Sequence[Dict[str, object]],
    *,
    strict_fact_ids: Sequence[str] = (),
    semantic_reason_by_fact: Mapping[str, Sequence[str]] | None = None,
    policy_path: Path | None = None,
) -> GeneralizedReleaseResult:
    policy = _load_policy(policy_path)
    thresholds = policy.get("source_thresholds", {})
    thresholds = thresholds if isinstance(thresholds, dict) else {}
    adjustments = policy.get("source_adjustments", {})
    adjustments = adjustments if isinstance(adjustments, dict) else {}
    penalties = policy.get("reason_penalties", {})
    penalties = penalties if isinstance(penalties, dict) else {}
    hard_gate = {str(item) for item in policy.get("hard_gate_reasons", [])}
    hard_semantic = {str(item) for item in policy.get("hard_semantic_reasons", [])}
    candidate_threshold = float(policy.get("candidate_threshold", 0.38))
    strict_ids = set(strict_fact_ids)
    semantic_reason_by_fact = semantic_reason_by_fact or {}

    released: List[Dict[str, object]] = []
    candidates: List[Dict[str, object]] = []
    rejected: List[Dict[str, object]] = []
    decisions: List[GeneralizedReleaseDecision] = []

    for original in rows:
        row = dict(original)
        fact_id = str(row.get("fact_id", "") or "")
        source = str(row.get("来源类型", "") or "")
        strict = fact_id in strict_ids or str(row.get("_strict_released", "0")) == "1"
        gate_reasons = _split_reasons(row.get("_gate_reasons", ""))
        semantic_reasons = tuple(sorted(set(_split_reasons(row.get("_semantic_reasons", ""))) | set(semantic_reason_by_fact.get(fact_id, ()))))
        surface_hard = _surface_hard_reasons(row)
        hard = sorted(
            set(surface_hard)
            | (set(gate_reasons) & hard_gate)
            | (set(semantic_reasons) & hard_semantic)
        )
        soft = sorted((set(gate_reasons) | set(semantic_reasons)) - set(hard))
        threshold = _source_threshold(source, thresholds)
        score = _float(row.get("置信度"), 0.0) + float(adjustments.get(source, 0.0))
        support_delta, positive = _support_adjustments(row)
        score += support_delta
        for reason in soft:
            score -= float(penalties.get(reason, 0.03))
        score = max(0.0, min(score, 1.0))

        if strict:
            action = "release"
            score = max(score, threshold)
            positive.append("strict_high_precision_release")
            hard = []
        elif hard:
            action = "reject"
        elif score >= threshold:
            action = "release"
        elif score >= candidate_threshold:
            action = "candidate"
        else:
            action = "reject"

        row["_generalized_score"] = f"{score:.6f}"
        row["_generalized_action"] = action
        row["_generalized_reasons"] = "|".join(soft)
        if action == "release":
            row["抽取来源"] = (
                str(row.get("抽取来源", "") or "")
                if strict
                else str(row.get("抽取来源", "") or "") + "|phase105_generalized_release"
            ).strip("|")
            released.append(row)
        elif action == "candidate":
            candidates.append(row)
        else:
            rejected.append(row)

        decisions.append(
            GeneralizedReleaseDecision(
                fact_id=fact_id,
                graph_fact_key=str(row.get("graph_fact_key", "") or ""),
                action=action,
                score=score,
                threshold=threshold,
                source_type=source,
                origin_stream=str(row.get("_origin_stream", "") or ""),
                strict_released=strict,
                gate_reasons=gate_reasons,
                semantic_reasons=semantic_reasons,
                hard_reasons=tuple(hard),
                soft_reasons=tuple(soft),
                positive_reasons=tuple(sorted(set(positive))),
            )
        )

    return GeneralizedReleaseResult(
        released_rows=released,
        candidate_rows=candidates,
        rejected_rows=rejected,
        decisions=decisions,
        profile=str(policy.get("profile", "generalized_80")),
    )


__all__ = [
    "GeneralizedReleaseDecision",
    "GeneralizedReleaseResult",
    "apply_generalized_release_gate",
]
