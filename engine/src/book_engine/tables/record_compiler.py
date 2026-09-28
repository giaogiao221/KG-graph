from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from statistics import mean
from typing import Dict, List, Optional, Sequence, Tuple

from book_engine.core.schemas import (
    AxisRoleDecision,
    CellRoleDecision,
    ConditionBinding,
    ConditionCandidate,
    ConditionalFactRecord,
    HeaderTree,
    SourceLocation,
    TableBlock,
    TableGrid,
    TableSemanticPlan,
)
from book_engine.tables.condition_scope_resolver import bind_conditions
from book_engine.tables.context_metadata_extractor import ContextMetadata, extract_context_metadata
from book_engine.tables.semantic_features import data_start_row, looks_like_material_value
from book_engine.tables.value_parser import parse_value

_UNIT_IN_LABEL_RE = re.compile(r"(?:/\s*|[（(]\s*)(%|‰|℃|°C|K|Pa|kPa|MPa|GPa|bar|g/cm(?:3|³)|kg/m(?:3|³)|V|mV|kV|A|mA|nm|μm|um|mm|cm|m|ns|μs|us|ms|s|min|h|d|Hz|kHz|MHz|rpm|r/min|K/min|℃/min|°C/min|mm/s|cm/s|m/s|km/s|J/g|kJ/kg|kJ/mol)\s*[）)]?\s*$")
_GENERIC_COLUMN_HEADERS = {
    "数值", "结果", "指标", "理化指标", "性能", "属性", "项目", "测试结果", "试验结果", "实验结果",
    "检验结果", "测定值", "实测值", "计算值", "理论值", "备注", "说明", "方法", "检验方法",
}

# Energetic-material tables produced by PDF-to-HTML conversion sometimes lose
# every line break inside a rowspan cell.  A row such as
# ``TNT / RDX / HMX / ...`` then becomes ``TNTRDXHMX...``, while parallel
# numeric cells become ``1.651.811.96...``.  The following recovery is
# deliberately closed-set and count-bound: it activates only when a recognised
# abbreviation column and at least two numeric property columns can be aligned
# to exactly the same number of entities.
_COLLAPSED_CODE_HEADER_RE = re.compile(r"(?:缩写|简称|代号|英文缩写|材料代码|材料代号)", re.I)
_COLLAPSED_MATERIAL_CODES = tuple(sorted({
    "CL-20(HNIW)", "HMX(β)", "HMX(α)", "PBXN-109", "PBX-9501", "PBX-9502",
    "CL-20", "FOX-7", "TNAZ", "PETN", "TATB", "RDX", "HMX", "TNT", "NTO",
    "ADN", "ONC", "NIGU", "GAP", "HTPB", "BAMO", "AMMO", "NIMMO", "NEPE",
    "CMDB", "AP", "AN", "NG", "NC", "Al",
}, key=len, reverse=True))
_COLLAPSED_PREFIX_RE = re.compile(r"^(?:现用|新型?|常用|一些)?(?:含能)?(?:材料|炸药|推进剂|火药|药剂)+", re.I)
_NUMERIC_SEPARATOR_RE = re.compile(r"[\s,，;；|/]+")



def _tokenize_collapsed_material_codes(text: str) -> List[str]:
    value = (text or "").replace("－", "-").replace("—", "-")
    value = re.sub(r"\s+", "", value)
    value = _COLLAPSED_PREFIX_RE.sub("", value)
    value = value.strip("：:、,，;；/|")
    if not value:
        return []

    upper = value.upper()

    @lru_cache(maxsize=None)
    def parse(position: int):
        while position < len(value) and value[position] in "、,，;；/|":
            position += 1
        if position == len(value):
            return ()
        for token in _COLLAPSED_MATERIAL_CODES:
            token_upper = token.upper()
            if upper.startswith(token_upper, position):
                tail = parse(position + len(token))
                if tail is not None:
                    surface = value[position:position + len(token)]
                    return (surface,) + tail
        return None

    tokens = parse(0)
    return list(tokens) if tokens and len(tokens) >= 2 else []


def _number_plausibility(value: float, property_name: str, token: str) -> float:
    prop = property_name or ""
    score = 0.0
    if "." in token:
        score += 0.8
    if "密度" in prop:
        if 0.2 <= value <= 5.0:
            score += 4.0
        else:
            score -= 6.0
    elif "氧平衡" in prop:
        if -250 <= value <= 100:
            score += 3.0
        else:
            score -= 5.0
    elif any(term in prop for term in ("生成能", "生成热", "生成焓", "能量")):
        if -5000 <= value <= 5000:
            score += 2.0
        else:
            score -= 5.0
    elif abs(value) <= 1_000_000:
        score += 0.5
    if token.startswith(("+", "-")):
        score += 0.1
    return score


def _split_collapsed_numeric_series(text: str, expected_count: int, property_name: str = "") -> List[str]:
    if expected_count <= 0:
        return []
    value = (text or "").strip().replace("−", "-").replace("—", "-")
    value = re.sub(r"(?i)(?:g/cm(?:3|³)|kg/m(?:3|³)|kJ/mol|kJ/kg|J/g|%|‰|ppm|ppb|vol%|wt%)", "", value)
    value = value.replace("．", ".")
    if not value:
        return []

    # Fast path for properly separated values.
    explicit = [item for item in _NUMERIC_SEPARATOR_RE.split(value) if item]
    if len(explicit) == expected_count and all(re.fullmatch(r"[+-]?\d+(?:\.\d+)?", item) for item in explicit):
        return explicit

    compact = _NUMERIC_SEPARATOR_RE.sub("", value)
    if not compact:
        return []

    @lru_cache(maxsize=None)
    def solve(position: int, remaining: int):
        if remaining == 0:
            return (0.0, ()) if position == len(compact) else None
        if position >= len(compact):
            return None
        sign_end = position + 1 if compact[position] in "+-" else position
        if sign_end >= len(compact) or not compact[sign_end].isdigit():
            return None
        digit_end = sign_end
        while digit_end < len(compact) and compact[digit_end].isdigit() and digit_end - sign_end < 5:
            digit_end += 1
        candidates = []
        # Integer candidates.
        for end in range(sign_end + 1, digit_end + 1):
            candidates.append(end)
        # Decimal candidates, limiting fractional precision to four digits.
        for int_end in range(sign_end + 1, digit_end + 1):
            if int_end < len(compact) and compact[int_end] == ".":
                frac_end = int_end + 1
                while frac_end < len(compact) and compact[frac_end].isdigit() and frac_end - int_end <= 4:
                    frac_end += 1
                    candidates.append(frac_end)
        best = None
        for end in candidates:
            token = compact[position:end]
            if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", token):
                continue
            try:
                numeric = float(token)
            except ValueError:
                continue
            tail = solve(end, remaining - 1)
            if tail is None:
                continue
            score = _number_plausibility(numeric, property_name, token) + tail[0]
            # Prefer a balanced segmentation over consuming many digits in one token.
            if "." in token:
                frac_len = len(token.rsplit(".", 1)[1])
                score -= max(0, frac_len - 2) * 0.25
            if best is None or score > best[0]:
                best = (score, (token,) + tail[1])
        return best

    result = solve(0, expected_count)
    if result is None or result[0] < -1.0:
        return []
    return list(result[1])


def _recover_collapsed_row_series(
    block: TableBlock,
    grid: TableGrid,
    header_tree: HeaderTree,
    plan: TableSemanticPlan,
    cell_roles: Sequence[CellRoleDecision],
    candidates: Sequence[ConditionCandidate],
) -> Tuple[List[ConditionalFactRecord], List[ConditionBinding], List[Dict[str, object]]]:
    if plan.orientation != "row_subject" or plan.topology not in {"entity_by_property", "composition_and_performance"}:
        return [], [], []
    code_columns = [
        column for column in range(grid.column_count)
        if _COLLAPSED_CODE_HEADER_RE.search(_header_label(grid, header_tree, column) or "")
    ]
    if not code_columns:
        return [], [], []
    output_payloads = _result_payloads_for_plan(plan)
    if len(output_payloads) < 2:
        return [], [], []

    role_map = _cell_role_map(cell_roles)
    records: List[ConditionalFactRecord] = []
    bindings: List[ConditionBinding] = []
    unresolved: List[Dict[str, object]] = []
    start_row = data_start_row(header_tree)

    for row in range(start_row, grid.row_count):
        recovered_group = False
        for code_column in code_columns:
            code_cell = grid.cells[row][code_column]
            if code_cell.is_span_copy:
                continue
            subjects = _tokenize_collapsed_material_codes(code_cell.normalized_text)
            if not (2 <= len(subjects) <= 20):
                continue
            group_end = min(grid.row_count, row + max(1, int(code_cell.rowspan or 1)))
            aligned: Dict[int, Tuple[str, str, List[str]]] = {}
            for column, payload in output_payloads.items():
                label = _label_from_payload(payload) or " / ".join(_path_for_column(header_tree, column))
                unit = str(payload.get("unit", "")) or _extract_unit_from_label(label)
                property_name = _clean_property_name(label, unit)
                pieces: List[str] = []
                seen_origins = set()
                for source_row in range(row, group_end):
                    cell = grid.cells[source_row][column]
                    origin = (cell.origin_row if cell.origin_row is not None else source_row,
                              cell.origin_column if cell.origin_column is not None else column)
                    if origin in seen_origins:
                        continue
                    seen_origins.add(origin)
                    if cell.normalized_text:
                        pieces.append(cell.normalized_text)
                values = _split_collapsed_numeric_series(";".join(pieces), len(subjects), property_name)
                if len(values) == len(subjects):
                    aligned[column] = (property_name, unit, values)
            if len(aligned) < 2:
                continue

            for subject_index, subject in enumerate(subjects):
                for column, (property_name, unit, values) in aligned.items():
                    value_text = values[subject_index]
                    role_decision = role_map.get((row, column))
                    record = _make_record(
                        block=block,
                        plan=plan,
                        row=row,
                        column=column,
                        subject=subject,
                        subject_type="material_or_formulation_candidate",
                        subject_source="collapsed_material_code_sequence_recovery_phase104",
                        sample_id=subject,
                        property_name=property_name,
                        property_role="property",
                        value_text=value_text,
                        value_role=_value_role(role_decision.role if role_decision else "measured_result"),
                        method="",
                        instrument="",
                        conditions=[],
                        row_path=[code_cell.normalized_text],
                        column_path=_path_for_column(header_tree, column),
                        confidence_parts=[plan.confidence, 0.94, 0.88],
                        unresolved_reasons=[],
                        unit_hint=unit,
                    )
                    digest = hashlib.sha1(
                        f"{block.table_id}|{row}|{column}|{subject}|{property_name}|{value_text}|phase104".encode("utf-8")
                    ).hexdigest()[:10]
                    record.record_id = f"{block.table_id}-R{row:04d}-C{column:04d}-S{subject_index + 1:02d}-{digest}"
                    record.confidence = min(record.confidence, 0.88)
                    records.append(record)
            recovered_group = True
            unresolved.append({
                "table_id": block.table_id,
                "row_index": row,
                "column_index": code_column,
                "reason": "collapsed_row_series_recovered_phase104",
                "value": code_cell.normalized_text,
            })
            break
        if recovered_group:
            continue
    return records, bindings, unresolved


def _axis_indices(items: Sequence[Dict[str, object]]) -> List[int]:
    result: List[int] = []
    for item in items:
        try:
            value = int(item["index"])
        except (KeyError, TypeError, ValueError):
            continue
        if value not in result:
            result.append(value)
    return result


def _column_payload_map(items: Sequence[Dict[str, object]]) -> Dict[int, Dict[str, object]]:
    result: Dict[int, Dict[str, object]] = {}
    for item in items:
        try:
            result[int(item["index"])] = item
        except (KeyError, TypeError, ValueError):
            continue
    return result


def _payload_is_condition_like(payload: Dict[str, object]) -> bool:
    reasons = {str(value) for value in payload.get("reasons", [])}
    return "header_lexicon:condition" in reasons


def _result_payloads_for_plan(plan: TableSemanticPlan) -> Dict[int, Dict[str, object]]:
    all_property_payloads = _column_payload_map(plan.property_axes)
    value_payloads = _column_payload_map(plan.value_axes)
    blocked_condition_indices = set()
    property_payloads = dict(all_property_payloads)
    if plan.topology == "composition_and_performance":
        blocked_condition_indices = {
            index for index, payload in all_property_payloads.items()
            if _payload_is_condition_like(payload)
        }
        property_payloads = {
            index: payload for index, payload in all_property_payloads.items()
            if index not in blocked_condition_indices
        }
    result = dict(property_payloads)
    for index, payload in value_payloads.items():
        label = _label_from_payload(payload)
        if index in blocked_condition_indices or not label:
            continue
        result.setdefault(index, payload)
    return result


def _label_from_payload(payload: Dict[str, object]) -> str:
    labels = [str(value).strip() for value in payload.get("label_path", []) if str(value).strip()]
    return " / ".join(labels)


def _normalize_label_punctuation(label: str) -> str:
    return (label or "").replace("／", "/").replace("（", "(").replace("）", ")")


def _extract_unit_from_label(label: str) -> str:
    value = _normalize_label_punctuation(label)
    compact = re.sub(r"\s+", "", value).replace("−", "-")
    if re.search(r"(?:^|/)(?:M(?:w|n|z)?)/(?:M(?:w|n|z)?)$", compact, re.IGNORECASE):
        return ""
    # OCR frequently turns the slash in ``密度/g·cm^-3`` into ``1`` or ``l``.
    if re.search(r"(?:^|[^A-Za-z])(?:1|l)?g(?:·|\*)?cm\^?-?3$", compact, re.I):
        return "g/cm3"
    if re.search(r"kJ(?:·|\*)?mol\^?-?1$", compact, re.I):
        return "kJ/mol"
    match = _UNIT_IN_LABEL_RE.search(value)
    if match:
        return match.group(1)
    # OCR often yields '/(J/g)' or '/（kJ/kg)' after another qualifier parenthesis.
    tail = re.search(r"/\s*\(([^()]{1,20})\)\s*$", value)
    if tail:
        candidate = tail.group(1).strip()
        if re.search(r"[%‰A-Za-z°℃·/^0-9-]", candidate):
            return candidate
    return ""


def _clean_property_name(label: str, unit: str = "") -> str:
    value = re.sub(r"\s+", " ", _normalize_label_punctuation(label)).strip()
    effective_unit = unit or _extract_unit_from_label(value)
    if effective_unit:
        patterns = (
            rf"\s*/\s*{re.escape(effective_unit)}\s*$",
            rf"\s*/?\s*\(\s*{re.escape(effective_unit)}\s*\)\s*$",
        )
        for pattern in patterns:
            value = re.sub(pattern, "", value, flags=re.IGNORECASE)
        if effective_unit == "g/cm3":
            value = re.sub(r"(?:1|l)?g(?:·|\*)?cm\^?-?3\s*$", "", value, flags=re.I)
        elif effective_unit == "kJ/mol":
            value = re.sub(r"kJ(?:·|\*)?mol\^?-?1\s*$", "", value, flags=re.I)
    # Qualifiers already represented as bound conditions should not remain in the property name.
    value = re.sub(r"\((?:密度|温度|压力|粒度|粒径)相同\)", "", value)
    return value.strip(" /：:") or "未命名属性"


def _path_for_column(header_tree: HeaderTree, column: int) -> List[str]:
    path = next((item for item in header_tree.column_paths if item.index == column), None)
    return list(path.labels) if path else []


def _path_for_row(header_tree: HeaderTree, row: int) -> List[str]:
    path = next((item for item in header_tree.row_paths if item.index == row), None)
    return list(path.labels) if path else []


def _header_label(grid: TableGrid, header_tree: HeaderTree, column: int) -> str:
    path = _path_for_column(header_tree, column)
    if path:
        return path[-1]
    for row in sorted(header_tree.header_rows, reverse=True):
        text = grid.cells[row][column].normalized_text
        if text:
            return text
    return ""


def _cell_role_map(cell_roles: Sequence[CellRoleDecision]) -> Dict[Tuple[int, int], CellRoleDecision]:
    return {(item.row_index, item.column_index): item for item in cell_roles}


def _column_role_map(axis_roles: Sequence[AxisRoleDecision]) -> Dict[int, AxisRoleDecision]:
    return {item.index: item for item in axis_roles if item.axis == "column"}


def _record_id(table_id: str, row: int, column: int, property_name: str, value_text: str) -> str:
    digest = hashlib.sha1(f"{table_id}|{row}|{column}|{property_name}|{value_text}".encode("utf-8")).hexdigest()[:10]
    return f"{table_id}-R{row:04d}-C{column:04d}-{digest}"


def _source(block: TableBlock, row: int, column: int) -> SourceLocation:
    return SourceLocation(
        source_path="",
        block_id=block.table_id,
        table_id=block.table_id,
        row_index=row,
        column_index=column,
        line_start=block.line_start,
        line_end=block.line_end,
    )


def _value_role(cell_role: str) -> str:
    mapping = {
        "measured_result": "measured_result",
        "calculated_result": "calculated_result",
        "theoretical_result": "theoretical_value",
        "comparison_metric": "comparison_metric",
        "qualitative_result": "qualitative_description",
        "formulation_component_value": "formulation_component",
        "condition_value": "condition_value",
    }
    return mapping.get(cell_role, cell_role or "unknown")


def _specific_header_subject(label: str) -> bool:
    value = re.sub(r"\s+", " ", label or "").strip()
    if not value or value in _GENERIC_COLUMN_HEADERS or len(value) > 60:
        return False
    return looks_like_material_value(value)


def _detect_property_row_layout(
    grid: TableGrid,
    header_tree: HeaderTree,
    plan: TableSemanticPlan,
    axis_roles: Sequence[AxisRoleDecision],
    metadata: ContextMetadata,
) -> Tuple[str, Optional[int], List[int]]:
    """Detect tables where rows are properties and columns carry values or subjects.

    Returns: (mode, property_column, data_columns)
    mode is one of: "", "multi_subject", "context_subject".
    """
    column_roles = _column_role_map(axis_roles)
    explicit_subject_columns = set(_axis_indices(plan.subject_axes) + _axis_indices(plan.identifier_axes))
    method_columns = set(_axis_indices(plan.method_axes))
    condition_columns = set(_axis_indices(plan.condition_axes))
    note_columns = set(_axis_indices(plan.note_axes) + _axis_indices(plan.group_axes))

    property_name_columns = [
        index
        for index, decision in column_roles.items()
        if decision.role == "property" and decision.numeric_ratio <= 0.35
    ]
    if len(property_name_columns) != 1 or explicit_subject_columns:
        return "", None, []
    property_column = property_name_columns[0]

    excluded = {property_column} | method_columns | condition_columns | note_columns | explicit_subject_columns
    data_columns = [column for column in range(grid.column_count) if column not in excluded]
    data_columns = [column for column in data_columns if _header_label(grid, header_tree, column)]
    if not data_columns:
        return "", None, []

    specific_subject_columns = [
        column for column in data_columns if _specific_header_subject(_header_label(grid, header_tree, column))
    ]
    if len(specific_subject_columns) >= 2:
        return "multi_subject", property_column, specific_subject_columns

    # Attribute-value(-method) tables use the local material/formulation context as subject.
    value_like_columns = [
        column
        for column in data_columns
        if column_roles.get(column) and column_roles[column].role in {"property_value", "value", "composition"}
    ]
    if metadata.subject and value_like_columns:
        return "context_subject", property_column, value_like_columns
    return "", None, []


def _status_and_reasons(
    plan: TableSemanticPlan,
    subject_source: str,
    condition_unresolved: Sequence[str],
) -> Tuple[str, List[str]]:
    reasons = list(condition_unresolved)
    status = "ready"
    if subject_source.startswith("context:"):
        status = "candidate"
        reasons.append("subject_from_context_requires_registry_confirmation")
    if plan.confidence < 0.6:
        status = "candidate"
        reasons.append("table_plan_low_confidence")
    if condition_unresolved:
        status = "unresolved"
    return status, sorted(set(reasons))


def _make_record(
    *,
    block: TableBlock,
    plan: TableSemanticPlan,
    row: int,
    column: int,
    subject: str,
    subject_type: str,
    subject_source: str,
    sample_id: str,
    property_name: str,
    property_role: str,
    value_text: str,
    value_role: str,
    method: str,
    instrument: str,
    conditions,
    row_path: List[str],
    column_path: List[str],
    confidence_parts: Sequence[float],
    unresolved_reasons: Sequence[str],
    unit_hint: str = "",
) -> ConditionalFactRecord:
    parsed = parse_value(value_text, unit_hint)
    rid = _record_id(block.table_id, row, column, property_name, value_text)
    confidence_values = [value for value in confidence_parts if value is not None]
    confidence_values.extend(atom.confidence for atom in conditions)
    confidence = mean(confidence_values) if confidence_values else 0.5
    status, reasons = _status_and_reasons(plan, subject_source, unresolved_reasons)
    if property_name == "未命名属性":
        status = "unresolved"
        reasons = sorted(set(reasons + ["property_name_unresolved"]))
    return ConditionalFactRecord(
        record_id=rid,
        table_id=block.table_id,
        row_index=row,
        column_index=column,
        subject=subject,
        subject_type=subject_type,
        subject_source=subject_source,
        sample_id=sample_id,
        property_name=property_name,
        property_role=property_role,
        value_text=value_text,
        normalized_value_text=parsed.normalized_text,
        unit=parsed.unit or unit_hint,
        value_num=parsed.value_num,
        lower_bound=parsed.lower_bound,
        upper_bound=parsed.upper_bound,
        comparator=parsed.comparator,
        value_role=value_role,
        method=method,
        instrument=instrument,
        conditions=list(conditions),
        row_header_path=row_path,
        column_header_path=column_path,
        # The authoritative evidence for every table-derived fact is the exact
        # original table block from the Markdown source. Local row/column evidence
        # is written separately by table_evidence_writer.
        evidence=block.raw_text,
        confidence=min(0.98, confidence),
        source=_source(block, row, column),
        record_status=status,
        unresolved_reasons=reasons,
    )


def _compile_property_row_records(
    block: TableBlock,
    grid: TableGrid,
    header_tree: HeaderTree,
    plan: TableSemanticPlan,
    axis_roles: Sequence[AxisRoleDecision],
    cell_roles: Sequence[CellRoleDecision],
    candidates: Sequence[ConditionCandidate],
    metadata: ContextMetadata,
    mode: str,
    property_column: int,
    data_columns: Sequence[int],
) -> Tuple[List[ConditionalFactRecord], List[ConditionBinding], List[Dict[str, object]]]:
    records: List[ConditionalFactRecord] = []
    bindings: List[ConditionBinding] = []
    unresolved: List[Dict[str, object]] = []
    role_map = _cell_role_map(cell_roles)
    method_columns = _axis_indices(plan.method_axes)
    start_row = data_start_row(header_tree)

    for row in range(start_row, grid.row_count):
        raw_property = grid.cells[row][property_column].normalized_text
        if not raw_property:
            continue
        property_unit = _extract_unit_from_label(raw_property)
        property_name = _clean_property_name(raw_property, property_unit)
        row_method = " / ".join(
            grid.cells[row][column].normalized_text
            for column in method_columns
            if grid.cells[row][column].normalized_text
        ) or metadata.method

        for column in data_columns:
            value_text = grid.cells[row][column].normalized_text
            if not value_text:
                continue
            role_decision = role_map.get((row, column))
            if role_decision and role_decision.role in {"header", "empty", "unresolved_numeric"}:
                if role_decision.role == "unresolved_numeric":
                    unresolved.append({
                        "table_id": block.table_id, "row_index": row, "column_index": column,
                        "reason": "unresolved_numeric_cell", "value": value_text,
                    })
                continue

            if mode == "multi_subject":
                subject = _header_label(grid, header_tree, column)
                subject_source = "column_header"
                subject_confidence = 0.9
                subject_type = "material_or_formulation_candidate"
                sample_id = subject
            else:
                subject = metadata.subject
                subject_source = f"context:{metadata.subject_source}"
                subject_confidence = metadata.subject_confidence
                subject_type = "material_or_formulation_candidate"
                sample_id = ""
            if not subject:
                unresolved.append({
                    "table_id": block.table_id, "row_index": row, "column_index": column,
                    "reason": "no_subject_for_property_row", "value": value_text,
                })
                continue

            rid = _record_id(block.table_id, row, column, property_name, value_text)
            bound, row_bindings, condition_unresolved = bind_conditions(
                candidates,
                rid,
                block.table_id,
                row,
                column,
                include_composition=False,
            )
            bindings.extend(row_bindings)
            cell_role = role_decision.role if role_decision else "measured_result"
            records.append(
                _make_record(
                    block=block,
                    plan=plan,
                    row=row,
                    column=column,
                    subject=subject,
                    subject_type=subject_type,
                    subject_source=subject_source,
                    sample_id=sample_id,
                    property_name=property_name,
                    property_role="property",
                    value_text=value_text,
                    value_role=_value_role(cell_role),
                    method=row_method,
                    instrument=metadata.instrument,
                    conditions=bound,
                    row_path=[raw_property],
                    column_path=_path_for_column(header_tree, column),
                    confidence_parts=[plan.confidence, subject_confidence, role_decision.confidence if role_decision else 0.75],
                    unresolved_reasons=condition_unresolved,
                    unit_hint=property_unit,
                )
            )
    return records, bindings, unresolved


def _subject_from_row(
    grid: TableGrid,
    row: int,
    header_tree: HeaderTree,
    plan: TableSemanticPlan,
    metadata: ContextMetadata,
) -> Tuple[str, str, str, float]:
    subject_columns = _axis_indices(plan.subject_axes)
    identifier_columns = _axis_indices(plan.identifier_axes)
    subject_values = [grid.cells[row][col].normalized_text for col in subject_columns if grid.cells[row][col].normalized_text]
    identifier_values = [grid.cells[row][col].normalized_text for col in identifier_columns if grid.cells[row][col].normalized_text]
    sample_id = " / ".join(identifier_values)
    if subject_values:
        return " / ".join(subject_values), "table_entity_axis", sample_id, 0.94
    if identifier_values:
        return sample_id, "table_identifier_axis", sample_id, 0.9
    row_headers = [
        grid.cells[row][col].normalized_text
        for col in header_tree.row_header_columns
        if grid.cells[row][col].normalized_text
    ]
    if row_headers:
        value = " / ".join(row_headers)
        return value, "row_header", sample_id or value, 0.84
    if metadata.subject:
        return metadata.subject, f"context:{metadata.subject_source}", sample_id, metadata.subject_confidence
    return "", "", sample_id, 0.0


def _compile_row_subject_records(
    block: TableBlock,
    grid: TableGrid,
    header_tree: HeaderTree,
    plan: TableSemanticPlan,
    cell_roles: Sequence[CellRoleDecision],
    candidates: Sequence[ConditionCandidate],
    metadata: ContextMetadata,
) -> Tuple[List[ConditionalFactRecord], List[ConditionBinding], List[Dict[str, object]]]:
    records: List[ConditionalFactRecord] = []
    bindings: List[ConditionBinding] = []
    unresolved: List[Dict[str, object]] = []
    role_map = _cell_role_map(cell_roles)
    start_row = data_start_row(header_tree)
    result_payloads = _result_payloads_for_plan(plan)
    composition_payloads = _column_payload_map(plan.composition_axes)
    method_columns = _axis_indices(plan.method_axes)
    emit_composition_records = plan.topology == "formulation_matrix" and not result_payloads
    output_payloads = dict(result_payloads)
    if emit_composition_records:
        output_payloads.update(composition_payloads)

    for row in range(start_row, grid.row_count):
        subject, subject_source, sample_id, subject_confidence = _subject_from_row(grid, row, header_tree, plan, metadata)
        row_method = " / ".join(
            grid.cells[row][column].normalized_text
            for column in method_columns
            if grid.cells[row][column].normalized_text
        ) or metadata.method
        if not subject:
            unresolved.append({"table_id": block.table_id, "row_index": row, "column_index": "", "reason": "no_subject_for_row", "value": ""})
            continue

        for column in sorted(output_payloads):
            value_text = grid.cells[row][column].normalized_text
            if not value_text:
                continue
            role_decision = role_map.get((row, column))
            cell_role = role_decision.role if role_decision else ""
            if cell_role in {"header", "empty", "unresolved_numeric", "condition_value", "method_name", "identifier", "subject_value"}:
                if cell_role == "unresolved_numeric":
                    unresolved.append({"table_id": block.table_id, "row_index": row, "column_index": column, "reason": "unresolved_numeric_cell", "value": value_text})
                continue

            payload = output_payloads.get(column) or {}
            label = _label_from_payload(payload) or " / ".join(_path_for_column(header_tree, column))
            unit = str(payload.get("unit", ""))
            property_name = _clean_property_name(label, unit)
            rid = _record_id(block.table_id, row, column, property_name, value_text)
            is_composition_record = emit_composition_records and column in composition_payloads
            include_composition = not is_composition_record
            bound, row_bindings, condition_unresolved = bind_conditions(
                candidates, rid, block.table_id, row, column, include_composition=include_composition
            )
            bindings.extend(row_bindings)
            records.append(
                _make_record(
                    block=block,
                    plan=plan,
                    row=row,
                    column=column,
                    subject=subject,
                    subject_type=("formulation_or_sample" if sample_id and subject == sample_id else "material_or_formulation_candidate"),
                    subject_source=subject_source,
                    sample_id=sample_id,
                    property_name=property_name,
                    property_role=("composition" if is_composition_record else "property"),
                    value_text=value_text,
                    value_role=("formulation_component" if is_composition_record else _value_role(cell_role)),
                    method=row_method,
                    instrument=metadata.instrument,
                    conditions=bound,
                    row_path=_path_for_row(header_tree, row),
                    column_path=_path_for_column(header_tree, column),
                    confidence_parts=[plan.confidence, subject_confidence, role_decision.confidence if role_decision else 0.65],
                    unresolved_reasons=condition_unresolved,
                    unit_hint=unit,
                )
            )
    return records, bindings, unresolved


def _compile_column_subject_records(
    block: TableBlock,
    grid: TableGrid,
    header_tree: HeaderTree,
    plan: TableSemanticPlan,
    cell_roles: Sequence[CellRoleDecision],
    candidates: Sequence[ConditionCandidate],
    metadata: ContextMetadata,
) -> Tuple[List[ConditionalFactRecord], List[ConditionBinding], List[Dict[str, object]]]:
    records: List[ConditionalFactRecord] = []
    bindings: List[ConditionBinding] = []
    unresolved: List[Dict[str, object]] = []
    role_map = _cell_role_map(cell_roles)
    start_row = data_start_row(header_tree)
    row_header_columns = set(header_tree.row_header_columns)
    data_columns = [column for column in range(grid.column_count) if column not in row_header_columns]

    for row in range(start_row, grid.row_count):
        row_path = _path_for_row(header_tree, row)
        property_label = " / ".join(row_path) or " / ".join(
            grid.cells[row][column].normalized_text
            for column in header_tree.row_header_columns
            if grid.cells[row][column].normalized_text
        )
        if not property_label:
            unresolved.append({"table_id": block.table_id, "row_index": row, "column_index": "", "reason": "no_property_row_label", "value": ""})
            continue
        unit = _extract_unit_from_label(property_label)
        property_name = _clean_property_name(property_label, unit)

        for column in data_columns:
            subject = _header_label(grid, header_tree, column)
            value_text = grid.cells[row][column].normalized_text
            if not subject or not value_text:
                continue
            role_decision = role_map.get((row, column))
            cell_role = role_decision.role if role_decision else ""
            if cell_role in {"header", "empty", "unresolved_numeric"}:
                if cell_role == "unresolved_numeric":
                    unresolved.append({"table_id": block.table_id, "row_index": row, "column_index": column, "reason": "unresolved_numeric_cell", "value": value_text})
                continue
            rid = _record_id(block.table_id, row, column, property_name, value_text)
            bound, row_bindings, condition_unresolved = bind_conditions(
                candidates, rid, block.table_id, row, column, include_composition=False
            )
            bindings.extend(row_bindings)
            records.append(
                _make_record(
                    block=block,
                    plan=plan,
                    row=row,
                    column=column,
                    subject=subject,
                    subject_type="material_or_formulation_candidate",
                    subject_source="column_header",
                    sample_id=subject,
                    property_name=property_name,
                    property_role="property",
                    value_text=value_text,
                    value_role=_value_role(cell_role),
                    method=metadata.method,
                    instrument=metadata.instrument,
                    conditions=bound,
                    row_path=row_path,
                    column_path=_path_for_column(header_tree, column),
                    confidence_parts=[plan.confidence, role_decision.confidence if role_decision else 0.65],
                    unresolved_reasons=condition_unresolved,
                    unit_hint=unit,
                )
            )
    return records, bindings, unresolved


def compile_conditional_records(
    block: TableBlock,
    grid: TableGrid,
    header_tree: HeaderTree,
    plan: TableSemanticPlan,
    axis_roles: Sequence[AxisRoleDecision],
    cell_roles: Sequence[CellRoleDecision],
    candidates: Sequence[ConditionCandidate],
) -> Tuple[List[ConditionalFactRecord], List[ConditionBinding], List[Dict[str, object]], ContextMetadata]:
    metadata = extract_context_metadata(block)
    if plan.topology == "pairwise_compatibility":
        return [], [], [{"table_id": block.table_id, "row_index": "", "column_index": "", "reason": "pairwise_topology_deferred", "value": ""}], metadata

    recovered_records, recovered_bindings, recovered_unresolved = _recover_collapsed_row_series(
        block, grid, header_tree, plan, cell_roles, candidates
    )

    layout_mode, property_column, data_columns = _detect_property_row_layout(
        grid, header_tree, plan, axis_roles, metadata
    )
    if layout_mode and property_column is not None:
        records, bindings, unresolved = _compile_property_row_records(
            block, grid, header_tree, plan, axis_roles, cell_roles, candidates, metadata,
            layout_mode, property_column, data_columns,
        )
    elif plan.orientation == "column_subject":
        records, bindings, unresolved = _compile_column_subject_records(
            block, grid, header_tree, plan, cell_roles, candidates, metadata
        )
    else:
        records, bindings, unresolved = _compile_row_subject_records(
            block, grid, header_tree, plan, cell_roles, candidates, metadata
        )

    records = list(recovered_records) + list(records)
    bindings = list(recovered_bindings) + list(bindings)
    unresolved = list(recovered_unresolved) + list(unresolved)

    unique_records: List[ConditionalFactRecord] = []
    seen = set()
    for record in records:
        key = (record.subject, record.property_name, record.value_text, record.row_index, record.column_index)
        if key in seen:
            continue
        seen.add(key)
        unique_records.append(record)
    return unique_records, bindings, unresolved, metadata


__all__ = ["compile_conditional_records"]
