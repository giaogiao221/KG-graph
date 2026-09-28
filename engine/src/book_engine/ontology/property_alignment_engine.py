from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, MutableMapping, Optional, Sequence, Tuple

from .property_candidate_generator import CandidateMatch, PropertyCandidateGenerator, clean_property_name
from .property_context_resolver import score_candidate
from .property_llm_adjudicator import PropertyLLMAdjudicator
from .property_ontology_loader import PropertyOntologyEntry, load_property_ontology


@dataclass(frozen=True)
class ScoredPropertyCandidate:
    property_id: str
    canonical_name: str
    attribute_category: str
    ontology_path: str
    root_system: str
    source: str
    match_type: str
    score: float
    reasons: Sequence[str] = field(default_factory=tuple)


@dataclass(frozen=True)
class PropertyAlignmentDecision:
    row_ref: str
    raw_property: str
    cleaned_property: str
    property_id: str
    canonical_name: str
    attribute_category: str
    ontology_path: str
    status: str
    confidence: float
    source: str
    decision_reason: str
    candidates: Sequence[ScoredPropertyCandidate] = field(default_factory=tuple)
    llm_used: bool = False
    llm_status: str = ""
    accepted: bool = True


_DEFAULT_POLICY = {
    "exact_threshold": 0.90,
    "context_threshold": 0.78,
    "ambiguity_margin": 0.07,
    "max_candidates": 8,
    "ambiguous_policy": "exclude",
    "unmapped_policy": "preserve_other",
    "llm_enabled": False,
    "llm_min_confidence": 0.72,
}


def _has_contextual_disambiguation(reasons: Sequence[str]) -> bool:
    return any(
        reason.startswith("positive_context:")
        or reason == "subject_root_match"
        or reason in {"composition_role_match", "process_role_match", "method_context_match"}
        for reason in reasons
    )


def _legacy_default_candidate(
    scored: Sequence[Tuple[CandidateMatch, float, Sequence[str]]]
) -> Optional[Tuple[CandidateMatch, float, Sequence[str]]]:
    # Preserve the old deterministic taxonomy when no root/context evidence separates
    # multiple exact aliases. Curated overrides take priority, then legacy taxonomy.
    preferred_sources = ("curated_override", "phase8a_generic_metric_seed", "legacy_taxonomy_seed")
    for source in preferred_sources:
        for item in scored:
            if item[0].entry.source == source and not _has_contextual_disambiguation(item[2]):
                return item
    return None


_DYNAMIC_PROPERTY_RULES = [
    ("制备工艺", re.compile(r"(?:制备|合成|加工|装配|操作|工艺|流程).*(?:步骤|工序)$|^(?:制备工艺步骤|操作步骤)$")),
    ("组成/配方", re.compile(r"(?:含量|用量|投料量|质量分数|质量百分比|摩尔比|配比|配方组成)$")),
    ("试验结果", re.compile(r"(?:得率|收率|产率|转化率|误差|偏差|增量|损失率|变化率)$")),
    ("力学性能", re.compile(r"(?:应力|应变|模量|强度|硬度|伸长率|断裂)$")),
    ("热性能", re.compile(r"(?:分解峰|峰顶温度|活化能|升华热|热量|热值|热容|热导率)$")),
    ("燃烧性能", re.compile(r"(?:燃速|燃烧时间|释能时间|释能功率|火焰温度|压力指数|比冲)$")),
    ("爆轰/爆炸性能", re.compile(r"(?:爆速|爆压|爆热|威力|猛度|殉爆距离|临界直径|炸高|破甲深度|壁速|膨胀速度|TNT当量)$", re.I)),
    ("安全性能", re.compile(r"(?:感度|爆发点|爆燃点|静电电压|起爆药量|装药量|卡片间隙|防护情况)$")),
    ("稳定性", re.compile(r"(?:稳定性|安定性|半衰期|放气量)$")),
    ("相容性", re.compile(r"(?:相容性|峰顶温度差|热量差值)$")),
    ("物理属性", re.compile(r"(?:密度|粒度|粒径|灰分|水分|挥发分|筛余物|不溶物|蒸气压|气味|物态|黏度|粘度)$")),
    ("化学属性", re.compile(r"(?:元素含量|氧系数|官能度|取代度|反应性|燃烧主产物)$")),
    ("模型/公式", re.compile(r"(?:相关系数|影响系数|分形维数|拟合系数|回归系数)$")),
    ("测试/检测方法", re.compile(r"(?:检验|试验|测试|测定|表征|分析方法)$")),
    ("应用", re.compile(r"(?:适用性|适用范围|应用|用途)$")),
    ("定义/分类", re.compile(r"(?:定义|分类|概念)$")),
    ("功能/作用", re.compile(r"(?:功能|作用|作用影响|影响关系|因果关系|机理描述)$")),
    ("结构/连接", re.compile(r"(?:组成描述|连接关系|安装位置|部件关系)$")),
    ("比较", re.compile(r"(?:比较结论|比较关系|优缺点)$")),
    ("安全/储运", re.compile(r"(?:安全要求|储存要求|贮存要求|运输要求|防护要求)$")),
    ("方法/模型", re.compile(r"(?:方法|方法描述|方法步骤|模型描述|算法步骤)$")),
    ("试验结果", re.compile(r"(?:试验结果|实验结果|测试结果)$")),
]

_PROPERTY_HINT_RE = re.compile(
    r"(?:量|率|度|温度|压力|密度|时间|速度|强度|性能|系数|指数|值|点|距离|长度|直径|深度|高度|含量|组成|方法|用途|稳定性|感度|应力|应变|模量|气味|物态|反应性|情况|关系|功能|要求|描述|结论|分类|定义|位置|范围)$"
)
_INVALID_PROPERTY_RE = re.compile(
    r"^(?:[A-Za-z0-9μµαβγδΔ△]+|目|项|优等品|一等品|二等品|三等品|合格品|优级纯|分析纯|化学纯|通用|耐热|电气|中国|日本|[IVXⅠⅡⅢ]+型|[一二三四五六七八九十]+级)$",
    re.I,
)
_ELEMENT_RE = re.compile(r"^[\u4e00-\u9fff]{1,4}(?:\([A-Z][a-z]?\))?$")


def _synthetic_alignment(cleaned: str, row: Mapping[str, object]) -> Optional[Tuple[str, str, float, str]]:
    if not cleaned:
        return None
    unit = str(row.get("normalized_unit") or row.get("单位") or "")
    context = " ".join(str(row.get(name, "") or "") for name in ("章节路径", "所属表格标题", "证据文本", "事实类型"))

    # A value already identified by the table/text compiler as a formulation component
    # remains a composition fact even when the component is an acronym such as AP or HMX.
    if str(row.get("component_name", "") or "") or "组成" in str(row.get("事实类型", "") or ""):
        return cleaned, "组成/配方", 0.92, "synthetic_compiler_confirmed_component"

    # Element names in a composition/quality table are component-content attributes.
    if _ELEMENT_RE.fullmatch(cleaned) and unit in {"%", "‰"} and re.search(r"(?:元素|组成|含量|成分|指标)", context):
        canonical = cleaned if cleaned.endswith("含量") else cleaned + "含量"
        return canonical, "组成/配方", 0.82, "synthetic_element_content"

    for category, pattern in _DYNAMIC_PROPERTY_RULES:
        if pattern.search(cleaned):
            return cleaned, category, 0.76, "synthetic_suffix_rule"
    return None


def _is_likely_non_property(cleaned: str, row: Mapping[str, object]) -> bool:
    if not cleaned:
        return True
    if _INVALID_PROPERTY_RE.fullmatch(cleaned):
        return True
    if len(cleaned) > 32 or re.search(r"(?:这里讨论|特点是|质量问题|数据转载|如下|分别为|组分为|配方为)$", cleaned):
        return True
    if re.fullmatch(r"[A-Za-z0-9_.+\-=/()]+", cleaned):
        return True
    source_type = str(row.get("来源类型", "") or "")
    if source_type == "table_conditional_record" and not _PROPERTY_HINT_RE.search(cleaned):
        return True
    return False


class PropertyAlignmentEngine:
    def __init__(self, ontology_path: Path, policy_path: Optional[Path] = None, *, cache_dir: Optional[Path] = None):
        self.ontology_path = ontology_path
        self.ontology = load_property_ontology(ontology_path)
        self.policy = dict(_DEFAULT_POLICY)
        if policy_path and policy_path.exists():
            loaded = json.loads(policy_path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                self.policy.update(loaded)
        self.generator = PropertyCandidateGenerator(
            self.ontology, max_candidates=int(self.policy.get("max_candidates", 8))
        )
        llm_enabled = bool(self.policy.get("llm_enabled", False)) or os.getenv("KGCHOUQU_PROPERTY_LLM_ENABLED") == "1"
        self.llm = PropertyLLMAdjudicator(
            enabled=llm_enabled,
            cache_dir=cache_dir,
            timeout=float(self.policy.get("llm_timeout", 120)),
            max_retries=int(self.policy.get("llm_max_retries", 2)),
        )

    @classmethod
    def from_default_config(cls) -> "PropertyAlignmentEngine":
        model_root = Path(__file__).resolve().parents[2]
        config_dir = model_root / "config"
        cache_env = os.getenv("KGCHOUQU_PROPERTY_LLM_CACHE", "").strip()
        cache_dir = Path(cache_env) if cache_env else model_root / "cache" / "property_alignment"
        return cls(
            config_dir / "property_ontology_v2.tsv",
            config_dir / "property_alignment_policy_v2_phase8a.json",
            cache_dir=cache_dir,
        )

    @staticmethod
    def _row_ref(row: Mapping[str, object], index: int) -> str:
        return str(row.get("fact_id", "") or row.get("graph_fact_key", "") or f"row:{index}")

    def align_row(self, row: Mapping[str, object], index: int = 0) -> PropertyAlignmentDecision:
        raw_property = str(row.get("predicate_raw") or row.get("attribute_name") or "").strip()
        cleaned = clean_property_name(raw_property)
        row_ref = self._row_ref(row, index)
        if str(row.get("来源类型", "") or "") in {"text_process_step", "text_method_step"} or str(row.get("step_id", "") or ""):
            process_type = str(row.get("process_type", "") or "")
            if any(token in process_type for token in ("测试", "试验", "检测")):
                canonical_name = "测试步骤"
                category = "测试/检测方法"
            elif any(token in process_type for token in ("计算", "方法", "算法", "模型")):
                canonical_name = "方法步骤"
                category = "方法/模型"
            else:
                canonical_name = "工艺步骤"
                category = "制备工艺"
            property_id = "prop:process-step:" + hashlib.sha1(
                f"{canonical_name}|{category}".encode("utf-8")
            ).hexdigest()[:16]
            return PropertyAlignmentDecision(
                row_ref=row_ref,
                raw_property=raw_property,
                cleaned_property=cleaned,
                property_id=property_id,
                canonical_name=canonical_name,
                attribute_category=category,
                ontology_path="",
                status="aligned_structured_process_step",
                confidence=0.99,
                source="phase91_process_semantics",
                decision_reason="structured_process_step_fields_present",
                candidates=(),
                accepted=True,
            )
        # Compiler-confirmed formulation components are entity labels, not
        # ordinary ontology properties.  Resolve them before fuzzy property
        # matching so an element symbol such as Al is not misaligned to an
        # unrelated property like 氧化铝含量.
        if str(row.get("component_name", "") or "") or "组成" in str(row.get("事实类型", "") or ""):
            canonical = str(row.get("component_name", "") or cleaned).strip() or cleaned
            property_id = "prop:synthetic:" + hashlib.sha1(
                f"{canonical}|组成/配方".encode("utf-8")
            ).hexdigest()[:16]
            return PropertyAlignmentDecision(
                row_ref=row_ref,
                raw_property=raw_property,
                cleaned_property=cleaned,
                property_id=property_id,
                canonical_name=canonical,
                attribute_category="组成/配方",
                ontology_path="",
                status="aligned_synthetic",
                confidence=0.92,
                source="phase100_structured_component",
                decision_reason="structured_component_precedes_property_alias",
                candidates=(),
                accepted=True,
            )

        # Preserve specific composition metrics such as ``氮含量`` and
        # ``固体含量``.  A fuzzy alias match to the parent ``含量`` must not
        # erase the leaf semantics before graph export.
        if cleaned.endswith("含量") and cleaned != "含量" and len(cleaned) <= 16:
            property_id = "prop:synthetic:" + hashlib.sha1(
                f"{cleaned}|组成/配方".encode("utf-8")
            ).hexdigest()[:16]
            return PropertyAlignmentDecision(
                row_ref=row_ref,
                raw_property=raw_property,
                cleaned_property=cleaned,
                property_id=property_id,
                canonical_name=cleaned,
                attribute_category="组成/配方",
                ontology_path="",
                status="aligned_synthetic_leaf",
                confidence=0.90,
                source="phase98_specific_content_leaf",
                decision_reason="specific_content_leaf_precedes_parent_alias",
                candidates=(),
                accepted=True,
            )

        matches = self.generator.generate(cleaned)
        if not matches:
            synthetic = _synthetic_alignment(cleaned, row)
            if synthetic is not None:
                canonical, category, confidence, reason = synthetic
                property_id = "prop:synthetic:" + hashlib.sha1(
                    f"{canonical}|{category}".encode("utf-8")
                ).hexdigest()[:16]
                return PropertyAlignmentDecision(
                    row_ref=row_ref,
                    raw_property=raw_property,
                    cleaned_property=cleaned,
                    property_id=property_id,
                    canonical_name=canonical,
                    attribute_category=category,
                    ontology_path="",
                    status="aligned_synthetic",
                    confidence=confidence,
                    source="phase8a_dynamic_rule",
                    decision_reason=reason,
                    candidates=(),
                    accepted=True,
                )
            if _is_likely_non_property(cleaned, row):
                return PropertyAlignmentDecision(
                    row_ref=row_ref,
                    raw_property=raw_property,
                    cleaned_property=cleaned,
                    property_id="",
                    canonical_name=cleaned or raw_property or "属性",
                    attribute_category="其他属性",
                    ontology_path="",
                    status="rejected_non_property_label",
                    confidence=0.20,
                    source="phase8a_non_property_gate",
                    decision_reason="unmapped_table_axis_label_without_property_semantics",
                    candidates=(),
                    accepted=False,
                )
            accepted = str(self.policy.get("unmapped_policy")) != "exclude"
            return PropertyAlignmentDecision(
                row_ref=row_ref,
                raw_property=raw_property,
                cleaned_property=cleaned,
                property_id="",
                canonical_name=cleaned or raw_property or "属性",
                attribute_category="其他属性",
                ontology_path="",
                status="unmapped",
                confidence=0.35,
                source="fallback_unmapped",
                decision_reason="no_ontology_candidate_but_property_like_label",
                candidates=(),
                accepted=accepted,
            )

        scored: List[Tuple[CandidateMatch, float, Sequence[str]]] = []
        for match in matches:
            context = score_candidate(match, row)
            scored.append((match, context.score, context.reasons))
        scored.sort(key=lambda item: (-item[1], -item[0].entry.priority, item[0].entry.property_id))

        candidate_payload = tuple(
            ScoredPropertyCandidate(
                property_id=match.entry.property_id,
                canonical_name=match.entry.canonical_name,
                attribute_category=match.entry.attribute_category,
                ontology_path=match.entry.ontology_path,
                root_system=match.entry.root_system,
                source=match.entry.source,
                match_type=match.match_type,
                score=round(score, 6),
                reasons=tuple(reasons),
            )
            for match, score, reasons in scored
        )

        best_match, best_score, best_reasons = scored[0]
        best_entry = best_match.entry
        distinct_second = None
        for item in scored[1:]:
            entry = item[0].entry
            if (entry.canonical_name, entry.attribute_category) != (best_entry.canonical_name, best_entry.attribute_category):
                distinct_second = item
                break
        margin = best_score - (distinct_second[1] if distinct_second else 0.0)
        exact = best_match.match_type in {"exact_alias", "exact_canonical"}
        exact_threshold = float(self.policy.get("exact_threshold", 0.90))
        context_threshold = float(self.policy.get("context_threshold", 0.78))
        ambiguity_margin = float(self.policy.get("ambiguity_margin", 0.07))

        if exact and best_score >= exact_threshold and (distinct_second is None or margin >= ambiguity_margin):
            status = "aligned_exact"
        elif best_score >= context_threshold and (distinct_second is None or margin >= ambiguity_margin):
            status = "aligned_context"
        else:
            status = "ambiguous"

        llm_used = False
        llm_status = ""
        if status == "ambiguous" and self.llm.available:
            llm_used = True
            llm_candidates = [asdict(item) for item in candidate_payload]
            selection = self.llm.adjudicate(row, llm_candidates)
            if selection:
                llm_status = selection.status
                if selection.selected_property_id != "unresolved" and selection.confidence >= float(self.policy.get("llm_min_confidence", 0.72)):
                    selected = next((item for item in scored if item[0].entry.property_id == selection.selected_property_id), None)
                    if selected:
                        best_match, best_score, best_reasons = selected
                        best_entry = best_match.entry
                        status = "aligned_llm"
                        best_score = max(best_score, selection.confidence)
                elif selection.status == "llm_failed":
                    llm_status = "llm_failed_closed"

        if status == "ambiguous":
            legacy_default = _legacy_default_candidate(scored)
            if legacy_default is not None:
                best_match, best_score, best_reasons = legacy_default
                best_entry = best_match.entry
                status = "aligned_legacy_default"
                llm_status = llm_status or "not_used_or_unresolved_then_legacy_default"

        accepted = True
        if status == "ambiguous" and str(self.policy.get("ambiguous_policy")) == "exclude":
            accepted = False

        return PropertyAlignmentDecision(
            row_ref=row_ref,
            raw_property=raw_property,
            cleaned_property=cleaned,
            property_id=best_entry.property_id,
            canonical_name=best_entry.canonical_name,
            attribute_category=best_entry.attribute_category,
            ontology_path=best_entry.ontology_path,
            status=status,
            confidence=max(0.0, min(float(best_score), 1.0)),
            source=best_entry.source,
            decision_reason=";".join(best_reasons) + f";margin={margin:.4f}",
            candidates=candidate_payload,
            llm_used=llm_used,
            llm_status=llm_status,
            accepted=accepted,
        )

    def align_rows(
        self, rows: Sequence[Dict[str, object]]
    ) -> Tuple[List[Dict[str, object]], List[PropertyAlignmentDecision]]:
        aligned: List[Dict[str, object]] = []
        decisions: List[PropertyAlignmentDecision] = []
        for index, source_row in enumerate(rows):
            row = dict(source_row)
            decision = self.align_row(row, index=index)
            decisions.append(decision)
            if not decision.accepted:
                continue
            row["attribute_category"] = decision.attribute_category
            row["attribute_category_key"] = "cat:" + _slug(decision.attribute_category)
            row["attribute_name"] = decision.canonical_name
            row["attribute_key"] = (
                "cat:" + _slug(decision.attribute_category) + "::attr:" + _slug(decision.canonical_name)
            )
            row["fact_node_label"] = decision.canonical_name + "事实"
            row["置信度"] = _combine_confidence(row.get("置信度"), decision.confidence)
            _refresh_identity(row)
            aligned.append(row)
        return aligned, decisions


def _slug(text: object) -> str:
    import re

    value = re.sub(r"\s+", "", str(text or "")).casefold()
    value = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "-", value).strip("-")
    return value[:80] or "unknown"


def _combine_confidence(existing: object, alignment: float) -> str:
    try:
        base = float(existing or 0.0)
    except Exception:
        base = 0.0
    if base <= 0:
        combined = alignment
    else:
        combined = min(base, alignment)
    return f"{max(0.0, min(combined, 1.0)):.4f}"


def _refresh_identity(row: MutableMapping[str, object]) -> None:
    identity_fields = [
        "文档ID", "主体名称", "attribute_category", "attribute_name", "尾实体/取值文本",
        "normalized_unit", "条件文本", "来源定位",
    ]
    if row.get("process_id") or row.get("step_id"):
        identity_fields.extend(("process_id", "step_id", "step_index"))
    payload = "|".join(str(row.get(name, "") or "") for name in identity_fields)
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    row["fact_id"] = f"fact:{digest[:20]}"
    row["graph_fact_key"] = f"gfk:{digest}"


__all__ = [
    "PropertyAlignmentDecision",
    "PropertyAlignmentEngine",
    "ScoredPropertyCandidate",
]
