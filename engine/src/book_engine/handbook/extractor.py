# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Tuple

from .common import (
    FACT_COLUMNS,
    make_fact,
    normalize_formula,
    normalize_math_text,
    parse_numeric_unit,
    prop_class,
)
from .entry_parser import ChemEntry

# 按优先级排序，长标签放前面，防止 “相对分子质量” 被 “质量” 截断
LABELS = [
    "中文别名",
    "英文别名",
    "相对分子质量",
    "相对分子量",
    "标准吉布斯自由能",
    "标准生成自由能",
    "标准生成热",
    "储存运输条件",
    "静电火花感度",
    "真空安定性",
    "撞击感度",
    "冲击感度",
    "摩擦感度",
    "分解温度",
    "化学式",
    "分子式",
    "结构式",
    "CAS号",
    "含氮量",
    "氧平衡",
    "相对密度",
    "密度",
    "熔点",
    "沸点",
    "闪点",
    "折射率",
    "溶解性",
    "外观",
    "毒性",
    "用途",
    "燃烧热",
    "爆速",
    "爆热",
    "爆容",
    "爆压",
    "爆发点",
]

SECTION_LABELS = [
    "理化性质",
    "热化学性质",
    "燃烧爆炸性能",
    "感度性能",
    "用途",
    "毒性",
    "储存运输条件",
    "储运",
]

def _norm_block_text(text: str) -> str:
    t = normalize_math_text(text)
    # 图片占位不参与属性取值
    t = re.sub(r"!\[\]\([^)]+\)", " ", t)
    # section 标记统一
    t = t.replace("【", "[").replace("】", "]")
    t = re.sub(r"\s+", " ", t)
    return t.strip()

def _label_pattern() -> re.Pattern:
    parts = sorted([re.escape(x) for x in LABELS], key=len, reverse=True)
    # 标签可能有 [] 包裹，也可能直接出现
    return re.compile(r"(?P<label>" + "|".join(parts) + r")\s*[:：]?", flags=re.I)

LABEL_RE = _label_pattern()

def _section_pattern() -> re.Pattern:
    parts = sorted([re.escape(x) for x in SECTION_LABELS], key=len, reverse=True)
    return re.compile(r"\[(?P<section>" + "|".join(parts) + r")\]")

SECTION_RE = _section_pattern()

def iter_labeled_values(text: str) -> Iterable[Tuple[str, str, str]]:
    """从归一化后的条目正文中抽取 label → value，证据为 label+value。"""
    t = _norm_block_text(text)
    # 将 [用途]本品... 这种也视为标签
    matches = []
    for m in LABEL_RE.finditer(t):
        matches.append(("label", m.start(), m.end(), m.group("label")))
    for m in SECTION_RE.finditer(t):
        sec = m.group("section")
        if sec in ("用途", "毒性", "储存运输条件", "储运"):
            label = "储存运输条件" if sec == "储运" else sec
        else:
            label = "__SECTION_BOUNDARY__"
        matches.append(("section", m.start(), m.end(), label))
    matches.sort(key=lambda x: x[1])
    for idx, (_, start, end, label) in enumerate(matches):
        if label == "__SECTION_BOUNDARY__":
            continue
        next_start = matches[idx + 1][1] if idx + 1 < len(matches) else len(t)
        raw_value = t[end:next_start].strip(" :：，,;；。")
        if not raw_value:
            continue
        # 对普通短属性，遇句号可截断，避免吞入后续正文；热化学/用途等保留长文本
        if label not in ("标准生成热", "标准生成自由能", "标准吉布斯自由能", "用途", "毒性", "储存运输条件"):
            raw_value = re.split(r"[。；;]", raw_value)[0].strip(" ，,")
        evidence = f"{label}：{raw_value}"
        yield label, raw_value, evidence

def split_multi_property_value(label: str, value: str) -> List[Tuple[str, str]]:
    """修复 相对分子质量：496.19含氮量：45% 这种粘连。"""
    pairs = [(label, value)]
    # 如果 value 内部还有其他标签，重新切
    internal = list(LABEL_RE.finditer(value))
    if not internal:
        return pairs
    out: List[Tuple[str, str]] = []
    # 当前标签的第一段
    first_val = value[:internal[0].start()].strip(" :：，,;；。")
    if first_val:
        out.append((label, first_val))
    for i, m in enumerate(internal):
        lab = m.group("label")
        end = internal[i + 1].start() if i + 1 < len(internal) else len(value)
        val = value[m.end():end].strip(" :：，,;；。")
        if val:
            out.append((lab, val))
    return out or pairs

def normalize_property_name(label: str) -> str:
    if label == "相对分子量":
        return "相对分子质量"
    if label == "分子式":
        return "化学式"
    if label == "储运":
        return "储存运输条件"
    return label

def _clean_formula_tail(v: str) -> str:
    s = normalize_formula(v)
    # 去掉结构式/图形残片：如 (C5H10O2S2)n(CH-CH-O-...)
    s = re.sub(r"(?i)(\)n|\)x|\)m|n|x|m)\(.*$", r"\1", s)
    # 简单分子式后若继续粘入结构式，保留最前面的化学式段。
    m = re.match(r"^\(?((?:[A-Z][a-z]?\d*(?:\.\d+)?)+)\)?([nmx])?", s)
    if m:
        prefix = m.group(0)
        rest = s[len(prefix):]
        if rest and re.search(r"(?:HC|CH|HN|NH|NO2|O2N|[A-Z]-|^-)", rest):
            # 若后面是结构键片段，且 prefix 末尾有无数字的元素符号粘入，则回退到最后一个带数字的元素处。
            if rest.startswith("-") or rest.startswith("="):
                prefix = re.sub(r"(?<=\d)(?:[A-Z][a-z]?)+$", "", prefix)
            s = prefix
    if len(s) > 80 and s.count("-") >= 3:
        cut = re.search(r"(?:HC|CH|HN|NH|NO2|O2N)-", s)
        if cut and cut.start() > 5:
            s = s[:cut.start()]
    return s.strip(" ,.;；。")

def clean_value_for_property(prop: str, value: str) -> str:
    v = normalize_math_text(value)
    v = v.strip(" :：，,;；。")
    if prop in ("化学式", "结构式"):
        return _clean_formula_tail(v)
    if prop == "CAS号":
        m = re.search(r"\d{2,7}-\d{2}-\d", v)
        return m.group(0) if m else v
    if prop in ("中文别名", "英文别名"):
        return v.strip()
    # 去掉残留的小节边界
    v = re.split(r"\[(理化性质|热化学性质|燃烧爆炸性能|感度性能)\]", v)[0].strip(" :：，,;；。")
    v = re.sub(r"(\d)\s*%", r"\1%", v)
    v = re.sub(r"(\d)\s*(℃|°C|K|kJ/mol|kJ/kg|J/mol|J/kg|g/cm3|g/cm³|L/kg|m/s|MPa|GPa|kPa|J|mJ|kg|g|mg|kp|N)", r"\1 \2", v)
    return v.strip()



_MULTIVALUE_NUMERIC_LABELS = {
    "标准生成热", "标准生成自由能", "标准吉布斯自由能", "密度", "熔点", "沸点",
    "爆速", "爆热", "爆压", "爆发点", "燃烧热",
}
_MULTIVALUE_MEASUREMENT_RE = re.compile(
    r"(?:[（(]?\s*(?P<state>s|l|g|aq)\s*[）)]?\s*)?"
    r"(?P<value>[-+−]?\s*\d+(?:\s*[.]\s*\d+)?)\s*"
    r"(?P<unit>kJ/mol|kJ/kg|J/mol|J/kg|g/cm3|g/cm³|kg/m3|kg/m³|℃|°C|K|m/s|km/s|MPa|GPa|kPa)"
    r"(?:\s*\[\s*(?P<ref>\d{1,4})\s*\])?",
    re.I,
)
_STATE_LABEL = {"s": "固态", "l": "液态", "g": "气态", "aq": "水溶液"}

def iter_multivalue_measurements(label: str, value: str) -> List[Tuple[str, str]]:
    if label not in _MULTIVALUE_NUMERIC_LABELS:
        return []
    normalized = normalize_math_text(value)
    matches = list(_MULTIVALUE_MEASUREMENT_RE.finditer(normalized))
    if len(matches) < 2:
        return []
    result: List[Tuple[str, str]] = []
    for match in matches:
        number = re.sub(r"\s+", "", match.group("value")).replace("−", "-")
        unit = match.group("unit")
        conditions: List[str] = []
        state = (match.group("state") or "").lower()
        if state:
            conditions.append(f"物态={_STATE_LABEL.get(state, state)}")
        if match.group("ref"):
            conditions.append(f"来源=[{match.group('ref')}]")
        result.append((f"{number} {unit}", "；".join(conditions)))
    return result

def extract_facts_from_entry(entry: ChemEntry, *, book_id: str, book_title: str = "") -> List[Dict[str, str]]:
    subject = entry.canonical_subject_name
    facts: List[Dict[str, str]] = []
    if not subject:
        return facts

    # 主体身份事实，确保 entry 主体本身有中文名/英文名
    facts.append(make_fact(
        book_id=book_id,
        entry_id=entry.entry_id,
        subject=subject,
        prop="中文名",
        value=subject,
        evidence=f"{entry.entry_id} {entry.title_raw}",
        source="chem_handbook_v2.entry_title",
        confidence=f"{entry.title_confidence:.2f}",
    ))
    if entry.english_name:
        facts.append(make_fact(
            book_id=book_id,
            entry_id=entry.entry_id,
            subject=subject,
            prop="英文名",
            value=entry.english_name,
            evidence=f"{entry.entry_id} {entry.title_raw}",
            source="chem_handbook_v2.entry_title",
            confidence=f"{entry.title_confidence:.2f}",
        ))

    text = entry.block_text
    for label, value, evidence in iter_labeled_values(text):
        for prop0, val0 in split_multi_property_value(label, value):
            prop = normalize_property_name(prop0)
            value_clean = clean_value_for_property(prop, val0)
            if not value_clean or value_clean.lower() in {"null", "<null>", "nan"}:
                continue
            multivalue = iter_multivalue_measurements(prop, value_clean)
            if multivalue:
                for split_value, split_condition in multivalue:
                    numeric, lower, upper, unit = parse_numeric_unit(split_value)
                    facts.append(make_fact(
                        book_id=book_id,
                        entry_id=entry.entry_id,
                        subject=subject,
                        prop=prop,
                        value=split_value,
                        evidence=evidence,
                        source="chem_handbook_v2.multivalue_rule",
                        numeric=numeric,
                        lower=lower,
                        upper=upper,
                        unit=unit,
                        condition=split_condition,
                        confidence="0.90",
                    ))
                continue
            numeric = lower = upper = unit = ""
            # 化学式/结构式/CAS/名称类不走数值解析，避免 C8N16O11 → 8 这类错误
            if prop not in ("化学式", "结构式", "中文名", "英文名", "中文别名", "英文别名", "CAS号"):
                numeric, lower, upper, unit = parse_numeric_unit(value_clean)
            facts.append(make_fact(
                book_id=book_id,
                entry_id=entry.entry_id,
                subject=subject,
                prop=prop,
                value=value_clean,
                evidence=evidence,
                source="chem_handbook_v2.rule",
                numeric=numeric,
                lower=lower,
                upper=upper,
                unit=unit,
                confidence="0.87",
            ))
    return facts

def dedup_facts(facts: List[Dict[str, str]]) -> List[Dict[str, str]]:
    seen: Dict[Tuple[str, str, str, str, str, str], Dict[str, str]] = {}
    for f in facts:
        key = (
            f.get("来源定位", ""),
            f.get("主体名称", "").lower().replace(" ", ""),
            f.get("关系/属性名称", "").lower().replace(" ", ""),
            f.get("尾实体/取值文本", "").lower().replace(" ", ""),
            f.get("单位", ""),
            f.get("条件文本", ""),
        )
        prev = seen.get(key)
        if prev is None:
            seen[key] = f
            continue
        # 保留证据更长/字段更完整者
        score_prev = sum(1 for v in prev.values() if v) + len(prev.get("证据文本", ""))
        score_new = sum(1 for v in f.values() if v) + len(f.get("证据文本", ""))
        if score_new > score_prev:
            seen[key] = f
    return list(seen.values())
