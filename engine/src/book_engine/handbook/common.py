# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

FACT_COLUMNS = [
    "文档ID",
    "领域类型",
    "章节路径",
    "来源定位",
    "主体名称",
    "主体类型",
    "专业材料类别",
    "关系/属性名称",
    "尾实体/取值文本",
    "尾实体类型/属性类别",
    "数值",
    "范围下限",
    "范围上限",
    "单位",
    "取值角色",
    "条件文本",
    "方法名称",
    "证据文本",
    "置信度",
    "抽取来源",
]

ENTRY_COLUMNS = [
    "entry_id",
    "canonical_subject_name",
    "english_name",
    "entry_type",
    "entry_start_line",
    "entry_end_line",
    "title_raw",
    "title_normalized",
    "title_confidence",
]

PROP_CLASS = {
    "中文名": "身份信息",
    "英文名": "身份信息",
    "中文别名": "身份信息",
    "英文别名": "身份信息",
    "化学式": "身份信息",
    "分子式": "身份信息",
    "结构式": "身份信息",
    "CAS号": "身份信息",
    "相对分子质量": "基础信息",
    "相对分子量": "基础信息",
    "含氮量": "基础信息",
    "氧平衡": "性能参数",
    "密度": "基础物性",
    "相对密度": "基础物性",
    "熔点": "基础物性",
    "沸点": "基础物性",
    "闪点": "安全属性",
    "折射率": "基础物性",
    "溶解性": "化学手册属性",
    "外观": "化学手册属性",
    "毒性": "安全属性",
    "用途": "应用安全",
    "储存运输条件": "应用安全",
    "标准生成热": "热力学性质",
    "标准生成自由能": "热力学性质",
    "标准吉布斯自由能": "热力学性质",
    "燃烧热": "燃烧爆炸性能",
    "爆速": "燃烧爆炸性能",
    "爆热": "燃烧爆炸性能",
    "爆容": "燃烧爆炸性能",
    "爆压": "燃烧爆炸性能",
    "爆发点": "燃烧爆炸性能",
    "撞击感度": "感度性能",
    "冲击感度": "感度性能",
    "摩擦感度": "感度性能",
    "静电火花感度": "感度性能",
    "真空安定性": "安全属性",
}

def strip_cell(x: Any) -> str:
    if x is None:
        return ""
    s = str(x).replace("\ufeff", "")
    s = re.sub(r"[\u200b\u200c\u200d]", "", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()

def normalize_math_text(s: Any) -> str:
    """将常见 Markdown/LaTeX/OCR 公式文本还原为可解析的普通文本。"""
    s = strip_cell(s)
    s = s.replace("\u00a0", " ")
    replacements = {
        r"\mathrm": "",
        r"\mathbf": "",
        r"\mathit": "",
        r"\mathfrak": "",
        r"\pmb": "",
        r"\operatorname": "",
        r"\textcircled": "",
        r"\bf": "",
        r"\cdot": "·",
        r"\times": "×",
        r"\%": "%",
        r"\sim": "~",
        r"\circ": "°",
        r"\prime": "'",
        r"\dag": "†",
    }
    for k, v in replacements.items():
        s = s.replace(k, v)
    s = re.sub(r"\^\s*\{\s*\\?prime\s*\}", "'", s)
    s = re.sub(r"\^\s*\\?prime", "'", s)
    s = re.sub(r"_\s*\{\s*([^{}]+?)\s*\}", lambda m: "".join(m.group(1).split()), s)
    s = re.sub(r"\^\s*\{\s*([^{}]+?)\s*\}", lambda m: "".join(m.group(1).split()), s)
    s = s.replace("$", "")
    s = re.sub(r"[{}]", "", s)
    s = s.replace("\\", "")
    s = s.replace("（", "(").replace("）", ")")
    s = s.replace("：", ":").replace("；", ";").replace("，", ",")
    s = s.replace("−", "-").replace("－", "-").replace("—", "-")
    s = re.sub(r"(?<=[A-Za-z])\s+(?=[0-9A-Za-z])", "", s)
    s = re.sub(r"(?<=[0-9])\s+(?=[A-Za-z0-9])", "", s)
    s = re.sub(r"(\d)\s*,\s*(\d)", r"\1,\2", s)
    s = re.sub(r"\s+'\s*", "'", s)
    s = re.sub(r"(\d)\s*\.\s*(\d)", r"\1.\2", s)
    s = re.sub(r"(?<=\d)\s*(?:qC|9C|QC|℃|°C)", "℃", s)
    s = re.sub(r"\s+", " ", s).strip()
    # OCR 常见：公式中的 0 常为 O，但不要全局替换，只在元素串中局部交给 normalize_formula。
    return s

def normalize_formula(s: Any) -> str:
    s = normalize_math_text(s)
    # 清理公式 OCR/LaTeX 残留命令词，防止进入分子式值
    for _w in ("dot", "mathbb", "mathrm", "mathbf", "left", "right", "big", "tilde", "bar", "textrm", "displaystyle", "langle", "rangle", "Phi"):
        s = s.replace(_w, "")
    s = s.replace("'", "")
    s = re.sub(r"^(化学式|分子式)\s*[:：]?", "", s).strip()
    # 去掉尾随中文属性标签
    s = re.split(r"(相对分子质量|相对分子量|含氮量|氧平衡|CAS号|中文别名|英文别名)\s*[:：]?", s)[0].strip()
    # 公式中多余空格
    s = re.sub(r"\s+", "", s)
    # 常见 OCR 修复：只在元素上下文中把 0 改为 O
    # 常见 OCR 修复：公式中的元素 O 被识别为 0。
    # Python 正则不支持可变长后顾，分两步处理。
    s = re.sub(r"^0(?=\d|[A-Z]|$)", "O", s)
    s = re.sub(r"(?<=[A-Z])0(?=\d|[A-Z]|$)", "O", s)
    s = re.sub(r"(?<=[a-z])0(?=\d|[A-Z]|$)", "O", s)
    s = re.sub(r"(?<=\d)0(?=\d|[A-Z]|$)", "O", s)
    s = s.replace("·", "·")
    return s.strip(" ,.;；。")

def normalize_subject_name(s: Any) -> str:
    s = normalize_math_text(s)
    s = s.strip("# \t")
    s = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", s)
    s = re.sub(r"\b(?:mathfrak|mathrm|mathbf|left|right)\b", "", s, flags=re.I)
    s = re.sub(r"left\(([^)]*)right\)", r"(\1)", s, flags=re.I)
    s = re.sub(r"([A-Za-z])'\.?\s*(?=[\u4e00-\u9fff])", r"\1'-", s)
    s = re.sub(r"\s+", "", s)
    # 去掉英文方位标记残留，通常应属于英文名而不是中文主体
    s = re.sub(r"\^?[ompOMP]\-?$", "", s)
    s = s.strip(" ,.;；。")
    return s

def category_by_entry_id(entry_id: str) -> str:
    if entry_id.startswith("1"):
        return "无机物"
    if entry_id.startswith("2"):
        return "有机物"
    if entry_id.startswith("3"):
        return "高聚物（含混合物）"
    return "化合物"

def prop_class(prop: str) -> str:
    return PROP_CLASS.get(prop, "化学手册属性")

def make_fact(
    *,
    book_id: str,
    entry_id: str,
    subject: str,
    prop: str,
    value: str,
    evidence: str,
    source: str,
    subject_type: str = "化合物",
    material_category: str = "",
    unit: str = "",
    numeric: str = "",
    lower: str = "",
    upper: str = "",
    condition: str = "",
    method: str = "",
    confidence: str = "0.88",
) -> Dict[str, str]:
    material_category = material_category or category_by_entry_id(entry_id)
    val_role = "属性值"
    return {
        "文档ID": book_id,
        "领域类型": "含能材料及相关物手册",
        "章节路径": f"entry_{entry_id}>{subject}",
        "来源定位": f"entry_{entry_id}",
        "主体名称": subject,
        "主体类型": subject_type,
        "专业材料类别": material_category,
        "关系/属性名称": prop,
        "尾实体/取值文本": value,
        "尾实体类型/属性类别": prop_class(prop),
        "数值": numeric,
        "范围下限": lower,
        "范围上限": upper,
        "单位": unit,
        "取值角色": val_role,
        "条件文本": condition,
        "方法名称": method,
        "证据文本": evidence,
        "置信度": confidence,
        "抽取来源": source,
    }

UNIT_RE = r"(kJ/mol|kJ/kg|J/mol|J/kg|g/cm3|g/cm³|kg/m3|℃|°C|K|%|kg|g|mg|J|mJ|cm|mm|kPa|MPa|GPa|L/kg|m/s|kp|N)"

def parse_numeric_unit(value: str) -> Tuple[str, str, str, str]:
    """返回 numeric, lower, upper, unit。只做轻量解析，不破坏原取值文本。

    v2.1 修正：
    - 对“按生成 CO2 计 -73%”优先取带百分号的 -73，而不是 CO2 中的 2。
    - 对带单位的值优先取紧邻单位的数值。
    - 对范围值保留上下限。
    """
    v = normalize_math_text(value)
    if not v:
        return "", "", "", ""

    pct_matches = list(re.finditer(r"([<>≤≥]?)\s*([-+]?\s*\d+(?:\.\d+)?)\s*%", v))
    if pct_matches:
        m = pct_matches[-1]
        op, num = m.group(1), m.group(2).replace(" ", "")
        if op in (">", "≥"):
            return "", num, "", "%"
        if op in ("<", "≤"):
            return "", "", num, "%"
        return num, "", "", "%"

    range_re = re.compile(r"([<>≤≥]?)\s*([-+]?\s*\d+(?:\.\d+)?)\s*(?:~|～|至)\s*([-+]?\s*\d+(?:\.\d+)?)\s*(" + UNIT_RE + r")?")
    rms = list(range_re.finditer(v))
    if rms:
        m = rms[-1]
        unit = m.group(4) or ""
        return "", m.group(2).replace(" ", ""), m.group(3).replace(" ", ""), unit

    comp_matches = list(re.finditer(r"([<>≤≥])\s*([-+]?\s*\d+(?:\.\d+)?)\s*(" + UNIT_RE + r")?", v))
    if comp_matches:
        m = comp_matches[-1]
        unit = m.group(3) or ""
        if m.group(1) in (">", "≥"):
            return "", m.group(2).replace(" ", ""), "", unit
        return "", "", m.group(2).replace(" ", ""), unit

    unit_matches = list(re.finditer(r"([-+]?\s*\d+(?:\.\d+)?)\s*(" + UNIT_RE + r")", v))
    if unit_matches:
        m = unit_matches[-1]
        return m.group(1).replace(" ", ""), "", "", m.group(2)

    signed = list(re.finditer(r"(?<![A-Za-z])[-+]\s*\d+(?:\.\d+)?", v))
    if signed:
        num = signed[-1].group(0).replace(" ", "")
        return num, "", "", ""
    nums = list(re.finditer(r"(?<![A-Za-z])\d+(?:\.\d+)?(?![A-Za-z])", v))
    if not nums:
        return "", "", "", ""
    return nums[0].group(0), "", "", ""
