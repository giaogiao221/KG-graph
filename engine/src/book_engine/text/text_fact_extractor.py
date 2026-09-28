from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace
from typing import List, Mapping, Sequence

from book_engine.core.schemas import ConditionAtom
from book_engine.document.block_segmenter import TextBlock
from book_engine.routing.heading_subject_resolver import is_entity_surface, strip_heading_number
from book_engine.routing.subject_name_normalizer import normalize_subject_name
from book_engine.routing.subject_type_inferer import infer_subject_type
from book_engine.routing.text_subject_anchor_resolver import TextSubjectAnchor
from book_engine.tables.value_parser import parse_value
from book_engine.text.evidence_self_containment import (
    compact_ocr_numeric_text,
    is_deictic_subject,
    iter_clauses,
)
from book_engine.text.text_owner_value_binding import bind_numeric_facts

_LABEL_RE = re.compile(r"^\s*(?:[-*•]\s*)?(?P<label>[^：:\n]{1,36})\s*[：:]\s*(?P<value>.+?)\s*$")
_KNOWN_LABELS = (
    "中文名称", "英文名称", "中文别称", "英文别称", "名称", "别名", "分子式", "化学式", "分子量",
    "相对分子质量", "CAS登记号", "CAS号", "编号", "外观", "状态", "密度", "熔点", "沸点", "闪点",
    "自燃点", "爆发点", "分解温度", "峰顶温度", "爆速", "爆压", "爆热", "燃速", "比冲", "氧平衡",
    "生成热", "生成焓", "纯度", "含量", "粒径", "粒度", "黏度", "粘度", "溶解性", "用途", "应用",
    "包装", "储存", "运输", "毒性", "防护", "制备方法", "合成方法", "检验方法", "组成", "组分", "配方组成",
)
_KNOWN_LABEL_PATTERN = "|".join(sorted((re.escape(item) for item in _KNOWN_LABELS), key=len, reverse=True))
_INLINE_KNOWN_FIELD_RE = re.compile(
    rf"(?P<label>{_KNOWN_LABEL_PATTERN})\s*[：:]\s*(?P<value>.*?)(?=\s*(?:{_KNOWN_LABEL_PATTERN})\s*[：:]|$)",
    re.S,
)
_UNIT_IN_LABEL_RE = re.compile(
    r"(?:[/（(]\s*(?P<unit>%|‰|℃|°C|K|Pa|kPa|MPa|GPa|g/cm(?:3|³)|kg/m(?:3|³)|V|mV|kV|A|mA|"
    r"nm|μm|um|mm|cm|m|ns|μs|us|ms|s|min|h|d|Hz|rpm|K/min|℃/min|mm/s|cm/s|m/s|km/s|J/g|"
    r"kJ/kg|kJ/mol)\s*[）)]?)$"
)
_NUMERIC_PROPERTY_RE = re.compile(
    r"(?:密度|熔点|沸点|闪点|自燃点|爆发点|分解温度|峰顶温度|爆速|爆压|爆热|燃速|比冲|"
    r"氧平衡|生成热|生成焓|分子量|相对分子质量|纯度|含量|粒径|粒度|黏度|粘度|感度|压力指数|释能时间|释能功率)",
    re.I,
)
_NUMERIC_SENTENCE_RE = re.compile(
    r"(?P<property>密度|熔点|沸点|闪点|自燃点|爆发点|分解温度|峰顶温度|爆速|爆压|爆热|燃速|比冲|"
    r"氧平衡|生成热|生成焓|分子量|相对分子质量|纯度|含量|粒径|粒度|黏度|粘度|压力指数|释能时间|释能功率)"
    r"\s*(?:为|是|约为|约|达到|可达|范围为|通常为|一般为)?\s*"
    r"(?P<value>(?:≥|≤|>|<|约|不小于|不大于)?\s*[-+−]?\d+(?:[.,]\d+)?(?:\s*(?:~|～|—|–|至|到|±)\s*[-+−]?\d+(?:[.,]\d+)?)?\s*"
    r"(?:%|‰|℃|°C|K|Pa|kPa|MPa|GPa|g/cm(?:3|³)|kg/m(?:3|³)|V|mV|kV|A|mA|nm|μm|um|mm|cm|m|ns|μs|us|ms|s|min|h|d|Hz|rpm|K/min|℃/min|mm/s|cm/s|m/s|km/s|J/g|kJ/kg|kJ/mol)?)",
    re.I,
)
_SECTION_PROPERTY_MAP = {
    "物理性质": "物理性质", "化学性质": "化学性质", "理化性质": "理化性质",
    "制备方法": "制备方法", "合成方法": "合成方法", "储存运输和应用": "储存运输和应用",
    "储存与运输": "储存与运输", "储存": "储存要求", "运输": "运输要求",
    "毒性与防护": "毒性与防护", "用途": "用途", "应用": "应用", "包装": "包装",
}
_SKIP_LABELS = {"注", "说明", "备注", "参考文献", "思考题", "图", "表", "来源", "小知识"}
_TOC_PAGE_LINE_RE = re.compile(
    r"^\s*(?:(?:第?\d+|[一二三四五六七八九十]+)\s*[章节篇部])?.{1,80}?"
    r"(?:[.．·…]{2,}|[：:]\s*)?\s*\d{1,4}\s*$"
)
_DISCOURSE_PREFIX_RE = re.compile(
    r"^(?:许多资料报道|资料报道|文献报道|研究表明|结果表明|可以看出|由此可见|此外|同时|其中|而|但)"
)
_CLASSIFICATION_RE = re.compile(
    r"(?P<owner>[A-Za-z0-9+._/\-（）()\u4e00-\u9fff]{2,50})\s*(?:可|通常|一般)?(?:分为|分成|分作)\s*(?P<value>[^。；;]{2,240})"
)
_APPLICATION_RE = re.compile(
    r"(?P<owner>[A-Za-z0-9+._/\-（）()\u4e00-\u9fff]{2,50})\s*(?:可|能|能够|常)?(?:用于|用作|作为)\s*(?P<value>[^。；;，,]{2,160})"
)
_EFFECT_RE = re.compile(
    r"(?P<owner>[A-Za-z0-9+._/\-（）()\u4e00-\u9fff]{2,50})\s*(?:可|能|能够|会|可以)?"
    r"(?P<action>提高|降低|促进|抑制|改善|增加|减小|缩短|延长|增强|削弱)\s*(?P<object>[^。；;，,]{2,120})"
)
_METHOD_REL_RE = re.compile(
    r"(?P<owner>[A-Za-z0-9+._/\-（）()\u4e00-\u9fff]{2,50})\s*(?:可|通常|一般)?(?:采用|使用|利用)\s*"
    r"(?P<value>[^。；;，,]{2,100}?(?:法|方法|技术|装置|仪器))\s*(?:测定|测试|检测|制备|处理|加工|评价)?"
)
_EQUIPMENT_ENTITY_RE = re.compile(r"(?:机|器|室|炉|泵|罐|釜|磨|筒|容器|箱|槽|塔|床|喷管|系统|装置)$")
_LOCAL_MATERIAL_ENTITY_RE = re.compile(r"(?:粉|金属|氧化物|化合物|火药|推进剂|炸药|药剂|材料|物质|聚合物|预聚物|共聚物|均聚物|橡胶|树脂|粘合剂|黏合剂|体系)$")
_CHEMICAL_ELEMENT_NAMES = {
    "氢", "氦", "锂", "铍", "硼", "碳", "氮", "氧", "氟", "氖", "钠", "镁", "铝", "硅", "磷", "硫", "氯", "氩",
    "钾", "钙", "钪", "钛", "钒", "铬", "锰", "铁", "钴", "镍", "铜", "锌", "镓", "锗", "砷", "硒", "溴", "氪",
    "铷", "锶", "钇", "锆", "铌", "钼", "锝", "钌", "铑", "钯", "银", "镉", "铟", "锡", "锑", "碲", "碘", "氙",
    "铯", "钡", "镧", "铈", "镨", "钕", "钷", "钐", "铕", "钆", "铽", "镝", "钬", "铒", "铥", "镱", "镥",
    "铪", "钽", "钨", "铼", "锇", "铱", "铂", "金", "汞", "铊", "铅", "铋", "钋", "砹", "氡", "镭", "钍", "铀",
}
_SAFE_OWNERLESS_SOURCES = {
    "identity_field", "nearest_entity_heading_anchor", "same_heading_identity_inheritance",
    "bounded_heading_inheritance", "explicit_process_subject_span", "explicit_process_final_product_span",
    "explicit_process_local_entity_span", "explicit_local_fact_owner", "multi_heading_unique_local_mention",
    "closed_set_subject_llm",
}


def _looks_like_toc_page_line(text: str) -> bool:
    value = re.sub(r"\s+", " ", text or "").strip()
    if not value or not _TOC_PAGE_LINE_RE.fullmatch(value):
        return False
    return bool(re.search(r"(?:章|节|篇|部|目录|[.．·…]{2,}|[：:])", value))


@dataclass
class TextFactRecord:
    record_id: str
    block_id: str
    subject: str
    subject_type: str
    property_name: str
    value_text: str
    unit: str = ""
    value_num: float | None = None
    lower_bound: float | None = None
    upper_bound: float | None = None
    comparator: str = ""
    normalized_value_text: str = ""
    conditions: List[ConditionAtom] = field(default_factory=list)
    method: str = ""
    relation_kind: str = "attribute"
    source_type: str = "text"
    heading_path: List[str] = field(default_factory=list)
    line_start: int = 0
    line_end: int = 0
    evidence: str = ""
    confidence: float = 0.0
    record_status: str = "candidate"
    unresolved_reasons: List[str] = field(default_factory=list)
    process_id: str = ""
    process_name: str = ""
    process_type: str = ""
    step_id: str = ""
    step_index: int | None = None
    step_label: str = ""
    step_action: str = ""
    step_object: str = ""
    step_condition_text: str = ""
    step_result_text: str = ""
    previous_step_id: str = ""
    next_step_id: str = ""
    anchor_source: str = ""
    fact_clause: str = ""
    owner_evidence: str = ""
    owner_source: str = ""


def _normalize_property(label: str) -> tuple[str, str]:
    value = re.sub(
        r"^\s*(?:[（(]?[一二三四五六七八九十0-9]+[）)]?[、.．]?|\d+(?:[.．]\d+)*)\s*",
        "",
        label or "",
    ).strip(" ：:、")
    unit = ""
    match = _UNIT_IN_LABEL_RE.search(value)
    if match:
        unit = match.group("unit") or ""
        value = value[:match.start()].strip(" /（(")
    value = re.sub(r"\s+", "", value)
    return value, unit


def _valid_free_label(label: str) -> bool:
    value = re.sub(r"\s+", "", label or "")
    if not value or len(value) > 24:
        return False
    if value in _KNOWN_LABELS:
        return True
    if re.search(r"(?:有两种|有三种|如下|包括|分别|例如|即为|可以|能够|通常|一般|其中|采用|利用|得到|生成|代入|计算|已知|示例)", value):
        return False
    if re.search(r"[。；;，,!?！？=$]", value):
        return False
    property_hint = re.search(
        r"(?:名称|别称|分子式|化学式|分子量|质量|编号|外观|状态|密度|熔点|沸点|闪点|温度|爆速|爆压|爆热|燃速|比冲|感度|氧平衡|生成热|生成焓|纯度|含量|粒径|粒度|黏度|粘度|溶解|用途|应用|包装|储存|运输|毒性|防护|方法|组成|组分)",
        value,
    )
    return bool(property_hint and re.fullmatch(r"[A-Za-z0-9_./（）()\-\u4e00-\u9fff]{1,24}", value))


def _iter_labeled_fields(line: str):
    known = list(_INLINE_KNOWN_FIELD_RE.finditer(line))
    if known:
        for match in known:
            yield match.group("label"), match.group("value").strip()
        return
    match = _LABEL_RE.match(line)
    if match and _valid_free_label(match.group("label")):
        yield match.group("label"), match.group("value").strip()


def _make_record(
    block: TextBlock,
    anchor: TextSubjectAnchor,
    property_name: str,
    value_text: str,
    *,
    unit_hint: str = "",
    source_type: str,
    confidence: float,
    relation_kind: str = "attribute",
    subject_override: str = "",
    fact_clause: str = "",
    owner_evidence: str = "",
    owner_source: str = "",
) -> TextFactRecord:
    effective_anchor = anchor
    if subject_override and subject_override != anchor.subject:
        subject_type, _, type_reasons = infer_subject_type(subject_override)
        effective_anchor = replace(
            anchor,
            subject=subject_override,
            subject_type=subject_type,
            source=owner_source or "explicit_composition_owner",
            confidence=max(anchor.confidence, 0.90),
            status="confirmed",
            reasons=list(anchor.reasons) + ["composition_local_owner_override"] + list(type_reasons),
        )
    cleaned_value = compact_ocr_numeric_text(value_text) if _NUMERIC_PROPERTY_RE.search(property_name) else value_text.strip()
    parsed = parse_value(cleaned_value, unit_hint) if _NUMERIC_PROPERTY_RE.search(property_name) else None
    digest = hashlib.sha1(
        f"{block.block_id}|{effective_anchor.subject}|{property_name}|{cleaned_value}|{source_type}".encode("utf-8")
    ).hexdigest()[:20]
    status = "ready" if effective_anchor.status == "confirmed" and confidence >= 0.74 else "candidate"
    reasons: List[str] = []
    if not effective_anchor.subject:
        status = "unresolved"
        reasons.append("missing_subject_anchor")
    if not property_name or not cleaned_value:
        status = "unresolved"
        reasons.append("missing_property_or_value")
    return TextFactRecord(
        record_id=f"txt:{digest}", block_id=block.block_id, subject=effective_anchor.subject,
        subject_type=effective_anchor.subject_type, property_name=property_name, value_text=cleaned_value,
        unit=(parsed.unit if parsed else unit_hint), value_num=(parsed.value_num if parsed else None),
        lower_bound=(parsed.lower_bound if parsed else None), upper_bound=(parsed.upper_bound if parsed else None),
        comparator=(parsed.comparator if parsed else ""), normalized_value_text=(parsed.normalized_text if parsed else cleaned_value),
        relation_kind=relation_kind, source_type=source_type, heading_path=list(block.heading_path),
        line_start=block.line_start, line_end=block.line_end, evidence=block.text,
        confidence=min(confidence, effective_anchor.confidence if effective_anchor.confidence else confidence),
        record_status=status, unresolved_reasons=reasons, anchor_source=effective_anchor.source,
        fact_clause=fact_clause or cleaned_value, owner_evidence=owner_evidence, owner_source=owner_source,
    )


def _clean_owner_candidate(value: str) -> str:
    candidate = normalize_subject_name(value or "").canonical_name
    candidate = _DISCOURSE_PREFIX_RE.sub("", candidate).strip()
    candidate = re.sub(r"^\s*(?:[（(]?[一二三四五六七八九十0-9]+[）)）、.．]\s*)", "", candidate)
    candidate = re.sub(r"(?:一般|通常|主要|基本)?(?:是|为)$", "", candidate).strip(" 的，,。；;：:")
    candidate = re.split(r"[。；;，,]", candidate)[-1].strip()
    return candidate


def _is_explicit_entity(value: str) -> bool:
    candidate = _clean_owner_candidate(value)
    if not candidate or is_deictic_subject(candidate):
        return False
    return (
        is_entity_surface(candidate)
        or bool(_EQUIPMENT_ENTITY_RE.search(candidate))
        or bool(_LOCAL_MATERIAL_ENTITY_RE.search(candidate))
        or candidate in _CHEMICAL_ELEMENT_NAMES
    )


def _safe_ownerless_anchor(anchor: TextSubjectAnchor) -> bool:
    return bool(
        anchor.subject and not is_deictic_subject(anchor.subject)
        and not re.search(r"(?:和|与|及|、)", anchor.subject)
        and anchor.source in _SAFE_OWNERLESS_SOURCES
    )


def _composition_candidates(clause: str, anchor: TextSubjectAnchor):
    """Yield clause-local composition candidates, including unsafe ones for audit.

    Safety is decided by the evidence self-containment gate.  Emitting an
    incomplete candidate here preserves the original evidence in the audit
    sidecar without releasing it to the graph.
    """
    for relation in re.finditer(r"组成|构成", clause or ""):
        relation_start = relation.start()
        search_start = max(0, relation_start - 280)
        by_index = clause.rfind("由", search_start, relation_start)
        if by_index < 0 or clause[by_index:by_index + 2] == "由于":
            continue
        components = clause[by_index + 1:relation_start].strip(" 的，,：:")
        value_text = f"由{components}{relation.group(0)}"

        prefix = clause[:by_index].strip(" ，,。；;：:")
        prefix = re.split(r"[，,]", prefix)[-1].strip()
        prefix = _DISCOURSE_PREFIX_RE.sub("", prefix).strip()
        raw_subject = _clean_owner_candidate(prefix)
        subject = raw_subject if _is_explicit_entity(raw_subject) else ""
        owner_source = "explicit_composition_owner"

        if not subject and (not prefix or is_deictic_subject(raw_subject) or re.fullmatch(r"(?:主要|一般|通常|基本)?(?:是)?", prefix)):
            if _safe_ownerless_anchor(anchor):
                subject = anchor.subject
                owner_source = "ownerless_composition_safe_anchor"
        if not subject:
            continue
        yield subject, value_text, clause, owner_source


def _heading_entity_override(block: TextBlock) -> str:
    title = strip_heading_number((block.heading_title or "").strip())
    if not title:
        return ""
    from book_engine.routing.heading_subject_resolver import classify_heading_subject
    parsed = classify_heading_subject(title)
    if parsed.is_entity and parsed.entity:
        return parsed.entity
    return _clean_owner_candidate(title) if _is_explicit_entity(title) else ""


def _iter_high_recall_relations(block: TextBlock, anchor: TextSubjectAnchor):
    for _, _, clause in iter_clauses(block.text):
        for pattern, property_name, source_type in (
            (_CLASSIFICATION_RE, "分类", "text_classification_relation"),
            (_APPLICATION_RE, "用途", "text_application_relation"),
            (_METHOD_REL_RE, "方法", "text_method_relation"),
        ):
            for match in pattern.finditer(clause):
                owner = _clean_owner_candidate(match.group("owner"))
                value = match.group("value").strip(" ，,。；;：:")
                if not _is_explicit_entity(owner) or len(value) < 2:
                    continue
                yield _make_record(
                    block, anchor, property_name, value, source_type=source_type, confidence=0.68,
                    subject_override=owner, fact_clause=clause, owner_evidence=match.group(0),
                    owner_source="explicit_relation_owner",
                )
        for match in _EFFECT_RE.finditer(clause):
            owner = _clean_owner_candidate(match.group("owner"))
            if not _is_explicit_entity(owner):
                continue
            value = f"{match.group('action')}{match.group('object').strip()}"
            yield _make_record(
                block, anchor, "作用影响", value, source_type="text_effect_relation", confidence=0.68,
                subject_override=owner, fact_clause=clause, owner_evidence=match.group(0),
                owner_source="explicit_relation_owner",
            )


def extract_text_facts(
    blocks: Sequence[TextBlock],
    anchors_by_block: Mapping[str, TextSubjectAnchor],
) -> List[TextFactRecord]:
    records: List[TextFactRecord] = []
    seen = set()

    for block in blocks:
        anchor = anchors_by_block[block.block_id]
        if anchor.status == "unresolved":
            continue
        nonempty_lines = [line for line in block.text.splitlines() if line.strip()]
        if nonempty_lines and sum(_looks_like_toc_page_line(line) for line in nonempty_lines) / len(nonempty_lines) >= 0.5:
            continue

        for raw_line in block.text.splitlines():
            if _looks_like_toc_page_line(raw_line):
                continue
            for raw_label, value_text in _iter_labeled_fields(raw_line):
                if not value_text.strip():
                    continue
                property_name, unit_hint = _normalize_property(raw_label)
                if property_name in _SKIP_LABELS or len(value_text) > 1600:
                    continue
                heading_owner = _heading_entity_override(block)
                record = _make_record(
                    block, anchor, property_name, value_text, unit_hint=unit_hint,
                    source_type="text_labeled_field", confidence=0.93,
                    relation_kind="composition" if property_name in {"组成", "组分", "配方组成"} else "attribute",
                    subject_override=heading_owner, fact_clause=raw_line.strip(),
                    owner_evidence=block.heading_title if heading_owner else "",
                    owner_source="leaf_entity_heading_field_owner" if heading_owner else "",
                )
                key = (record.subject, record.property_name, record.value_text, record.line_start)
                if key not in seen:
                    seen.add(key)
                    records.append(record)

        leaf = block.heading_title.strip()
        mapped_property = _SECTION_PROPERTY_MAP.get(leaf)
        if mapped_property and 5 <= len(block.text) <= 1600 and not any(
            True for line in block.text.splitlines() for _ in _iter_labeled_fields(line)
        ):
            record = _make_record(
                block, anchor, mapped_property, block.text.strip(), source_type="text_section_narrative",
                confidence=0.82, relation_kind="process" if "方法" in mapped_property else "attribute",
                fact_clause=block.text.strip(),
            )
            key = (record.subject, record.property_name, record.value_text, record.line_start)
            if key not in seen:
                seen.add(key)
                records.append(record)

        # Phase 9.7 binds owner, property, result value and clause-local
        # conditions before creating a fact.  A chapter anchor is only a
        # fallback when the clause contains no competing entity surface.
        for bound in bind_numeric_facts(block.text, anchor.subject):
            record = _make_record(
                block, anchor, bound.property_name, bound.value_text,
                source_type="text_clause_bound_numeric_property", confidence=bound.confidence,
                subject_override=bound.owner, fact_clause=bound.clause,
                owner_evidence=bound.clause, owner_source=bound.owner_source,
            )
            record.conditions.extend(bound.conditions)
            key = (record.subject, record.property_name, record.value_text, record.line_start, bound.clause)
            if key not in seen:
                seen.add(key)
                records.append(record)

        # High-recall relation candidates are emitted conservatively.  They
        # still pass through the normal subject, evidence and production gates;
        # unsupported ontology labels remain visible in the candidate layer.
        for record in _iter_high_recall_relations(block, anchor):
            key = (record.subject, record.property_name, record.value_text, record.line_start, record.fact_clause)
            if key not in seen:
                seen.add(key)
                records.append(record)

        # Composition extraction is clause-local.  It no longer crosses a full
        # stop/semicolon and unsafe visual/list references are retained only as
        # held audit candidates by the downstream self-containment gate.
        for _, _, clause in iter_clauses(block.text):
            for subject, value_text, fact_clause, owner_source in _composition_candidates(clause, anchor):
                record = _make_record(
                    block, anchor, "组成描述", value_text,
                    source_type="text_composition_description", confidence=0.84,
                    relation_kind="composition", subject_override=subject,
                    fact_clause=fact_clause, owner_evidence=fact_clause, owner_source=owner_source,
                )
                key = (record.subject, record.property_name, record.value_text, record.line_start)
                if key not in seen:
                    seen.add(key)
                    records.append(record)

    return records


__all__ = ["TextFactRecord", "extract_text_facts", "_looks_like_toc_page_line"]
