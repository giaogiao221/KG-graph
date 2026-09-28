from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable, List, Sequence, Tuple

from book_engine.core.schemas import HeaderPath, HeaderTree, TableGrid

_NUMERIC_RE = re.compile(
    r"^[\s<>≤≥≈~～±+\-−]?(?:\d+(?:[.,]\d+)?|[.,]\d+)(?:\s*(?:%|‰|[a-zA-Z°℃·/\^\-0-9]+))?$"
)
_RANGE_RE = re.compile(r"\d\s*(?:[-~～—至]|\.{2,})\s*\d")
_FORMULA_RE = re.compile(r"^(?:[A-Z][a-z]?\d*){2,}$")
_SHORT_CODE_RE = re.compile(r"^[A-Z][A-Z0-9\-_/]{1,15}$", re.IGNORECASE)
_ANALYTICAL_LEAF_RE = re.compile(
    r"(?:DSC|DTG\s*[-–—]?\s*TG|TG|结果|[/\s])\s*(?:Texon|To|Td|Tg|Tp|Tm|T)"
    r"(?:\s*/\s*(?:℃C|℃|°C{1,2}|C|K))?\s*$",
    re.IGNORECASE,
)
_MOLECULAR_SYMBOL_RE = re.compile(r"^(?:Mw|Mn|Mz|Mw/Mn|Mw/M|Mn/Mw)$", re.IGNORECASE)

ENTITY_WORDS = {
    "材料", "名称", "样品", "试样", "配方", "编号", "型号", "代号", "炸药", "推进剂", "火药",
    "烟火药", "药剂", "化合物", "体系", "聚合物", "单体", "预聚物", "组分", "物质", "产品",
    "试验样品", "样品名称", "配方号", "材料名称", "炸药名称", "推进剂名称", "药剂名称",
}
IDENTIFIER_WORDS = {
    "编号", "序号", "代号", "型号", "批次", "cas", "cas号", "登记号", "样品号", "配方号",
}
PROPERTY_WORDS = {
    "性能", "属性", "项目", "指标", "密度", "相对密度", "理论密度", "装药密度", "熔点", "沸点",
    "分解温度", "爆速", "爆压", "爆热", "爆温", "爆容", "燃速", "比冲", "压力指数", "感度",
    "撞击感度", "摩擦感度", "火花感度", "热感度", "临界直径", "威力", "猛度", "生成热",
    "生成焓", "燃烧热", "氧平衡", "分子量", "相对分子质量", "含量", "纯度", "黏度", "粘度",
    "强度", "拉伸强度", "伸长率", "模量", "硬度", "粒径", "粒度", "比表面积", "含水量",
    "吸湿性", "热值", "产气量", "发光强度", "燃烧时间", "释能时间", "释能功率", "峰顶温度",
    "玻璃化温度", "活化能", "速率常数", "误差", "计算值", "实验值", "实测值", "预测值",
}
CONDITION_WORDS = {
    "条件", "温度", "试验温度", "实验温度", "环境温度", "压力", "湿度", "时间", "老化时间",
    "加热速率", "升温速率", "加载速率", "应变率", "频率", "粒径", "粒度", "装药密度", "试验密度",
    "样品质量", "试样质量", "落锤质量", "落高", "浓度", "固含量", "转速", "搅拌速度", "气氛",
    "介质", "溶剂", "真空度", "保温时间", "干燥温度", "循环次数", "配比", "质量比", "摩尔比",
    "在", "下", "时", "不同温度", "不同压力", "不同时间", "不同粒径", "不同密度",
}
METHOD_WORDS = {
    "方法", "试验方法", "测试方法", "检验方法", "测定方法", "分析方法", "仪器", "设备", "标准",
    "模型", "算法", "计算方法", "表征方法", "检测方法", "测试仪器", "装置", "程序",
}
COMPOSITION_WORDS = {
    "组成", "组分", "配方组成", "质量分数", "质量百分数", "含量", "比例", "配比", "摩尔分数",
    "氧化剂", "燃料", "粘合剂", "黏合剂", "增塑剂", "固化剂", "键合剂", "催化剂", "添加剂",
    "安定剂", "燃速调节剂", "金属燃料", "填料", "链段", "软链段", "硬链段",
}
GROUP_WORDS = {"类别", "类型", "分组", "等级", "用途", "应用领域", "状态", "结果", "判定", "级别"}
NOTE_WORDS = {"备注", "注", "说明", "现象", "特点", "优点", "缺点", "适用范围", "结论"}
COMPARISON_WORDS = {"对照", "基准", "实验值", "计算值", "预测值", "理论值", "误差", "偏差", "差值"}


def normalize_label(text: str) -> str:
    value = re.sub(r"\s+", "", (text or "").lower())
    value = value.replace("（", "(").replace("）", ")")
    return value


def join_labels(labels: Sequence[str]) -> str:
    return "/".join(label for label in labels if label)


def contains_any(text: str, words: Iterable[str]) -> bool:
    normalized = normalize_label(text)
    return any(normalize_label(word) in normalized for word in words)


def exact_or_contains(text: str, words: Iterable[str]) -> bool:
    normalized = normalize_label(text)
    normalized = re.sub(r"[/()\[\]{}%‰·:_\-]+", "", normalized)
    for word in words:
        w = normalize_label(word)
        if normalized == w or w in normalized:
            return True
    return False


def is_numeric_like(text: str) -> bool:
    value = re.sub(r"\s+", "", text or "")
    if not value:
        return False
    if _NUMERIC_RE.match(value):
        return True
    if _RANGE_RE.search(value) and len(value) <= 40:
        return True
    return bool(any(ch.isdigit() for ch in value) and len(value) <= 28)


def is_formula_or_code(text: str) -> bool:
    value = re.sub(r"\s+", "", text or "")
    if not value or len(value) > 32:
        return False
    if _FORMULA_RE.match(value):
        return True
    return bool(_SHORT_CODE_RE.match(value) and any(ch.isalpha() for ch in value))


def nonempty(values: Sequence[str]) -> List[str]:
    return [value for value in values if (value or "").strip()]


def numeric_ratio(values: Sequence[str]) -> float:
    vals = nonempty(values)
    if not vals:
        return 0.0
    return sum(is_numeric_like(value) for value in vals) / len(vals)


def unique_ratio(values: Sequence[str]) -> float:
    vals = nonempty(values)
    if not vals:
        return 0.0
    return len(set(vals)) / len(vals)


def text_ratio(values: Sequence[str]) -> float:
    vals = nonempty(values)
    if not vals:
        return 0.0
    return sum(not is_numeric_like(value) for value in vals) / len(vals)


def data_start_row(header_tree: HeaderTree) -> int:
    return max(header_tree.header_rows) + 1 if header_tree.header_rows else 0


def column_values(grid: TableGrid, header_tree: HeaderTree, column: int) -> List[str]:
    start = data_start_row(header_tree)
    return [grid.cells[row][column].normalized_text for row in range(start, grid.row_count)]


def row_values(grid: TableGrid, row: int, skip_columns: Sequence[int] = ()) -> List[str]:
    skip = set(skip_columns)
    return [
        grid.cells[row][column].normalized_text
        for column in range(grid.column_count)
        if column not in skip
    ]


def header_label(path: HeaderPath) -> str:
    return join_labels(path.labels)


def unit_dimension_hint(unit: str) -> str:
    value = normalize_label(unit)
    if not value:
        return ""
    if any(token in value for token in ("℃", "°c", "k")):
        return "temperature"
    if any(token in value for token in ("mpa", "gpa", "kpa", "pa")):
        return "pressure"
    if any(token in value for token in ("g/cm", "kg/m")):
        return "density"
    if any(token in value for token in ("mm/s", "cm/s")):
        return "rate"
    if any(token in value for token in ("m/s", "km/s")):
        return "velocity"
    if any(token in value for token in ("μm", "um", "nm", "mm")):
        return "length"
    if any(token in value for token in ("min", "ms", "ns", "μs", "us", "h", "d", "s")):
        return "time"
    if "%" in value or "‰" in value:
        return "fraction"
    return ""


def analytical_property_leaf(label: str) -> str:
    value = re.sub(r"\s+", " ", (label or "")).strip()
    if not value:
        return ""
    molecular = _MOLECULAR_SYMBOL_RE.fullmatch(value.replace(" ", ""))
    if molecular:
        return molecular.group(0)
    match = _ANALYTICAL_LEAF_RE.search(value)
    if not match:
        return ""
    symbol = re.search(r"(Texon|To|Td|Tg|Tp|Tm|T)(?:\s*/|\s*$)", match.group(0), re.IGNORECASE)
    return symbol.group(1) if symbol else ""


def lexical_scores(label: str) -> dict:
    analytical_leaf = analytical_property_leaf(label)
    return {
        "entity": 1.0 if exact_or_contains(label, ENTITY_WORDS) else 0.0,
        "identifier": 1.0 if exact_or_contains(label, IDENTIFIER_WORDS) else 0.0,
        "property": 1.0 if analytical_leaf or exact_or_contains(label, PROPERTY_WORDS) else 0.0,
        "condition": 1.0 if exact_or_contains(label, CONDITION_WORDS) else 0.0,
        "method": 1.0 if exact_or_contains(label, METHOD_WORDS) else 0.0,
        "composition": 1.0 if exact_or_contains(label, COMPOSITION_WORDS) else 0.0,
        "group": 0.0 if analytical_leaf else (1.0 if exact_or_contains(label, GROUP_WORDS) else 0.0),
        "note": 1.0 if exact_or_contains(label, NOTE_WORDS) else 0.0,
        "comparison": 1.0 if exact_or_contains(label, COMPARISON_WORDS) else 0.0,
    }


def looks_like_component_label(label: str, unit: str = "") -> bool:
    normalized = normalize_label(label)
    if not normalized:
        return False
    if exact_or_contains(label, COMPOSITION_WORDS):
        return True
    # Short chemical/component labels are common in formulation matrices.
    token = re.sub(r"[/()\[\]{}%‰·:_\-]+", "", normalized)
    if len(token) <= 12 and is_formula_or_code(token) and ("%" in label or "%" in unit or not exact_or_contains(label, PROPERTY_WORDS)):
        return True
    return False


def looks_like_material_value(text: str) -> bool:
    value = (text or "").strip()
    if not value or is_numeric_like(value):
        return False
    if len(value) > 80:
        return False
    if any(mark in value for mark in ("。", "；", ";")):
        return False
    if is_formula_or_code(value):
        return True
    # Chinese names, abbreviations and formulation identifiers.
    return bool(re.search(r"[\u4e00-\u9fffA-Za-z]", value))


def same_label_set(left: Sequence[str], right: Sequence[str]) -> bool:
    a = {normalize_label(value) for value in left if normalize_label(value)}
    b = {normalize_label(value) for value in right if normalize_label(value)}
    if len(a) < 2 or len(b) < 2:
        return False
    overlap = len(a & b) / max(len(a), len(b))
    return overlap >= 0.75
