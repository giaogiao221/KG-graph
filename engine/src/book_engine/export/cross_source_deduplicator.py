from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple

_PROPERTY_ALIASES = {
    "相对分子质量": "分子量",
    "CAS登记号": "CAS号",
    "中文名称": "名称",
    "黏度": "粘度",
    "应用领域": "用途",
}
_SOURCE_PRIORITY = {
    "table_conditional_record": 100,
    "text_labeled_field": 90,
    "text_explicit_numeric_property": 80,
    "text_process_step": 88,
    "text_section_narrative": 60,
    "text_composition_description": 55,
}


@dataclass
class DedupAudit:
    cluster_id: str
    kept_fact_id: str
    dropped_fact_id: str
    kept_source: str
    dropped_source: str
    reason: str
    semantic_key: str


def _compact(text: object) -> str:
    value = str(text or "").replace("−", "-").replace("–", "-").replace("—", "-")
    value = re.sub(r"\s+", "", value).casefold()
    return value


def _canonical_property(text: object) -> str:
    value = re.sub(r"\s+", "", str(text or ""))
    return _PROPERTY_ALIASES.get(value, value)


def _decimal_text(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        dec = Decimal(raw.replace(",", "."))
    except InvalidOperation:
        return _compact(raw)
    normalized = format(dec.normalize(), "f")
    return normalized.rstrip("0").rstrip(".") if "." in normalized else normalized


def _canonical_condition(row: Mapping[str, object]) -> str:
    raw_json = str(row.get("structured_condition_json", "") or "").strip()
    if raw_json and raw_json not in {"[]", "{}"}:
        try:
            payload = json.loads(raw_json)
            items = []
            for item in payload if isinstance(payload, list) else [payload]:
                if not isinstance(item, dict):
                    continue
                name = _compact(item.get("normalized_name") or item.get("name"))
                unit = _compact(item.get("unit"))
                if item.get("value_num") is not None:
                    value = _decimal_text(item.get("value_num"))
                elif item.get("lower_bound") is not None or item.get("upper_bound") is not None:
                    value = f"{_decimal_text(item.get('lower_bound'))}~{_decimal_text(item.get('upper_bound'))}"
                else:
                    value = _compact(item.get("value_text"))
                items.append((name, value, unit))
            return json.dumps(sorted(set(items)), ensure_ascii=False, separators=(",", ":"))
        except Exception:
            pass
    return _compact(row.get("条件文本", ""))


def semantic_key(row: Mapping[str, object]) -> str:
    subject = _compact(row.get("主体名称", ""))
    prop = _compact(_canonical_property(row.get("attribute_name", "")))
    unit = _compact(row.get("normalized_unit") or row.get("单位"))
    number = _decimal_text(row.get("normalized_value_num") or row.get("数值"))
    lower = _decimal_text(row.get("范围下限"))
    upper = _decimal_text(row.get("范围上限"))
    if number:
        value = f"num:{number}"
    elif lower or upper:
        value = f"range:{lower}~{upper}"
    else:
        value = "text:" + _compact(row.get("normalized_value_text") or row.get("尾实体/取值文本"))
        if unit and value.endswith(unit):
            value = value[: -len(unit)]
    condition = _canonical_condition(row)
    if str(row.get("事实类型", "") or "") == "工艺步骤事实" or str(row.get("step_id", "") or ""):
        process_scope = "|".join(
            (
                _compact(row.get("process_id", "")),
                _compact(row.get("step_index", "")),
                _compact(row.get("step_action", "")),
                _compact(row.get("step_object", "")),
            )
        )
        return "|".join((subject, prop, value, unit, condition, process_scope))
    return "|".join((subject, prop, value, unit, condition))


def _row_score(row: Mapping[str, object]) -> Tuple[int, float, int, int]:
    source = str(row.get("来源类型", "") or "")
    source_rank = _SOURCE_PRIORITY.get(source, 40)
    try:
        confidence = float(row.get("置信度", 0) or 0)
    except Exception:
        confidence = 0.0
    has_conditions = 1 if str(row.get("条件文本", "") or "").strip() else 0
    evidence_len = len(str(row.get("证据文本", "") or ""))
    return source_rank, confidence, has_conditions, evidence_len


def deduplicate_rows(rows: Sequence[Dict[str, object]]) -> Tuple[List[Dict[str, object]], List[DedupAudit]]:
    groups: Dict[str, List[Dict[str, object]]] = {}
    order: List[str] = []
    for row in rows:
        key = semantic_key(row)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)

    kept: List[Dict[str, object]] = []
    audits: List[DedupAudit] = []
    for key in order:
        group = groups[key]
        winner = max(group, key=_row_score)
        kept.append(winner)
        cluster = "dup:" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        for row in group:
            if row is winner:
                continue
            audits.append(DedupAudit(
                cluster_id=cluster,
                kept_fact_id=str(winner.get("fact_id", "")),
                dropped_fact_id=str(row.get("fact_id", "")),
                kept_source=str(winner.get("来源类型", "")),
                dropped_source=str(row.get("来源类型", "")),
                reason="same_subject_property_value_unit_and_conditions;higher_source_or_confidence_kept",
                semantic_key=key,
            ))
    return kept, audits


def find_semantic_conflicts(rows: Sequence[Mapping[str, object]]) -> List[Dict[str, object]]:
    groups: Dict[str, Dict[str, List[str]]] = {}
    for row in rows:
        subject = _compact(row.get("主体名称", ""))
        prop = _compact(_canonical_property(row.get("attribute_name", "")))
        condition = _canonical_condition(row)
        base = "|".join((subject, prop, condition))
        full = semantic_key(row)
        value = full.split("|", 2)[-1]
        groups.setdefault(base, {}).setdefault(value, []).append(str(row.get("fact_id", "")))
    conflicts = []
    for base, values in groups.items():
        if len(values) <= 1:
            continue
        conflicts.append({
            "conflict_id": "conf:" + hashlib.sha1(base.encode("utf-8")).hexdigest()[:16],
            "semantic_base": base,
            "distinct_value_count": len(values),
            "values_json": json.dumps(values, ensure_ascii=False, sort_keys=True),
        })
    return conflicts


__all__ = ["DedupAudit", "deduplicate_rows", "find_semantic_conflicts", "semantic_key"]
