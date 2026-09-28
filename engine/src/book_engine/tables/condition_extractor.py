from __future__ import annotations

import re
from dataclasses import replace
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from book_engine.core.schemas import (
    AxisRoleDecision,
    CellRoleDecision,
    ConditionCandidate,
    HeaderTree,
    SourceLocation,
    TableBlock,
    TableGrid,
    TableSemanticPlan,
)
from book_engine.tables.condition_rules import (
    CONDITION_CUES,
    FOLLOWING_NOTE_CUES,
    RESULT_ONLY_TERMS,
    all_aliases,
)
from book_engine.tables.semantic_features import data_start_row
from book_engine.tables.value_parser import parse_value

_VALUE_TOKEN = r"(?:[<>≤≥≈~～±+\-−]?\s*\d+(?:[.,]\d+)?(?:\s*(?:~|～|—|–|至|到|\.\.|±|:|：|/)\s*\d+(?:[.,]\d+)?)?\s*(?:%|‰|℃|°C|K|Pa|kPa|MPa|GPa|bar|mbar|g/cm(?:3|³)|kg/m(?:3|³)|g|kg|mg|μg|ug|nm|μm|um|mm|cm|m|ns|μs|us|ms|s|min|h|d|Hz|kHz|MHz|rpm|r/min|K/min|℃/min|°C/min)?)"
_NONNUM_TOKEN = r"(?:氮气|空气|氧气|氩气|氦气|真空|水|乙醇|甲醇|丙酮|乙酸乙酯|二氯甲烷|氯仿|酸性|碱性|中性)"


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").lower())


def _source_location(block: TableBlock, row: Optional[int] = None, column: Optional[int] = None) -> SourceLocation:
    return SourceLocation(
        source_path="",
        block_id=block.table_id,
        table_id=block.table_id,
        row_index=row,
        column_index=column,
        line_start=block.line_start,
        line_end=block.line_end,
    )


def _condition_cue_ok(text: str, alias: str, match_start: int, match_end: int) -> bool:
    left = text[max(0, match_start - 12):match_start]
    right = text[match_end:min(len(text), match_end + 18)]
    window = left + alias + right
    return any(cue in window for cue in CONDITION_CUES) or bool(re.search(r"\d", right))


def _is_result_only_context(text: str, alias: str, match_start: int) -> bool:
    window = text[max(0, match_start - 8):match_start + len(alias) + 4]
    normalized = _norm(window)
    return any(_norm(term) in normalized for term in RESULT_ONLY_TERMS)


def _extract_after_alias(text: str, alias: str, start: int) -> Optional[Tuple[str, int, int]]:
    tail = text[start + len(alias):]
    pattern = re.compile(
        rf"^\s*(?:为|是|=|：|:|控制在|保持在|设为|设置为|达到|约为|分别为|取)?\s*(?P<value>{_VALUE_TOKEN}|{_NONNUM_TOKEN})",
        re.IGNORECASE,
    )
    match = pattern.search(tail)
    if not match:
        # Handle constructions such as '在100℃下'.
        prefix = text[max(0, start - 16):start]
        before_match = re.search(rf"(?P<value>{_VALUE_TOKEN})\s*(?:条件下|下|时|时测定)?\s*$", prefix, re.IGNORECASE)
        if before_match:
            return before_match.group("value").strip(), max(0, start - 16) + before_match.start("value"), max(0, start - 16) + before_match.end("value")
        return None
    value = match.group("value").strip()
    return value, start + len(alias) + match.start("value"), start + len(alias) + match.end("value")


def _candidate_key(candidate: ConditionCandidate) -> Tuple[str, str, str, Optional[int], Optional[int]]:
    return (
        candidate.normalized_name,
        _norm(candidate.value_text),
        candidate.scope,
        candidate.target_row,
        candidate.target_column,
    )


def _extract_from_text(
    block: TableBlock,
    text: str,
    source_kind: str,
    scope: str,
    priority: int,
    confidence: float,
    target_row: Optional[int] = None,
    target_column: Optional[int] = None,
) -> List[ConditionCandidate]:
    candidates: List[ConditionCandidate] = []
    if not text:
        return candidates
    for rule, alias in all_aliases():
        for match in re.finditer(re.escape(alias), text, re.IGNORECASE):
            if rule.requires_condition_cue and not _condition_cue_ok(text, alias, match.start(), match.end()):
                continue
            if _is_result_only_context(text, alias, match.start()):
                continue
            if alias in {"室温", "常温"}:
                extracted = (alias, match.start(), match.end())
            else:
                extracted = _extract_after_alias(text, alias, match.start())
            if extracted is None:
                continue
            value_text, _, _ = extracted
            parsed = parse_value(value_text)
            candidate = ConditionCandidate(
                condition_id="",
                table_id=block.table_id,
                name=alias,
                normalized_name=rule.canonical_name,
                condition_type=rule.condition_type,
                value_text=value_text,
                unit=parsed.unit,
                value_num=parsed.value_num,
                lower_bound=parsed.lower_bound,
                upper_bound=parsed.upper_bound,
                comparator=parsed.comparator,
                scope=scope,
                source_kind=source_kind,
                source_text=text,
                target_row=target_row,
                target_column=target_column,
                priority=priority,
                confidence=min(0.98, confidence + 0.05 * parsed.parse_confidence),
                reasons=[f"lexical_condition:{rule.canonical_name}", f"source:{source_kind}"],
                source=_source_location(block, target_row, target_column),
            )
            candidates.append(candidate)
    return candidates


def _header_label(path_labels: Sequence[str]) -> str:
    return " / ".join(label for label in path_labels if label)


def _axis_map(axis_roles: List[AxisRoleDecision], axis: str) -> Dict[int, AxisRoleDecision]:
    return {decision.index: decision for decision in axis_roles if decision.axis == axis}


def _condition_axis_candidates(
    block: TableBlock,
    grid: TableGrid,
    header_tree: HeaderTree,
    plan: TableSemanticPlan,
    axis_roles: List[AxisRoleDecision],
) -> List[ConditionCandidate]:
    candidates: List[ConditionCandidate] = []
    column_roles = _axis_map(axis_roles, "column")
    start_row = data_start_row(header_tree)
    condition_columns = {int(payload["index"]): payload for payload in plan.condition_axes}
    for column, payload in condition_columns.items():
        label = _header_label(payload.get("label_path", [])) or "条件"
        header_unit = str(payload.get("unit", ""))
        for row in range(start_row, grid.row_count):
            text = grid.cells[row][column].normalized_text
            if not text:
                continue
            parsed = parse_value(text, header_unit)
            candidates.append(
                ConditionCandidate(
                    condition_id="",
                    table_id=block.table_id,
                    name=label,
                    normalized_name=label,
                    condition_type="axis_condition",
                    value_text=text,
                    unit=parsed.unit or header_unit,
                    value_num=parsed.value_num,
                    lower_bound=parsed.lower_bound,
                    upper_bound=parsed.upper_bound,
                    comparator=parsed.comparator,
                    scope="row",
                    source_kind="condition_axis",
                    source_text=text,
                    target_row=row,
                    target_column=column,
                    priority=320,
                    confidence=max(0.82, column_roles.get(column).confidence if column in column_roles else 0.82),
                    reasons=["semantic_plan_condition_axis"],
                    source=_source_location(block, row, column),
                )
            )
    return candidates


def _composition_axis_candidates(
    block: TableBlock,
    grid: TableGrid,
    header_tree: HeaderTree,
    plan: TableSemanticPlan,
) -> List[ConditionCandidate]:
    candidates: List[ConditionCandidate] = []
    start_row = data_start_row(header_tree)
    for payload in plan.composition_axes:
        column = int(payload["index"])
        if column in set(header_tree.row_header_columns):
            continue
        label = _header_label(payload.get("label_path", [])) or f"component_{column}"
        header_unit = str(payload.get("unit", ""))
        for row in range(start_row, grid.row_count):
            text = grid.cells[row][column].normalized_text
            if not text:
                continue
            parsed = parse_value(text, header_unit)
            candidates.append(
                ConditionCandidate(
                    condition_id="",
                    table_id=block.table_id,
                    name=label,
                    normalized_name=label,
                    condition_type="composition",
                    value_text=text,
                    unit=parsed.unit or header_unit,
                    value_num=parsed.value_num,
                    lower_bound=parsed.lower_bound,
                    upper_bound=parsed.upper_bound,
                    comparator=parsed.comparator,
                    scope="row",
                    source_kind="composition_axis",
                    source_text=text,
                    target_row=row,
                    target_column=column,
                    priority=310,
                    confidence=0.9,
                    reasons=["semantic_plan_composition_axis"],
                    source=_source_location(block, row, column),
                )
            )
    return candidates


def _condition_like_property_candidates(
    block: TableBlock,
    grid: TableGrid,
    header_tree: HeaderTree,
    plan: TableSemanticPlan,
) -> List[ConditionCandidate]:
    if plan.topology not in {"composition_and_performance", "experiment_result"}:
        return []
    if not plan.value_axes:
        return []
    candidates: List[ConditionCandidate] = []
    start_row = data_start_row(header_tree)
    for payload in plan.property_axes:
        reasons = {str(value) for value in payload.get("reasons", [])}
        label = _header_label(payload.get("label_path", []))
        if "header_lexicon:condition" not in reasons:
            continue
        column = int(payload["index"])
        header_unit = str(payload.get("unit", ""))
        for row in range(start_row, grid.row_count):
            text = grid.cells[row][column].normalized_text
            if not text:
                continue
            parsed = parse_value(text, header_unit)
            candidates.append(
                ConditionCandidate(
                    condition_id="", table_id=block.table_id, name=label or f"condition_{column}",
                    normalized_name=label or f"condition_{column}", condition_type="sample_state",
                    value_text=text, unit=parsed.unit or header_unit, value_num=parsed.value_num,
                    lower_bound=parsed.lower_bound, upper_bound=parsed.upper_bound,
                    comparator=parsed.comparator, scope="row", source_kind="condition_like_property_axis",
                    source_text=text, target_row=row, target_column=column, priority=325, confidence=0.88,
                    reasons=["property_axis_reinterpreted_as_condition", "header_lexicon:condition"],
                    source=_source_location(block, row, column),
                )
            )
    return candidates


def _header_embedded_candidates(
    block: TableBlock,
    header_tree: HeaderTree,
) -> List[ConditionCandidate]:
    candidates: List[ConditionCandidate] = []
    for path in header_tree.column_paths:
        label = _header_label(path.labels)
        candidates.extend(
            _extract_from_text(
                block,
                label,
                source_kind="column_header",
                scope="column",
                priority=350,
                confidence=0.82,
                target_column=path.index,
            )
        )
        candidates.extend(_qualitative_condition_candidates(
            block, label, "column_header", "column", 350, 0.82, target_column=path.index
        ))
    for path in header_tree.row_paths:
        label = _header_label(path.labels)
        candidates.extend(
            _extract_from_text(
                block,
                label,
                source_kind="row_header",
                scope="row",
                priority=350,
                confidence=0.82,
                target_row=path.index,
            )
        )
        candidates.extend(_qualitative_condition_candidates(
            block, label, "row_header", "row", 350, 0.82, target_row=path.index
        ))
    return candidates


def _cell_embedded_candidates(
    block: TableBlock,
    grid: TableGrid,
    header_tree: HeaderTree,
    cell_roles: List[CellRoleDecision],
) -> List[ConditionCandidate]:
    candidates: List[ConditionCandidate] = []
    roles = {(item.row_index, item.column_index): item.role for item in cell_roles}
    start_row = data_start_row(header_tree)
    for row in range(start_row, grid.row_count):
        for column in range(grid.column_count):
            role = roles.get((row, column), "")
            if role in {"condition_value", "formulation_component_value", "header", "empty"}:
                continue
            text = grid.cells[row][column].normalized_text
            if not text or not any(mark in text for mark in ("(", "（", "条件", "下", "时")):
                continue
            embedded = _extract_from_text(
                block,
                text,
                source_kind="cell_text",
                scope="cell",
                priority=400,
                confidence=0.78,
                target_row=row,
                target_column=column,
            )
            candidates.extend(embedded)
    return candidates


def _qualitative_condition_candidates(
    block: TableBlock,
    text: str,
    source_kind: str,
    scope: str,
    priority: int,
    confidence: float,
    target_row: Optional[int] = None,
    target_column: Optional[int] = None,
) -> List[ConditionCandidate]:
    patterns = [
        (r"(?:密度相同|相同密度)", "试验密度", "sample_state", "相同"),
        (r"(?:温度相同|相同温度)", "试验温度", "environment", "相同"),
        (r"(?:压力相同|相同压力)", "压力", "environment", "相同"),
        (r"(?:粒度相同|相同粒度|粒径相同|相同粒径)", "粒度", "sample_state", "相同"),
        (r"(?:氮气气氛|氮气保护)", "气氛", "environment", "氮气"),
        (r"(?:空气气氛|空气中)", "气氛", "environment", "空气"),
    ]
    result: List[ConditionCandidate] = []
    for pattern, name, condition_type, value in patterns:
        if not re.search(pattern, text or ""):
            continue
        result.append(
            ConditionCandidate(
                condition_id="", table_id=block.table_id, name=name, normalized_name=name,
                condition_type=condition_type, value_text=value, scope=scope, source_kind=source_kind,
                source_text=text, target_row=target_row, target_column=target_column, priority=priority,
                confidence=confidence, reasons=["qualitative_condition_phrase", f"source:{source_kind}"],
                source=_source_location(block, target_row, target_column),
            )
        )
    return result


def extract_condition_candidates(
    block: TableBlock,
    grid: TableGrid,
    header_tree: HeaderTree,
    plan: TableSemanticPlan,
    axis_roles: List[AxisRoleDecision],
    cell_roles: List[CellRoleDecision],
) -> List[ConditionCandidate]:
    candidates: List[ConditionCandidate] = []
    candidates.extend(
        _extract_from_text(block, block.heading, "heading", "section", 100, 0.62)
    )
    candidates.extend(_qualitative_condition_candidates(block, block.heading, "heading", "section", 100, 0.62))
    candidates.extend(
        _extract_from_text(block, block.preceding_text, "preceding_text", "table", 200, 0.86)
    )
    candidates.extend(_qualitative_condition_candidates(block, block.preceding_text, "preceding_text", "table", 200, 0.86))
    following = block.following_text or ""
    if any(cue in following[:80] for cue in FOLLOWING_NOTE_CUES):
        candidates.extend(
            _extract_from_text(block, following, "following_note", "table", 210, 0.82)
        )
        candidates.extend(_qualitative_condition_candidates(block, following, "following_note", "table", 210, 0.82))
    candidates.extend(_condition_axis_candidates(block, grid, header_tree, plan, axis_roles))
    candidates.extend(_composition_axis_candidates(block, grid, header_tree, plan))
    candidates.extend(_condition_like_property_candidates(block, grid, header_tree, plan))
    candidates.extend(_header_embedded_candidates(block, header_tree))
    candidates.extend(_cell_embedded_candidates(block, grid, header_tree, cell_roles))

    deduped: List[ConditionCandidate] = []
    seen = set()
    for candidate in sorted(candidates, key=lambda item: (-item.priority, -item.confidence)):
        key = _candidate_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)

    deduped.sort(
        key=lambda item: (
            item.target_row if item.target_row is not None else -1,
            item.target_column if item.target_column is not None else -1,
            item.scope,
            item.normalized_name,
            item.value_text,
        )
    )
    for index, candidate in enumerate(deduped, start=1):
        candidate.condition_id = f"{block.table_id}-C{index:04d}"
    return deduped


__all__ = ["extract_condition_candidates"]
