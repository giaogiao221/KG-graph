from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Sequence, Tuple

from .property_candidate_generator import CandidateMatch
from .property_ontology_loader import normalize_key


_ROOT_HINTS = [
    ("发射药", ("发射药", "枪药", "炮药", "gunpropellant")),
    ("火工药剂", ("火工药剂", "延期药", "点火药", "起爆药", "击发药", "针刺药", "烟火药", "pyrotechnic")),
    ("固体推进剂", ("固体推进剂", "推进剂", "solidpropellant")),
    ("混合炸药", ("混合炸药", "pbx", "配方炸药", "复合炸药")),
    ("单质炸药", ("单质炸药", "炸药", "explosivesubstance")),
]

_CATEGORY_CONTEXT = {
    "标识信息": ("名称", "别名", "编号", "cas", "分子式", "分子量", "牌号", "型号"),
    "组成/配方": ("组成", "组分", "配方", "含量", "质量分数", "配比"),
    "结构/形貌": ("结构", "晶型", "形貌", "粒形", "表面", "孔隙", "缺陷"),
    "物理属性": ("密度", "熔点", "沸点", "粒度", "粒径", "颜色", "外观", "溶解", "吸湿"),
    "化学属性": ("分子式", "化学式", "氧平衡", "反应性", "官能团", "元素"),
    "热性能": ("热分析", "dsc", "tg", "热分解", "分解温度", "热容", "热导", "放热峰"),
    "燃烧性能": ("燃烧", "燃速", "比冲", "压力指数", "火焰", "推进"),
    "爆轰/爆炸性能": ("爆轰", "爆炸", "爆速", "爆压", "猛度", "威力", "爆热"),
    "力学性能": ("拉伸", "压缩", "模量", "断裂", "应变", "强度", "力学"),
    "安全性能": ("安全", "感度", "危险", "爆发点", "冲击", "摩擦", "静电"),
    "稳定性": ("稳定", "老化", "安定", "储存期", "保存期"),
    "相容性": ("相容", "兼容", "配伍", "混合峰温"),
    "工艺参数": ("工艺条件", "温度", "压力", "时间", "转速", "浓度", "流量"),
    "制备工艺": ("制备", "合成", "结晶", "包覆", "混合", "干燥", "压制", "固化"),
    "试验条件": ("试验条件", "测试条件", "环境温度", "压力为", "加热速率", "样品质量"),
    "试验结果": ("试验结果", "测试结果", "实测", "计算值", "误差", "收率", "产率"),
    "性能影响": ("影响", "提高", "降低", "增加", "减少", "改善", "促进", "抑制"),
    "影响因素": ("影响因素", "决定因素", "控制因素", "原因"),
    "应用": ("应用", "应用于", "用于", "领域", "场景"),
    "用途": ("用途", "用作", "作为"),
    "标准/要求": ("标准", "规范", "要求", "限值", "判据"),
    "储存/运输": ("储存", "贮存", "运输", "包装", "储运", "仓储"),
    "毒理/环境": ("毒性", "毒理", "ld50", "lc50", "健康", "腐蚀", "刺激", "环境", "生物降解"),
    "模型/公式": ("模型", "公式", "方程", "算法", "拟合", "计算式"),
    "测试/检测方法": ("测试方法", "检测方法", "表征方法", "仪器", "测定", "分析方法"),
    "来源/出处": ("来源", "文献", "作者", "报道", "参考文献"),
}

_UNIT_DIMENSION_PATTERNS = [
    (re.compile(r"^(?:℃|°c|k)$", re.I), "temperature"),
    (re.compile(r"^(?:pa|kpa|mpa|gpa)$", re.I), "pressure"),
    (re.compile(r"^(?:g/cm3|g/cm³|kg/m3|kg/m³)$", re.I), "density"),
    (re.compile(r"^(?:nm|μm|um|mm|cm|m)$", re.I), "length"),
    (re.compile(r"^(?:ns|μs|us|ms|s|min|h|d)$", re.I), "time"),
    (re.compile(r"^(?:mm/s|cm/s)$", re.I), "burning_rate"),
    (re.compile(r"^(?:m/s|km/s)$", re.I), "detonation_velocity"),
    (re.compile(r"^(?:%|‰)$", re.I), "fraction"),
    (re.compile(r"^(?:j/g|kj/kg)$", re.I), "energy_per_mass"),
    (re.compile(r"^(?:j/mol|kj/mol)$", re.I), "energy_per_mole"),
    (re.compile(r"^(?:n·s/kg|ns/kg|s)$", re.I), "specific_impulse"),
    (re.compile(r"^(?:mg/kg|g/kg|mg/m3)$", re.I), "dose"),
]


def infer_root_system(subject_type: object) -> str:
    key = normalize_key(subject_type)
    for root, hints in _ROOT_HINTS:
        if any(normalize_key(hint) in key for hint in hints):
            return root
    return ""


def infer_unit_dimension(unit: object) -> str:
    value = str(unit or "").strip().replace(" ", "")
    for pattern, dimension in _UNIT_DIMENSION_PATTERNS:
        if pattern.match(value):
            return dimension
    return ""


@dataclass(frozen=True)
class ContextScore:
    score: float
    reasons: Tuple[str, ...]


def score_candidate(candidate: CandidateMatch, row: Mapping[str, object]) -> ContextScore:
    entry = candidate.entry
    score = candidate.lexical_score
    reasons = [candidate.match_type]

    subject_root = infer_root_system(row.get("主体类型", ""))
    if entry.root_system and subject_root:
        if entry.root_system == subject_root:
            score += 0.14
            reasons.append("subject_root_match")
        else:
            score -= 0.09
            reasons.append("subject_root_mismatch")

    context = " ".join(
        str(row.get(name, "") or "")
        for name in (
            "证据文本", "章节路径", "所属表格标题", "predicate_raw", "事实类型", "来源类型",
            "条件文本", "方法名称", "component_role", "process_type",
        )
    ).casefold()
    compact_context = normalize_key(context)

    for term in _CATEGORY_CONTEXT.get(entry.attribute_category, ()):
        if normalize_key(term) in compact_context:
            score += 0.035
            reasons.append(f"category_context:{term}")
            break

    for term in entry.context_positive:
        if normalize_key(term) in compact_context:
            score += 0.12
            reasons.append(f"positive_context:{term}")
    for term in entry.context_negative:
        if normalize_key(term) in compact_context:
            score -= 0.08
            reasons.append(f"negative_context:{term}")

    fact_type = str(row.get("事实类型", "") or "")
    if str(row.get("component_name", "") or "") or "组成" in fact_type:
        if entry.attribute_category == "组成/配方":
            score += 0.24
            reasons.append("composition_role_match")
        elif entry.attribute_category not in {"标识信息", "其他属性"}:
            score -= 0.08
    if str(row.get("process_id", "") or "") or "工艺" in fact_type:
        if entry.attribute_category in {"制备工艺", "工艺参数"}:
            score += 0.18
            reasons.append("process_role_match")
    if str(row.get("方法名称", "") or "") and entry.attribute_category == "测试/检测方法":
        score += 0.10
        reasons.append("method_context_match")

    unit_dimension = infer_unit_dimension(row.get("normalized_unit") or row.get("单位"))
    if unit_dimension and entry.unit_dimensions:
        if unit_dimension in entry.unit_dimensions:
            score += 0.09
            reasons.append("unit_dimension_match")
        else:
            score -= 0.13
            reasons.append("unit_dimension_mismatch")

    value_role = str(row.get("condition_metric_role", "") or "")
    if entry.allowed_value_roles and value_role:
        if any(role in value_role for role in entry.allowed_value_roles):
            score += 0.05
            reasons.append("value_role_match")

    score += min(max(entry.priority, 0), 100) / 2000.0
    return ContextScore(score=max(0.0, min(score, 1.50)), reasons=tuple(reasons))


__all__ = ["ContextScore", "infer_root_system", "infer_unit_dimension", "score_candidate"]
