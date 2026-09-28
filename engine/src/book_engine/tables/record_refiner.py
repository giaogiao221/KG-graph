from __future__ import annotations

import copy
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Dict, Iterable, List, Sequence, Tuple

from book_engine.core.schemas import (
    ConditionAtom,
    ConditionBinding,
    ConditionalFactRecord,
    TableBlock,
    TableGrid,
    TableSemanticPlan,
    SourceLocation,
)
from book_engine.tables.value_parser import parse_value

_UNIT_PATTERN = (
    r"%|‰|℃|°C|K|Pa|kPa|MPa|GPa|bar|mbar|g/cm(?:3|³)|kg/m(?:3|³)|"
    r"g|kg|mg|ug|μg|V|mV|kV|A|mA|nm|um|μm|mm|cm|m|C|ns|us|μs|ms|s|min|h|d|Hz|kHz|MHz|"
    r"rpm|r/min|K/min|℃/min|°C/min|mm/s|cm/s|m/s|km/s|J/g|kJ/kg|kJ/mol|"
    r"mol/L|g/L|mg/L"
)
_UNIT_SUFFIX_RE = re.compile(
    rf"(?:\s*/\s*|\s*[（(]\s*)?(?P<unit>{_UNIT_PATTERN})\s*[）)]?\s*$",
    re.IGNORECASE,
)
_PROPERTY_LIKE_RE = re.compile(
    r"^(?:相对分子质量|分子量|密度|相对密度|理论密度|装药密度|熔点|沸点|闪点|"
    r"分解温度|峰顶温度|爆速|爆压|爆热|爆温|爆容|燃速|比冲|压力指数|感度|"
    r"撞击感度|摩擦感度|火花感度|威力|猛度|生成热|生成焓|燃烧热|氧平衡|"
    r"粒径|粒度|比表面积|黏度|粘度|纯度|含量|含水量|吸湿性|拉伸强度|"
    r"伸长率|模量|硬度|活化能|速率常数|误差|计算值|实测值|理论值)"
    rf"(?:\s*/\s*{_UNIT_PATTERN}|\s*[（(]\s*{_UNIT_PATTERN}\s*[）)])?$",
    re.IGNORECASE,
)
_GENERIC_PROPERTY = {
    "", "属性", "项目", "指标", "数值", "结果", "性能", "理化指标", "测试结果", "试验结果",
    "实验结果", "检验结果", "测定值", "实测值", "计算值", "理论值", "未命名属性",
}
_MATERIAL_CODE_RE = re.compile(
    r"^(?:HMX|RDX|TNT|PETN|CL-?20|NTO|ADN|AP|AN|NG|NC|GAP|HTPB|BAMO|AMMO|"
    r"NIMMO|FOX-?7|TATB|PBX(?:N)?-?\d+|LX-?\d+|NEPE|CMDB)(?:\b|$)",
    re.IGNORECASE,
)
_MATERIAL_HINT_RE = re.compile(
    r"(?:硝酸|硝基|叠氮|高氯酸|聚合物|预聚物|弹性体|推进剂|炸药|药剂|火药|"
    r"氧化剂|粘结剂|黏结剂|增塑剂|键合剂|固化剂|催化剂|铝|银|铅|铜|铁|镍|钴)",
    re.IGNORECASE,
)
_EXPLICIT_SUBJECT_SPLIT_RE = re.compile(r"\s*(?:、|，|,|；|;|\n|\r\n)\s*")
_RANGE_MARK_RE = re.compile(r"(?:~|～|—|–|至|到|\.\.|±)")
_NUMBER_TOKEN_RE = re.compile(r"[-+−]?\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?")
_CONCAT_DECIMAL_RE = re.compile(r"^[<>≤≥≈~～±+\-−]?\d+\.\d+\.\d+(?:\.\d+)*")

_GROUP_SUBJECT_RE = re.compile(
    r"^(?:特级品|优级品|一级品|二级品|三级品|合格品|不合格品|[A-Da-d]级|第?\d+组|样品\s*\d+|试样\s*\d+)$"
)
_SAMPLE_CODE_SUBJECT_RE = re.compile(r"^[A-Za-z]$")
_EXPERIMENT_COLUMN_CODE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+._\-/]{1,24}$")
_TEXT_IDENTITY_PROPERTY_RE = re.compile(
    r"^(?:中文名称|英文名称|中文别称|英文别称|别名|简称|代号|编号|CAS号|CAS登记号|(?:假定|经验|理论)?(?:分子式|化学式|结构式))$",
    re.IGNORECASE,
)
_EXPERIMENT_FACTOR_SUBJECTS = {
    "引发剂", "催化剂", "溶剂", "反应温度", "反应时间", "投料比", "单体", "共聚单体", "添加剂",
}
_TEMPERATURE_PROPERTY_RE = re.compile(r"(?:温度|熔点|沸点|闪点|燃点|自燃点|爆发点|玻璃化温度|分解温度|峰顶温度)")
_IDENTIFIER_PREFIX_RE = re.compile(r"^(?:编号|序号|批次|样品|试样|配方|聚合物|no\.?|id|#)\s*", re.IGNORECASE)
_IDENTIFIER_ONLY_RE = re.compile(
    r"^(?:\d+(?:[.．-]\d+)?|[一二三四五六七八九十百]+)(?:号|[#@*※①②③④⑤⑥⑦⑧⑨⑩])?$",
    re.IGNORECASE,
)
_IDENTIFIER_SUBJECT_SOURCES = {"table_identifier_axis", "table_entity_axis", "row_header"}
_GENERIC_PROPERTY_AXIS_HEADERS = {"项目", "属性", "指标", "参数", "名称"}
_MOLECULAR_RATIO_RE = re.compile(
    r"^(?:Mw|Mn|Mz|M)(?:\s*/\s*)(?:Mw|Mn|Mz|M)(?:[@#*※①②③④⑤⑥⑦⑧⑨⑩])?$",
    re.IGNORECASE,
)
_GENERIC_SAMPLE_VARIANT_RE = re.compile(
    r"^(?P<label>(?:共聚物|均聚物|聚合物|预聚物|样品|试样|配方|产物))\s*[（(](?P<id>[A-Za-z0-9-]{1,12})[）)]$",
    re.IGNORECASE,
)

_GENERIC_FORMULATION_SUBJECTS = {"基本配方", "实验配方", "试验配方", "配方", "基本组成", "配方记录"}
_GROUPED_CONDITION_HINT_RE = re.compile(r"(?:贮存期|储存期|时间|压力|温度|含量|用量|浓度|湿度|粒度|粒径|密度|延迟期|龄期)")
_FORMULATION_COMPOSITION_GROUP_RE = re.compile(
    r"(?:w(?:i)?\s*[×x*]\s*100|质量分数|质量百分|百分含量|组分含量|组成|配方)", re.I
)
_FORMULATION_SAMPLE_HEADER_RE = re.compile(r"(?:样品|试样|配方|组样成品|编号|代号)", re.I)
_FORMULATION_NOTE_HEADER_RE = re.compile(r"(?:备\s*注|说明|注)$", re.I)
_FORMULATION_COMPONENT_LEAF_RE = re.compile(
    r"^(?:[A-Z][A-Za-z0-9-]{0,12}|"
    r"(?:粘合剂|黏合剂|氧化剂|燃料|金属|粉|树脂|橡胶|聚合物|预聚物|共聚物|均聚物)"
    r"[\u4e00-\u9fffA-Za-z0-9+._/\-()]{0,12}|"
    r"[\u4e00-\u9fffA-Za-z0-9+._/\-()]{1,24}"
    r"(?:粘合剂|黏合剂|氧化剂|燃料|金属|粉|树脂|橡胶|聚合物|预聚物|共聚物|均聚物))$",
    re.I,
)


_ANALYTICAL_SYMBOL_RE = re.compile(
    r"(?:DSC|DTG\s*[-–—]?\s*TG|TG|结果|[/\s])\s*(?P<symbol>Texon|To|Td|Tg|Tp|Tm|T)"
    r"(?:\s*/\s*(?:℃C|℃|°C{1,2}|C|K))?\s*$",
    re.IGNORECASE,
)


def _normalize_analytical_property(label: str) -> Tuple[str, str]:
    """Preserve analytical leaf symbols instead of collapsing them to a parent result group."""
    raw = re.sub(r"\s+", " ", _nfkc(label)).strip()
    compact = re.sub(r"\s+", "", raw)
    if not compact:
        return "", ""
    if "生成热" in compact and ("DSC" in compact.upper() or "结果" in compact):
        unit = "J/g" if re.search(r"J[·.]?g[-−]?1|J/g", compact, re.IGNORECASE) else ""
        return "生成热", unit
    match = _ANALYTICAL_SYMBOL_RE.search(raw)
    if not match:
        return "", ""
    symbol = match.group("symbol")
    symbol_key = symbol.casefold()
    canonical_symbol = {
        "texon": "Texon", "to": "To", "td": "Td", "tg": "Tg",
        "tp": "Tp", "tm": "Tm", "t": "T",
    }.get(symbol_key, symbol)
    upper = compact.upper()
    if "DTG" in upper and "TG" in upper:
        prefix = "DTG-TG"
    elif "DSC" in upper:
        prefix = "DSC"
    elif "TG" in upper:
        prefix = "TG"
    else:
        return "", ""
    suffix = {
        "T": "温度T",
        "To": "起始温度To",
        "Texon": "外推起始温度Texon",
        "Td": "分解温度Td",
        "Tg": "玻璃化温度Tg",
        "Tp": "峰值温度Tp",
        "Tm": "熔融温度Tm",
    }[canonical_symbol]
    unit = "℃" if re.search(r"/(?:℃C|℃|°C{1,2}|C)\s*$", compact, re.IGNORECASE) else ""
    return f"{prefix}{suffix}", unit


def _collapsed_identifier_sequence(text: str) -> bool:
    value = _IDENTIFIER_PREFIX_RE.sub("", re.sub(r"\s+", "", _nfkc(text))).strip("#@*※①②③④⑤⑥⑦⑧⑨⑩")
    return value in {"12", "123", "1234", "12345", "123456", "1234567", "12345678", "123456789"}


def _long_unseparated_integer(text: str) -> bool:
    value = re.sub(r"\s+", "", _nfkc(text)).strip()
    return bool(re.fullmatch(r"[-+−]?\d{7,}", value))


@dataclass
class RefinementEvent:
    old_record_id: str
    new_record_id: str
    table_id: str
    action: str
    original_subject: str
    refined_subject: str
    original_property: str
    refined_property: str
    original_value: str
    refined_value: str
    original_unit: str
    refined_unit: str
    original_status: str
    refined_status: str
    reasons: List[str] = field(default_factory=list)


@dataclass
class RefinementSummary:
    events: List[RefinementEvent] = field(default_factory=list)
    unresolved: List[Dict[str, object]] = field(default_factory=list)


def _nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").replace("／", "/").replace("（", "(").replace("）", ")")


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", _nfkc(text)).casefold()


def _identifier_only_subject(text: str) -> bool:
    value = re.sub(r"\s+", "", _nfkc(text)).strip(" ,，;；:：|[]()（）")
    value = _IDENTIFIER_PREFIX_RE.sub("", value)
    return bool(value and _IDENTIFIER_ONLY_RE.fullmatch(value))


def _normalize_identifier_from_table_note(identifier: str, evidence: str) -> Tuple[str, str]:
    raw = re.sub(r"\s+", "", _nfkc(identifier)).strip()
    # Strip explicit/OCR footnote marks first (6@, 6①, 6※, etc.).
    stripped = raw.rstrip("@#*※①②③④⑤⑥⑦⑧⑨⑩")
    if stripped and stripped != raw and re.fullmatch(r"\d+|[一二三四五六七八九十百]+", stripped):
        return stripped, "stripped_identifier_footnote_marker"

    table_text = _nfkc(evidence)
    known_ids = set(re.findall(r"编号\s*([0-9]+)", table_text, flags=re.IGNORECASE))
    # OCR commonly turns a footnote glyph after a one-digit identifier into
    # zero, e.g. 1① -> 10.  Only repair when the table note explicitly lists
    # the one-digit ID and does not list the longer surface.
    if raw.isdigit() and len(raw) == 2 and raw.endswith("0"):
        candidate = raw[0]
        if candidate in known_ids and raw not in known_ids:
            return candidate, "normalized_identifier_ocr_footnote_zero_from_table_note"
    return raw, ""


def _extract_unit(label: str) -> Tuple[str, str]:
    value = re.sub(r"\s+", " ", _nfkc(label)).strip()
    # Molecular-weight ratios use a slash as part of the property symbol; the
    # trailing M/Mn/Mw is not the metre unit.
    compact_ratio = re.sub(r"\s+", "", value)
    if _MOLECULAR_RATIO_RE.fullmatch(compact_ratio) or re.search(
        r"(?:^|/)(?:M(?:w|n|z)?)/(?:M(?:w|n|z)?)$", compact_ratio, re.IGNORECASE
    ):
        return value, ""
    match = _UNIT_SUFFIX_RE.search(value)
    if not match:
        return value.strip(" /:："), ""
    unit = match.group("unit")
    name = value[: match.start()].strip(" /:：(")
    return name or value.strip(" /:："), unit


def _collapse_path(label: str) -> str:
    parts = [part.strip() for part in re.split(r"\s*/\s*", label or "") if part.strip()]
    if not parts:
        return ""
    result: List[str] = []
    for part in parts:
        if result and _compact(result[-1]) == _compact(part):
            continue
        result.append(part)
    # Drop generic parent headers when a specific leaf exists.
    if len(result) > 1 and _compact(result[0]) in {_compact(x) for x in _GENERIC_PROPERTY}:
        result = result[1:]
    return " / ".join(result)


def _property_like(text: str) -> bool:
    name, _ = _extract_unit(text)
    return bool(_PROPERTY_LIKE_RE.fullmatch(_nfkc(text).strip()) or _PROPERTY_LIKE_RE.fullmatch(name.strip()))


def _material_like(text: str) -> bool:
    value = re.sub(r"\s+", " ", _nfkc(text)).strip(" ,，;；:：|")
    if not value or len(value) > 60 or _property_like(value):
        return False
    if _MATERIAL_CODE_RE.search(value):
        return True
    if _MATERIAL_HINT_RE.search(value):
        return True
    # Short Latin chemical/formulation labels are useful but exclude ordinary words.
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9+._\-/]{1,24}", value):
        return True
    # Specific Chinese chemical-looking names.
    return bool(re.search(r"(?:酸|盐|酯|醚|胺|酚|烷|烯|醇|酮)$", value))


def _candidate_property_from_paths(record: ConditionalFactRecord) -> Tuple[str, str]:
    candidates = list(record.row_header_path) + list(record.column_header_path)
    subject_key = _compact(record.subject)
    for raw in reversed(candidates):
        without_unit, unit = _extract_unit(raw)
        name = _collapse_path(without_unit)
        if _compact(name) == subject_key and unit in {"%", "‰"}:
            return "含量", unit
        if not name or _compact(name) == subject_key or _compact(name) in {_compact(x) for x in _GENERIC_PROPERTY}:
            continue
        if _property_like(name) or any(token in name for token in ("含量", "纯度", "外观", "用途", "方法", "温度", "压力")):
            return name, unit
    return "", ""


def _multiple_numeric_tokens_without_structure(text: str) -> bool:
    value = _nfkc(text).strip()
    if not value or _RANGE_MARK_RE.search(value):
        return False
    # Ratios such as 1:1 are a single structured value.
    if re.fullmatch(r"\s*[-+]?\d+(?:\.\d+)?\s*[:：/]\s*[-+]?\d+(?:\.\d+)?\s*(?:%|‰)?\s*", value):
        return False
    if _CONCAT_DECIMAL_RE.match(re.sub(r"\s+", "", value)):
        return True
    tokens = _NUMBER_TOKEN_RE.findall(value)
    if len(tokens) <= 1:
        return False
    # Chemical names and identifiers commonly contain several digits; require a value-like surface.
    residue = _NUMBER_TOKEN_RE.sub("", value)
    residue = re.sub(rf"(?:{_UNIT_PATTERN})|[%‰,，;；/()\[\]\s:+\-−]", "", residue, flags=re.IGNORECASE)
    return len(tokens) >= 2 and not residue


def _split_subjects(subject: str) -> List[str]:
    value = _nfkc(subject).strip()
    if "/" in value and not any(mark in value for mark in ("、", "，", ",", "；", ";", "\n")):
        # Slash usually denotes a formulation system, not an enumeration.
        return [value]
    parts = [part.strip(" ,，;；:：|") for part in _EXPLICIT_SUBJECT_SPLIT_RE.split(value) if part.strip()]
    if not 2 <= len(parts) <= 6:
        return [value]
    if all(_material_like(part) for part in parts):
        return list(dict.fromkeys(parts))
    return [value]


def _new_split_id(record: ConditionalFactRecord, index: int, subject: str) -> str:
    digest = hashlib.sha1(f"{record.record_id}|split|{index}|{subject}".encode("utf-8")).hexdigest()[:10]
    return f"{record.record_id}-S{index:02d}-{digest}"


def _event(original: ConditionalFactRecord, refined: ConditionalFactRecord, action: str, reasons: Sequence[str]) -> RefinementEvent:
    return RefinementEvent(
        old_record_id=original.record_id,
        new_record_id=refined.record_id,
        table_id=original.table_id,
        action=action,
        original_subject=original.subject,
        refined_subject=refined.subject,
        original_property=original.property_name,
        refined_property=refined.property_name,
        original_value=original.value_text,
        refined_value=refined.value_text,
        original_unit=original.unit,
        refined_unit=refined.unit,
        original_status=original.record_status,
        refined_status=refined.record_status,
        reasons=list(dict.fromkeys(reasons)),
    )


def refine_record(record: ConditionalFactRecord, metadata=None) -> Tuple[List[ConditionalFactRecord], List[RefinementEvent], List[Dict[str, object]]]:
    original = copy.deepcopy(record)
    refined = copy.deepcopy(record)
    reasons: List[str] = []
    unresolved: List[Dict[str, object]] = []

    # 1. Normalize property paths and separate unit from the attribute name.
    analytical_name, analytical_unit = _normalize_analytical_property(refined.property_name)
    if analytical_name:
        property_name, property_unit = analytical_name, analytical_unit
        reasons.append("preserved_analytical_property_leaf")
    else:
        property_without_unit, property_unit = _extract_unit(refined.property_name)
        property_name = _collapse_path(property_without_unit)
    if _MOLECULAR_RATIO_RE.fullmatch(re.sub(r"\s+", "", property_name or "")):
        property_name = "分散度"
        property_unit = ""
        reasons.append("normalized_molecular_weight_ratio_to_dispersity")
    if property_name and property_name != refined.property_name:
        refined.property_name = property_name
        reasons.append("normalized_property_path")
    if property_unit and not refined.unit:
        refined.unit = property_unit
        reasons.append("unit_extracted_from_property")
    if refined.unit.upper() == "C" and _TEMPERATURE_PROPERTY_RE.search(refined.property_name):
        refined.unit = "℃"
        reasons.append("normalized_ocr_temperature_unit_C_to_degC")

    # Numeric row identifiers are sample/batch keys, not material entities.
    # Rebind them to an explicit contextual material when available and keep the
    # original identifier as a row-scoped condition. Otherwise fail closed.
    if refined.subject_source in _IDENTIFIER_SUBJECT_SOURCES and _identifier_only_subject(refined.subject):
        raw_identifier_value = _nfkc(refined.subject).strip()
        identifier_value, identifier_normalization_reason = _normalize_identifier_from_table_note(
            raw_identifier_value, refined.evidence
        )
        refined.sample_id = identifier_value
        if identifier_normalization_reason:
            reasons.append(identifier_normalization_reason)
        if metadata is not None and getattr(metadata, "subject", ""):
            contextual_subject = str(metadata.subject).strip()
            refined.subject = contextual_subject
            refined.subject_source = f"context_identifier_rebind:{metadata.subject_source or 'table_context'}"
            refined.subject_type = "material_or_formulation_candidate"
            refined.confidence = min(refined.confidence, float(getattr(metadata, "subject_confidence", 0.0) or 0.82))
            if refined.record_status == "ready":
                refined.record_status = "candidate"
            refined.unresolved_reasons = sorted(set(
                refined.unresolved_reasons + ["subject_from_context_requires_registry_confirmation"]
            ))
            identifier_atom = ConditionAtom(
                condition_id=f"{refined.record_id}:sample_identifier",
                name="样品编号",
                normalized_name="样品编号",
                condition_type="sample_state",
                value_text=identifier_value,
                scope="row",
                priority=95,
                confidence=0.96,
                source_kind="phase93_identifier_subject_repair",
                source_text=raw_identifier_value,
                target_row=refined.row_index,
                binding_target=refined.record_id,
                reasons=["identifier_only_axis_rebound_to_context_subject"],
            )
            if not any(
                atom.normalized_name == identifier_atom.normalized_name and atom.value_text == identifier_atom.value_text
                for atom in refined.conditions
            ):
                refined.conditions = list(refined.conditions) + [identifier_atom]
            reasons.append("rebound_identifier_only_subject_to_context_subject")
            # OCR can collapse several table rows into one surface, e.g.
            # identifier “123” with molecular-mass text “38591166444214”.
            # Do not publish such values as a single measurement.
            if _collapsed_identifier_sequence(identifier_value) and _long_unseparated_integer(refined.value_text):
                refined.record_status = "unresolved"
                refined.unresolved_reasons = sorted(set(
                    refined.unresolved_reasons + ["suspected_collapsed_multirow_numeric_series"]
                ))
                unresolved.append({
                    "record_id": refined.record_id,
                    "table_id": refined.table_id,
                    "row_index": refined.row_index,
                    "column_index": refined.column_index,
                    "reason": "suspected_collapsed_multirow_numeric_series",
                    "value": refined.value_text,
                })
                reasons.append("held_collapsed_multirow_numeric_series")
        else:
            refined.record_status = "unresolved"
            refined.unresolved_reasons = sorted(set(
                refined.unresolved_reasons + ["identifier_only_subject_without_context"]
            ))
            unresolved.append({
                "record_id": refined.record_id,
                "table_id": refined.table_id,
                "row_index": refined.row_index,
                "column_index": refined.column_index,
                "reason": "identifier_only_subject_without_context",
                "value": identifier_value,
            })
            reasons.append("held_identifier_only_subject_without_context")

    # Bare A-F labels in repeated experimental rows are sample codes rather
    # than standalone materials. Rebind only when a confident contextual
    # material exists and the compiler already treated the label as a row/sample
    # key. The code is preserved as an explicit condition.
    if (
        metadata is not None
        and getattr(metadata, "subject", "")
        and float(getattr(metadata, "subject_confidence", 0.0) or 0.0) >= 0.74
        and refined.subject_source in {"row_header", "table_identifier_axis", "table_entity_axis"}
        and _SAMPLE_CODE_SUBJECT_RE.fullmatch(_nfkc(refined.subject).strip())
        and _compact(refined.sample_id or refined.subject) == _compact(refined.subject)
    ):
        sample_code = _nfkc(refined.subject).strip().upper()
        refined.subject = str(metadata.subject).strip()
        refined.subject_source = f"context_sample_code_rebind:{metadata.subject_source or 'table_context'}"
        refined.subject_type = "material_or_formulation_candidate"
        refined.sample_id = sample_code
        refined.confidence = min(refined.confidence, float(getattr(metadata, "subject_confidence", 0.0) or 0.82))
        if refined.record_status == "ready":
            refined.record_status = "candidate"
        refined.unresolved_reasons = sorted(set(
            refined.unresolved_reasons + ["subject_from_context_requires_registry_confirmation"]
        ))
        sample_atom = ConditionAtom(
            condition_id=f"{refined.record_id}:sample_code",
            name="样品代号", normalized_name="样品代号", condition_type="sample_state",
            value_text=sample_code, scope="row", priority=94, confidence=0.94,
            source_kind="phase93_sample_code_subject_repair", source_text=sample_code,
            target_row=refined.row_index, binding_target=refined.record_id,
            reasons=["bare_sample_code_rebound_to_context_subject"],
        )
        if not any(
            atom.normalized_name == sample_atom.normalized_name and atom.value_text == sample_atom.value_text
            for atom in refined.conditions
        ):
            refined.conditions = list(refined.conditions) + [sample_atom]
        reasons.append("rebound_bare_sample_code_to_context_subject")

    # Generic row labels such as 共聚物(1)/共聚物(2) are sample variants, not
    # useful standalone entities.  Rebind them only to a specific local context;
    # attached variants of a real material name (GAP-PEG(200)) remain identities.
    generic_variant = _GENERIC_SAMPLE_VARIANT_RE.fullmatch(_nfkc(refined.subject).strip())
    if (
        generic_variant
        and metadata is not None
        and getattr(metadata, "subject", "")
        and float(getattr(metadata, "subject_confidence", 0.0) or 0.0) >= 0.74
    ):
        sample_label = generic_variant.group("label")
        sample_id = generic_variant.group("id")
        contextual_subject = str(metadata.subject).strip()
        if _compact(contextual_subject) != _compact(sample_label):
            refined.subject = contextual_subject
            refined.subject_source = f"context_sample_variant_rebind:{metadata.subject_source or 'table_context'}"
            refined.subject_type = "material_or_formulation_candidate"
            refined.sample_id = sample_id
            refined.confidence = min(refined.confidence, float(getattr(metadata, "subject_confidence", 0.0) or 0.80))
            if refined.record_status == "ready":
                refined.record_status = "candidate"
            refined.unresolved_reasons = sorted(set(
                refined.unresolved_reasons + ["subject_from_context_requires_registry_confirmation"]
            ))
            atom = ConditionAtom(
                condition_id=f"{refined.record_id}:sample_variant",
                name="样品编号", normalized_name="样品编号", condition_type="sample_state",
                value_text=sample_id, scope="row", priority=94, confidence=0.94,
                source_kind="phase95_generic_sample_variant_repair",
                source_text=f"{sample_label}({sample_id})", target_row=refined.row_index,
                binding_target=refined.record_id,
                reasons=["generic_parenthetical_sample_rebound_to_context_subject"],
            )
            if not any(a.normalized_name == atom.normalized_name and a.value_text == atom.value_text for a in refined.conditions):
                refined.conditions = list(refined.conditions) + [atom]
            reasons.append("rebound_generic_parenthetical_sample_to_context_subject")

    # Group/grade columns identify a sample state, not the material itself.
    if metadata is not None and getattr(metadata, "subject", "") and _GROUP_SUBJECT_RE.fullmatch(_nfkc(refined.subject).strip()):
        group_value = refined.subject
        refined.subject = metadata.subject
        refined.subject_source = f"context:{metadata.subject_source or 'table_context'}"
        refined.subject_type = "material_or_formulation_candidate"
        refined.sample_id = refined.sample_id or group_value
        refined.conditions = list(refined.conditions) + [ConditionAtom(
            condition_id=f"{refined.record_id}:sample_group",
            name="样品等级", normalized_name="样品等级", condition_type="sample_state",
            value_text=group_value, scope="column", priority=80, confidence=0.88,
            source_kind="phase5_group_subject_repair", source_text=group_value,
            target_column=refined.column_index, binding_target=refined.record_id,
            reasons=["group_header_rebound_to_context_subject"],
        )]
        reasons.append("rebound_group_header_to_context_subject")

    # Attribute-value tables are sometimes misread as row-subject tables.
    # When a specific table-caption subject exists, rebind the numeric result to that subject.
    property_key = _compact(refined.property_name)
    generic_result_headers = {_compact(x) for x in ("项目", "指标", "理化指标", "结果", "数值", "性能")}
    if metadata is not None and getattr(metadata, "subject", "") and property_key in generic_result_headers:
        row_property_without_unit, row_property_unit = _extract_unit(refined.subject)
        row_property = _collapse_path(row_property_without_unit)
        if refined.value_text and _compact(refined.value_text) != _compact(refined.subject) and row_property:
            refined.subject = metadata.subject
            refined.subject_source = f"context:{metadata.subject_source or 'table_caption'}"
            refined.subject_type = "material_or_formulation_candidate"
            refined.property_name = row_property
            if row_property_unit:
                refined.unit = "℃" if row_property_unit.upper() == "C" and _TEMPERATURE_PROPERTY_RE.search(row_property) else row_property_unit
            reasons.append("repaired_attribute_value_table_orientation")
        elif _compact(refined.value_text) == _compact(refined.subject):
            refined.record_status = "unresolved"
            refined.unresolved_reasons = sorted(set(refined.unresolved_reasons + ["row_label_cell_not_fact"]))
            unresolved.append({
                "record_id": refined.record_id, "table_id": refined.table_id,
                "row_index": refined.row_index, "column_index": refined.column_index,
                "reason": "row_label_cell_not_fact", "value": refined.value_text,
            })
            reasons.append("held_row_label_cell")

    # Phase 9.9: a generic formulation label plus a row/sample identity is
    # represented as a stable row-level formulation entity.  This prevents
    # several distinct experimental formulations from collapsing onto the
    # same generic subject while preserving the original sample code as a
    # condition.
    generic_subject = _nfkc(refined.subject).strip()
    has_sample_condition = bool(refined.sample_id) or any(
        (atom.normalized_name or atom.name) in {"样品编号", "样品代号", "配方编号", "试样编号"}
        for atom in refined.conditions
    )
    if (
        generic_subject in _GENERIC_FORMULATION_SUBJECTS
        and refined.table_id
        and refined.row_index is not None
        and has_sample_condition
    ):
        refined.subject = f"配方:{refined.table_id}:R{refined.row_index}"
        refined.subject_source = "row_level_formulation_entity_phase99"
        refined.subject_type = "配方/材料体系"
        refined.confidence = max(refined.confidence, 0.90)
        reasons.append("synthesized_row_level_formulation_entity")

    # 2. Remove a repeated subject prefix from the attribute path.
    subject_key = _compact(refined.subject)
    property_parts = [part.strip() for part in re.split(r"\s*/\s*", refined.property_name) if part.strip()]
    if len(property_parts) > 1 and _compact(property_parts[0]) == subject_key:
        refined.property_name = " / ".join(property_parts[1:])
        reasons.append("removed_subject_prefix_from_property")

    # 3. Repair clear subject/property axis inversion.
    if _property_like(refined.subject) and _material_like(refined.property_name):
        parsed = parse_value(refined.value_text, refined.unit)
        if parsed.value_num is not None or parsed.lower_bound is not None or parsed.upper_bound is not None:
            old_subject = refined.subject
            old_property = refined.property_name
            old_property_name, old_property_unit = _extract_unit(old_subject)
            refined.subject = old_property
            refined.property_name = old_property_name
            if old_property_unit:
                refined.unit = old_property_unit
            refined.subject_source = "axis_inversion_repair"
            refined.subject_type = "material_or_formulation_candidate"
            refined.confidence = min(refined.confidence, 0.88)
            reasons.append("repaired_subject_property_axis_inversion")

    # 4. Resolve subject == property using independent header paths.
    if _compact(refined.subject) == _compact(refined.property_name) and refined.unit in {"%", "‰"}:
        refined.property_name = "含量"
        reasons.append("subject_named_percentage_normalized_to_content")
    if _compact(refined.subject) == _compact(refined.property_name) or _compact(refined.property_name) in {_compact(x) for x in _GENERIC_PROPERTY}:
        candidate, candidate_unit = _candidate_property_from_paths(refined)
        if candidate:
            refined.property_name = candidate
            if candidate_unit and not refined.unit:
                refined.unit = candidate_unit
            reasons.append("property_recovered_from_header_path")
        else:
            refined.record_status = "unresolved"
            refined.unresolved_reasons = sorted(set(refined.unresolved_reasons + ["subject_property_collision_unresolved"]))
            unresolved.append({
                "record_id": refined.record_id,
                "table_id": refined.table_id,
                "row_index": refined.row_index,
                "column_index": refined.column_index,
                "reason": "subject_property_collision_unresolved",
                "value": refined.value_text,
            })
            reasons.append("held_subject_property_collision")

    # Experimental-factor rows (initiator, catalyst, solvent, etc.) are not
    # material subjects. A textual value under a material-code column is a
    # strong sign of a transposed experiment matrix; fail closed until the
    # semantic planner resolves the orientation.
    if (
        _nfkc(refined.subject).strip() in _EXPERIMENT_FACTOR_SUBJECTS
        and refined.subject_source in {"table_entity_axis", "row_header"}
        and (
            _material_like(refined.property_name)
            or bool(_EXPERIMENT_COLUMN_CODE_RE.fullmatch(_nfkc(refined.property_name).strip()))
        )
    ):
        refined.record_status = "unresolved"
        refined.unresolved_reasons = sorted(set(
            refined.unresolved_reasons + ["experimental_factor_axis_misread_as_subject"]
        ))
        unresolved.append({
            "record_id": refined.record_id,
            "table_id": refined.table_id,
            "row_index": refined.row_index,
            "column_index": refined.column_index,
            "reason": "experimental_factor_axis_misread_as_subject",
            "value": refined.value_text,
        })
        reasons.append("held_experimental_factor_axis_misread_as_subject")

    # A paired “项目/性质 | 项目/性质” table can be miscompiled as
    # property(col-0) -> value(col-2).  When the target column itself is
    # labelled as a property axis and contains a short qualitative label,
    # fail closed rather than emit property-to-property facts.
    if (
        refined.subject_source.startswith(("context:", "context_identifier_rebind:"))
        and refined.value_role == "qualitative_description"
        and any(_compact(label) in {_compact(x) for x in _GENERIC_PROPERTY_AXIS_HEADERS} for label in refined.column_header_path)
        and parse_value(refined.value_text, refined.unit).value_num is None
    ):
        refined.record_status = "unresolved"
        refined.unresolved_reasons = sorted(set(
            refined.unresolved_reasons + ["property_axis_cell_misused_as_value"]
        ))
        unresolved.append({
            "record_id": refined.record_id,
            "table_id": refined.table_id,
            "row_index": refined.row_index,
            "column_index": refined.column_index,
            "reason": "property_axis_cell_misused_as_value",
            "value": refined.value_text,
        })
        reasons.append("held_property_axis_cell_misused_as_value")

    # 5. Reparse after property/unit normalization.
    parsed = parse_value(refined.value_text, refined.unit)
    refined.normalized_value_text = parsed.normalized_text
    refined.value_num = parsed.value_num
    refined.lower_bound = parsed.lower_bound
    refined.upper_bound = parsed.upper_bound
    refined.comparator = parsed.comparator
    if parsed.unit:
        refined.unit = parsed.unit

    # Identity values may contain digits and decimal points (chemical formulae,
    # CAS numbers, model codes) but are not numeric measurements. Keep their
    # full surface as visible text and suppress the numeric projection.
    if _TEXT_IDENTITY_PROPERTY_RE.fullmatch(_nfkc(refined.property_name).strip()):
        if any(value is not None for value in (refined.value_num, refined.lower_bound, refined.upper_bound)):
            reasons.append("suppressed_numeric_projection_for_identity_property")
        refined.value_num = None
        refined.lower_bound = None
        refined.upper_bound = None
        refined.comparator = ""
        refined.value_role = "identity_text"

    # 6. Quarantine OCR-concatenated or unbound numeric series instead of accepting the first number.
    if _multiple_numeric_tokens_without_structure(refined.value_text):
        refined.record_status = "unresolved"
        refined.unresolved_reasons = sorted(set(refined.unresolved_reasons + ["suspected_concatenated_numeric_series"]))
        unresolved.append({
            "record_id": refined.record_id,
            "table_id": refined.table_id,
            "row_index": refined.row_index,
            "column_index": refined.column_index,
            "reason": "suspected_concatenated_numeric_series",
            "value": refined.value_text,
        })
        reasons.append("held_concatenated_numeric_series")

    # 7. Split only explicit, conservative multi-subject enumerations.
    subjects = _split_subjects(refined.subject)
    output: List[ConditionalFactRecord] = []
    events: List[RefinementEvent] = []
    if len(subjects) > 1:
        for index, subject in enumerate(subjects, start=1):
            item = copy.deepcopy(refined)
            item.subject = subject
            item.record_id = _new_split_id(refined, index, subject)
            item.subject_source = "multi_subject_split"
            item.confidence = max(0.0, min(item.confidence, 0.86))
            item.unresolved_reasons = sorted(set(item.unresolved_reasons + ["split_from_explicit_multi_subject_cell"]))
            output.append(item)
            events.append(_event(original, item, "split_multi_subject", reasons + ["explicit_enumeration_all_parts_material_like"]))
        return output, events, unresolved

    output.append(refined)
    if reasons or any((
        original.subject != refined.subject,
        original.property_name != refined.property_name,
        original.unit != refined.unit,
        original.record_status != refined.record_status,
    )):
        events.append(_event(original, refined, "refine_record", reasons))
    return output, events, unresolved


_CAUSAL_TITLE_RE = re.compile(
    r"(?P<x>[^。；;：:]{1,55}?)(?:对|与)(?P<y>[^。；;：:]{1,35}?)(?:的)?(?:影响|关系)",
    re.IGNORECASE,
)
_RESPONSE_TERM_RE = re.compile(
    r"(?:电压|电流|熄灭长度|起爆能力|燃速|爆速|爆压|爆热|感度|强度|应力|应变|模量|"
    r"得率|产率|收率|转化率|反应速率|性能|温度|压力|距离|深度|时间)$"
)
_COUNT_PROPERTY_RE = re.compile(r"(?:试验|实验|测试|样本|样品)?(?:数量|次数|数)$")


def _clean_causal_phrase(value: str) -> str:
    value = re.sub(r"^\\s*表\\s*\\d+(?:[-－—.]\\d+)*\\s*", "", _nfkc(value), flags=re.IGNORECASE)
    value = re.sub(r"(?:大小|变化|不同|平均)$", "", value.strip(" 的：:、，,"))
    return value


def _response_atom(record: ConditionalFactRecord) -> ConditionAtom | None:
    candidates = []
    for atom in record.conditions:
        clean_name, _ = _extract_unit(atom.normalized_name or atom.name)
        if _RESPONSE_TERM_RE.search(_clean_causal_phrase(clean_name)):
            candidates.append(atom)
    return max(candidates, key=lambda atom: (atom.priority, atom.confidence), default=None)


def _condition_from_measurement(record: ConditionalFactRecord, target_id: str) -> ConditionAtom:
    return ConditionAtom(
        condition_id=f"{target_id}:causal:{record.column_index}",
        name=record.property_name,
        normalized_name=record.property_name,
        condition_type="experimental_condition",
        value_text=record.value_text,
        unit=record.unit,
        scope="row",
        priority=86,
        confidence=min(0.92, max(0.76, record.confidence)),
        source_kind="phase95_causal_axis_repair",
        source_text=record.value_text,
        target_row=record.row_index,
        target_column=record.column_index,
        binding_target=target_id,
        reasons=["independent_measurement_rebound_as_causal_condition"],
    )


def _repair_causal_response_records(
    block: TableBlock,
    records: Sequence[ConditionalFactRecord],
) -> Tuple[List[ConditionalFactRecord], List[RefinementEvent]]:
    context = " ".join([block.preceding_text or "", block.heading or "", *(block.heading_path or [])])
    match = _CAUSAL_TITLE_RE.search(_nfkc(context))
    if not match or not records:
        return list(records), []
    response_phrase = _clean_causal_phrase(match.group("y"))
    by_row: Dict[int | None, List[ConditionalFactRecord]] = {}
    for record in records:
        by_row.setdefault(record.row_index, []).append(record)
    repaired: List[ConditionalFactRecord] = []
    events: List[RefinementEvent] = []
    changed = False
    for row_index, row_records in by_row.items():
        if row_index is None:
            repaired.extend(row_records)
            continue
        response_atoms = [atom for record in row_records for atom in [_response_atom(record)] if atom is not None]
        response_atom = max(response_atoms, key=lambda atom: (atom.priority, atom.confidence), default=None)
        response_records = [
            record for record in row_records
            if response_phrase and (
                _compact(response_phrase) in _compact(record.property_name)
                or _compact(record.property_name) in _compact(response_phrase)
            )
        ]
        if response_atom is None and not response_records:
            response_records = [record for record in row_records if _RESPONSE_TERM_RE.search(_clean_causal_phrase(record.property_name))]
        response_record = response_records[-1] if response_records else None
        if response_atom is None and response_record is None:
            repaired.extend(row_records)
            continue
        # Require at least one independent measurement; otherwise the table was
        # already compiled as response-with-conditions and needs no rewrite.
        independent = [record for record in row_records if record is not response_record]
        if not independent and response_atom is None:
            repaired.extend(row_records)
            continue
        base = copy.deepcopy(response_record or row_records[0])
        original_base = copy.deepcopy(base)
        if response_atom is not None:
            prop, unit = _extract_unit(response_atom.normalized_name or response_atom.name)
            base.property_name = _collapse_path(prop)
            base.value_text = response_atom.value_text
            base.unit = response_atom.unit or unit
            base.column_index = response_atom.target_column if response_atom.target_column is not None else base.column_index
        else:
            base.property_name = _collapse_path(_extract_unit(base.property_name)[0])
        parsed = parse_value(base.value_text, base.unit)
        base.normalized_value_text = parsed.normalized_text
        base.value_num = parsed.value_num
        base.lower_bound = parsed.lower_bound
        base.upper_bound = parsed.upper_bound
        base.comparator = parsed.comparator
        if parsed.unit:
            base.unit = parsed.unit
        base.record_id = f"{base.table_id}-R{int(row_index):04d}-CAUSAL-{hashlib.sha1((base.property_name+'|'+base.value_text).encode('utf-8')).hexdigest()[:10]}"
        base.property_role = "response_property"
        base.value_role = "measured_result"
        base.subject_source = base.subject_source or "causal_table_context"
        base.confidence = min(base.confidence, 0.88)
        if base.record_status == "ready":
            base.record_status = "candidate"
        # Preserve existing non-response conditions, then bind independent axes.
        conditions = []
        for atom in base.conditions:
            if response_atom is not None and (
                atom.condition_id == response_atom.condition_id
                or _compact(atom.normalized_name or atom.name) == _compact(response_atom.normalized_name or response_atom.name)
            ):
                continue
            conditions.append(atom)
        for record in independent:
            if response_record is not None and record.record_id == response_record.record_id:
                continue
            atom = _condition_from_measurement(record, base.record_id)
            if not any(
                _compact(existing.normalized_name or existing.name) == _compact(atom.normalized_name)
                and _compact(existing.value_text) == _compact(atom.value_text)
                for existing in conditions
            ):
                conditions.append(atom)
        base.conditions = conditions
        base.unresolved_reasons = [reason for reason in base.unresolved_reasons if reason != "subject_from_context_requires_registry_confirmation"]
        repaired.append(base)
        events.append(_event(original_base, base, "repair_causal_response_axis", [
            "causal_caption_x_to_y",
            "independent_axes_rebound_as_conditions",
            "response_axis_promoted_to_property",
        ]))
        changed = True
    return (repaired if changed else list(records)), events



def _clean_grouped_header_label(label: str) -> Tuple[str, str]:
    value = re.sub(r"\s+", " ", _nfkc(label)).strip()
    name, unit = _extract_unit(value)
    name = re.sub(r"[（(][^()（）]{1,16}[）)]\s*$", "", name).strip(" /:：")
    return name or value, unit


def _looks_numeric_cell(value: str) -> bool:
    parsed = parse_value(_nfkc(value))
    return parsed.value_num is not None or parsed.lower_bound is not None or parsed.upper_bound is not None


def _recover_grouped_property_condition_matrix(
    block: TableBlock,
    grid: TableGrid,
    plan: TableSemanticPlan,
    records: Sequence[ConditionalFactRecord],
) -> Tuple[List[ConditionalFactRecord], List[RefinementEvent]]:
    """Recover matrices with property-group headers over numeric condition leaves.

    Generic structure:
      row 0: entity label | condition family spanning N columns
      row 1: property A spanning k columns | property B spanning m columns
      row 2: numeric condition levels
      rows 3+: row subjects and measured results

    The recovery is strictly structural and does not depend on any book, table
    id or material whitelist.
    """
    if grid.row_count < 5 or grid.column_count < 3:
        return list(records), []
    top_left = grid.cells[0][0]
    top_group = grid.cells[0][1]
    if top_left.rowspan < 2 or top_group.colspan < 2:
        return list(records), []

    property_row = -1
    condition_row = -1
    for row in range(1, min(4, grid.row_count - 1)):
        independent = [cell for cell in grid.cells[row][1:] if cell.normalized_text and not cell.is_span_copy]
        if len(independent) < 2 or not any(cell.colspan > 1 for cell in independent):
            continue
        next_values = [grid.cells[row + 1][col].normalized_text for col in range(1, grid.column_count)]
        numeric_ratio = sum(_looks_numeric_cell(v) for v in next_values if v) / max(1, sum(bool(v) for v in next_values))
        if numeric_ratio >= 0.70:
            property_row, condition_row = row, row + 1
            break
    if property_row < 0:
        return list(records), []

    condition_family, condition_unit = _clean_grouped_header_label(top_group.normalized_text)
    if not condition_family or not _GROUPED_CONDITION_HINT_RE.search(condition_family):
        return list(records), []

    data_start = condition_row + 1
    subjects = [grid.cells[row][0].normalized_text.strip() for row in range(data_start, grid.row_count)]
    lexical = [x for x in subjects if x and re.search(r"[A-Za-z\u4e00-\u9fff]", x)]
    if len(lexical) < 2 or len(lexical) / max(1, len([x for x in subjects if x])) < 0.60:
        return list(records), []

    property_by_col: Dict[int, Tuple[str, str]] = {}
    for col in range(1, grid.column_count):
        label = grid.cells[property_row][col].normalized_text
        prop, unit = _clean_grouped_header_label(label)
        if not prop or _looks_numeric_cell(prop):
            return list(records), []
        property_by_col[col] = (prop, unit)
    if len({prop for prop, _ in property_by_col.values()}) < 2:
        return list(records), []

    recovered: List[ConditionalFactRecord] = []
    for row in range(data_start, grid.row_count):
        subject = grid.cells[row][0].normalized_text.strip()
        if not subject:
            continue
        for col in range(1, grid.column_count):
            value_text = grid.cells[row][col].normalized_text.strip()
            condition_value = grid.cells[condition_row][col].normalized_text.strip()
            if not value_text or not condition_value or not _looks_numeric_cell(value_text):
                continue
            prop, prop_unit = property_by_col[col]
            parsed = parse_value(value_text, prop_unit)
            condition_text = f"{condition_value}{condition_unit}" if condition_unit else condition_value
            condition_parsed = parse_value(condition_text, condition_unit)
            digest = hashlib.sha1(
                f"{block.table_id}|{row}|{col}|{subject}|{prop}|{value_text}|{condition_text}".encode("utf-8")
            ).hexdigest()[:10]
            record_id = f"{block.table_id}-R{row:04d}-C{col:04d}-{digest}"
            atom = ConditionAtom(
                name=condition_family,
                normalized_name=condition_family,
                condition_type="axis_condition",
                value_text=condition_text,
                unit=condition_unit,
                value_num=condition_parsed.value_num,
                lower_bound=condition_parsed.lower_bound,
                upper_bound=condition_parsed.upper_bound,
                comparator=condition_parsed.comparator,
                scope="column",
                priority=95,
                confidence=0.95,
                condition_id=f"{record_id}:grouped_condition",
                source_kind="grouped_numeric_header_phase99",
                source_text=grid.cells[condition_row][col].raw_text,
                target_column=col,
                binding_target=record_id,
                reasons=["numeric_leaf_under_condition_family"],
            )
            recovered.append(ConditionalFactRecord(
                subject=subject,
                subject_type="material_or_formulation_candidate",
                property_name=prop,
                value_text=value_text,
                unit=parsed.unit or prop_unit,
                value_num=parsed.value_num,
                lower_bound=parsed.lower_bound,
                upper_bound=parsed.upper_bound,
                comparator=parsed.comparator,
                value_role="measured_result",
                conditions=[atom],
                evidence=block.raw_text,
                confidence=0.91,
                source=SourceLocation(
                    source_path="", block_id=block.table_id, table_id=block.table_id,
                    row_index=row, column_index=col, line_start=block.line_start, line_end=block.line_end,
                ),
                record_id=record_id,
                table_id=block.table_id,
                row_index=row,
                column_index=col,
                property_role="measurement",
                normalized_value_text=parsed.normalized_text,
                subject_source="row_header",
                row_header_path=[subject],
                column_header_path=[condition_family, prop, condition_value],
                record_status="ready",
                unresolved_reasons=[],
            ))
    expected = len([x for x in subjects if x]) * (grid.column_count - 1)
    if not recovered or len(recovered) < max(4, int(expected * 0.70)):
        return list(records), []
    old = records[0] if records else recovered[0]
    event = _event(old, recovered[0], "recover_grouped_property_condition_matrix", [
        "property_group_header_detected",
        "numeric_leaf_headers_rebound_as_conditions",
        "row_entities_preserved",
    ])
    return recovered, [event]



def _recover_row_level_formulation_matrix(
    block: TableBlock,
    grid: TableGrid,
    plan: TableSemanticPlan,
    records: Sequence[ConditionalFactRecord],
) -> Tuple[List[ConditionalFactRecord], List[RefinementEvent]]:
    """Recover formulation rows with component percentages and leaf properties.

    Generic structure:
      row 0: sample axis | composition group spanning >=2 columns | property groups | note
      row 1: component/property leaf names
      rows 2+: sample code and values

    Each row becomes a stable formulation entity.  Component percentages are
    emitted as composition facts; measured columns retain the leaf material in
    the property name (e.g. AP粒度 vs Al粒度).
    """
    if grid.row_count < 4 or grid.column_count < 4:
        return list(records), []
    first_header = grid.cells[0][0]
    if (
        first_header.rowspan < 2
        or first_header.colspan != 1
        or not _FORMULATION_SAMPLE_HEADER_RE.search(first_header.normalized_text or "")
    ):
        return list(records), []

    composition_cols: List[int] = []
    property_cols: List[int] = []
    note_cols: List[int] = []
    composition_unit = "%"
    for col in range(1, grid.column_count):
        top = grid.cells[0][col]
        top_text = top.normalized_text.strip()
        if _FORMULATION_NOTE_HEADER_RE.search(top_text):
            note_cols.append(col)
            continue
        if _FORMULATION_COMPOSITION_GROUP_RE.search(top_text):
            composition_cols.append(col)
        else:
            property_cols.append(col)
    if len(composition_cols) < 2 or not property_cols:
        return list(records), []

    # A composition group must have one real spanning origin.  This prevents
    # a row-spanned sample header such as “配方代号” from being mistaken for
    # a composition family merely because it contains the word 配方.
    composition_origins = {
        (grid.cells[0][col].origin_row, grid.cells[0][col].origin_column)
        for col in composition_cols
    }
    if len(composition_origins) != 1:
        return list(records), []
    origin_col = next(iter(composition_origins))[1]
    origin_cell = grid.cells[0][origin_col]
    if origin_cell.colspan < 2 or origin_col == 0:
        return list(records), []

    # Span copies repeat the same top group across all member columns, so the
    # simple classification above is sufficient.  Reject tables whose second
    # row does not provide useful leaf names.
    leaf_by_col: Dict[int, str] = {}
    for col in composition_cols + property_cols:
        leaf = grid.cells[1][col].normalized_text.strip()
        if not leaf:
            return list(records), []
        # Material codes such as A1/Al are lexical headers even though a generic
        # numeric parser can see the trailing digit. Pure numeric leaves remain
        # invalid for this topology.
        if _looks_numeric_cell(leaf) and not (
            col in composition_cols and _FORMULATION_COMPONENT_LEAF_RE.fullmatch(leaf)
        ):
            return list(records), []
        leaf_by_col[col] = leaf

    component_leaves = [leaf_by_col[col] for col in composition_cols]
    material_like_leaves = sum(bool(_FORMULATION_COMPONENT_LEAF_RE.fullmatch(leaf)) for leaf in component_leaves)
    if material_like_leaves < 2 or material_like_leaves / len(component_leaves) < 0.67:
        return list(records), []

    # Composition columns should behave like percentages: on most populated
    # rows their sum is close to 100.  This structural invariant is generic and
    # prevents mechanical-property matrices from being reinterpreted as
    # formulations.
    populated_rows = 0
    plausible_sum_rows = 0
    for row in range(2, grid.row_count):
        numbers: List[float] = []
        complete = True
        for col in composition_cols:
            parsed = parse_value(grid.cells[row][col].normalized_text.strip())
            number = parsed.value_num
            if number is None or number < 0 or number > 100:
                complete = False
                break
            numbers.append(float(number))
        if not complete or len(numbers) != len(composition_cols):
            continue
        populated_rows += 1
        if 90.0 <= sum(numbers) <= 110.0:
            plausible_sum_rows += 1
    if populated_rows < 2 or plausible_sum_rows / populated_rows < 0.60:
        return list(records), []

    recovered: List[ConditionalFactRecord] = []
    for row in range(2, grid.row_count):
        sample = grid.cells[row][0].normalized_text.strip()
        if not sample:
            continue
        subject = f"配方:{block.table_id}:R{row}"
        sample_atom = ConditionAtom(
            name="样品代号", normalized_name="样品代号", condition_type="identifier",
            value_text=sample, scope="row", priority=100, confidence=0.96,
            condition_id=f"{subject}:sample", source_kind="row_sample_identifier_phase100",
            source_text=grid.cells[row][0].raw_text, target_row=row, reasons=["row_level_formulation_sample"],
        )
        note_atoms: List[ConditionAtom] = []
        for col in note_cols:
            note = grid.cells[row][col].normalized_text.strip()
            if note:
                note_atoms.append(ConditionAtom(
                    name="备注", normalized_name="备注", condition_type="qualitative_context",
                    value_text=note, scope="row", priority=20, confidence=0.85,
                    condition_id=f"{subject}:note:{col}", source_kind="row_note_phase100",
                    source_text=grid.cells[row][col].raw_text, target_row=row, reasons=["row_level_formulation_note"],
                ))

        for col in composition_cols:
            raw_value = grid.cells[row][col].normalized_text.strip()
            if not raw_value or not _looks_numeric_cell(raw_value):
                continue
            component = leaf_by_col[col]
            # Common OCR ambiguity: A1 is almost always Al in energetic-material
            # formulation headers.  Preserve the raw header in the path but use
            # the chemically valid surface for the component name.
            normalized_component = "Al" if re.fullmatch(r"A[1lI]", component, re.I) else component
            value_text = raw_value if re.search(r"%|‰", raw_value) else raw_value + composition_unit
            parsed = parse_value(value_text, composition_unit)
            digest = hashlib.sha1(
                f"{block.table_id}|{row}|{col}|{subject}|{normalized_component}|{value_text}".encode("utf-8")
            ).hexdigest()[:10]
            record_id = f"{block.table_id}-R{row:04d}-C{col:04d}-{digest}"
            recovered.append(ConditionalFactRecord(
                subject=subject, subject_type="配方/材料体系", property_name=normalized_component,
                value_text=value_text, unit=parsed.unit or composition_unit, value_num=parsed.value_num,
                lower_bound=parsed.lower_bound, upper_bound=parsed.upper_bound, comparator=parsed.comparator,
                value_role="formulation_component", conditions=[sample_atom] + note_atoms, evidence=block.raw_text,
                confidence=0.94, source=SourceLocation(
                    source_path="", block_id=block.table_id, table_id=block.table_id, row_index=row,
                    column_index=col, line_start=block.line_start, line_end=block.line_end,
                ), record_id=record_id, table_id=block.table_id, row_index=row, column_index=col,
                property_role="composition", normalized_value_text=parsed.normalized_text,
                subject_source="row_level_formulation_entity_phase100", row_header_path=[sample],
                column_header_path=[grid.cells[0][col].normalized_text, component],
                record_status="ready", unresolved_reasons=[], sample_id=sample,
            ))

        for col in property_cols:
            raw_value = grid.cells[row][col].normalized_text.strip().lstrip(":：")
            if not raw_value or not _looks_numeric_cell(raw_value):
                continue
            parent = grid.cells[0][col].normalized_text.strip()
            leaf = leaf_by_col[col]
            if re.search(r"粒度|粒径", parent):
                leaf_clean = re.sub(r"[（(].*?[）)]", "", leaf).strip()
                property_name = f"{leaf_clean}粒度"
            elif parent and _compact(parent) != _compact(leaf):
                property_name = f"{leaf}{parent}"
            else:
                property_name = leaf
            parsed = parse_value(raw_value)
            digest = hashlib.sha1(
                f"{block.table_id}|{row}|{col}|{subject}|{property_name}|{raw_value}".encode("utf-8")
            ).hexdigest()[:10]
            record_id = f"{block.table_id}-R{row:04d}-C{col:04d}-{digest}"
            recovered.append(ConditionalFactRecord(
                subject=subject, subject_type="配方/材料体系", property_name=property_name,
                value_text=raw_value, unit=parsed.unit, value_num=parsed.value_num, lower_bound=parsed.lower_bound,
                upper_bound=parsed.upper_bound, comparator=parsed.comparator, value_role="measured_result",
                conditions=[sample_atom] + note_atoms, evidence=block.raw_text, confidence=0.92,
                source=SourceLocation(
                    source_path="", block_id=block.table_id, table_id=block.table_id, row_index=row,
                    column_index=col, line_start=block.line_start, line_end=block.line_end,
                ), record_id=record_id, table_id=block.table_id, row_index=row, column_index=col,
                property_role="measurement", normalized_value_text=parsed.normalized_text,
                subject_source="row_level_formulation_entity_phase100", row_header_path=[sample],
                column_header_path=[parent, leaf], record_status="ready", unresolved_reasons=[], sample_id=sample,
            ))

    data_rows = sum(1 for row in range(2, grid.row_count) if grid.cells[row][0].normalized_text.strip())
    if data_rows < 2 or len(recovered) < data_rows * 3:
        return list(records), []
    old = records[0] if records else recovered[0]
    event = _event(old, recovered[0], "recover_row_level_formulation_matrix", [
        "row_level_formulation_entities", "component_percentages_projected",
        "property_leaf_material_preserved", "sample_and_note_bound_as_conditions",
    ])
    return recovered, [event]


def _clone_bindings(bindings: Sequence[ConditionBinding], record_map: Dict[str, List[str]]) -> List[ConditionBinding]:
    result: List[ConditionBinding] = []
    for binding in bindings:
        target_ids = record_map.get(binding.record_id, [binding.record_id])
        for target_id in target_ids:
            result.append(replace(binding, record_id=target_id))
    return result


def refine_condition_results(condition_results: Sequence[Tuple]) -> Tuple[List[Tuple], RefinementSummary]:
    refined_results: List[Tuple] = []
    summary = RefinementSummary()

    for result in condition_results:
        block, grid, plan, candidates, records, bindings, unresolved, metadata = result
        refined_records: List[ConditionalFactRecord] = []
        record_map: Dict[str, List[str]] = {}
        local_unresolved = list(unresolved)
        for record in records:
            new_records, events, extra_unresolved = refine_record(record, metadata=metadata)
            refined_records.extend(new_records)
            record_map[record.record_id] = [item.record_id for item in new_records]
            summary.events.extend(events)
            summary.unresolved.extend(extra_unresolved)
            local_unresolved.extend(extra_unresolved)
        refined_records, causal_events = _repair_causal_response_records(block, refined_records)
        summary.events.extend(causal_events)
        refined_records, grouped_events = _recover_grouped_property_condition_matrix(block, grid, plan, refined_records)
        summary.events.extend(grouped_events)
        refined_records, formulation_events = _recover_row_level_formulation_matrix(block, grid, plan, refined_records)
        summary.events.extend(formulation_events)
        refined_bindings = _clone_bindings(bindings, record_map)
        refined_results.append((
            block, grid, plan, candidates, refined_records, refined_bindings, local_unresolved, metadata
        ))
    return refined_results, summary


__all__ = [
    "RefinementEvent",
    "RefinementSummary",
    "refine_record",
    "refine_condition_results",
]
