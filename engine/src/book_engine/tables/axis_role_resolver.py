from __future__ import annotations

from dataclasses import asdict
from typing import List

from book_engine.core.schemas import AxisRoleDecision, HeaderTree, TableBlock, TableGrid
from book_engine.tables.semantic_features import (
    column_values,
    data_start_row,
    header_label,
    is_numeric_like,
    lexical_scores,
    looks_like_component_label,
    looks_like_material_value,
    numeric_ratio,
    text_ratio,
    unique_ratio,
)


def _column_role(
    block: TableBlock,
    grid: TableGrid,
    header_tree: HeaderTree,
    column: int,
) -> AxisRoleDecision:
    path = header_tree.column_paths[column]
    label = header_label(path)
    values = column_values(grid, header_tree, column)
    nratio = numeric_ratio(values)
    uratio = unique_ratio(values)
    tratio = text_ratio(values)
    lex = lexical_scores(label)
    reasons: List[str] = []
    scores = {
        "entity": 0.0,
        "identifier": 0.0,
        "property": 0.0,
        "condition": 0.0,
        "value": 0.0,
        "method": 0.0,
        "composition": 0.0,
        "group": 0.0,
        "note": 0.0,
        "unknown": 0.05,
    }

    for key in ("entity", "identifier", "property", "condition", "method", "composition", "group", "note"):
        if lex[key]:
            scores[key] += 0.75
            reasons.append(f"header_lexicon:{key}")

    if looks_like_component_label(label, path.unit):
        scores["composition"] += 0.90
        scores["value"] -= 0.10
        reasons.append("component_like_header")

    if column in header_tree.row_header_columns:
        material_ratio = (
            sum(looks_like_material_value(value) for value in values if value.strip())
            / max(1, len([value for value in values if value.strip()]))
        )
        if material_ratio >= 0.6 and uratio >= 0.5:
            scores["entity"] += 0.55
            reasons.append("row_header_text_unique")
        elif tratio >= 0.7:
            scores["group"] += 0.2
            reasons.append("row_header_textual")

    if nratio >= 0.75:
        scores["value"] += 0.65
        reasons.append("mostly_numeric_column")
    elif nratio >= 0.35:
        scores["value"] += 0.3
        reasons.append("partly_numeric_column")

    if path.unit:
        if lex["condition"]:
            scores["condition"] += 0.25
        elif lex["composition"] or looks_like_component_label(label, path.unit):
            scores["composition"] += 0.2
        else:
            scores["property"] += 0.2
            scores["value"] += 0.1
        reasons.append("unit_in_header")

    if lex["condition"] and nratio >= 0.35:
        scores["condition"] += 0.35
        scores["value"] -= 0.1
        reasons.append("numeric_condition_axis")

    if lex["property"]:
        if nratio >= 0.35:
            scores["property"] += 0.25
            scores["value"] += 0.15
            reasons.append("property_with_values")
        elif tratio >= 0.7 and column in header_tree.row_header_columns:
            scores["property"] += 0.25
            reasons.append("property_name_axis")

    if lex["method"] and tratio >= 0.5:
        scores["method"] += 0.25
        reasons.append("textual_method_axis")

    if lex["identifier"]:
        scores["identifier"] += 0.3
        scores["entity"] -= 0.1
        reasons.append("identifier_priority")

    if not label and column == 0 and tratio >= 0.8 and uratio >= 0.5:
        scores["entity"] += 0.4
        reasons.append("unlabelled_first_text_column")

    # A numeric column with a property header represents a property/value axis.
    if scores["property"] >= 0.75 and nratio >= 0.5:
        role = "property_value"
        confidence = min(0.98, 0.6 + 0.25 * nratio + 0.1 * bool(path.unit))
    else:
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        role, top = ranked[0]
        second = ranked[1][1]
        confidence = max(0.2, min(0.98, 0.55 + (top - second) * 0.35))
        if top < 0.25:
            role = "unknown"
            confidence = 0.25

    return AxisRoleDecision(
        table_id=grid.table_id,
        axis="column",
        index=column,
        label_path=list(path.labels),
        role=role,
        unit=path.unit,
        numeric_ratio=nratio,
        unique_ratio=uratio,
        confidence=confidence,
        reasons=reasons,
    )


def _row_role(
    block: TableBlock,
    grid: TableGrid,
    header_tree: HeaderTree,
    row: int,
) -> AxisRoleDecision:
    row_path = next((path for path in header_tree.row_paths if path.index == row), None)
    labels = list(row_path.labels) if row_path else []
    label = "/".join(labels)
    data_values = [
        grid.cells[row][column].normalized_text
        for column in range(grid.column_count)
        if column not in set(header_tree.row_header_columns)
    ]
    nratio = numeric_ratio(data_values)
    uratio = unique_ratio(data_values)
    lex = lexical_scores(label)
    reasons: List[str] = []
    scores = {
        "entity": 0.0,
        "property": 0.0,
        "condition": 0.0,
        "method": 0.0,
        "composition": 0.0,
        "group": 0.0,
        "note": 0.0,
        "unknown": 0.05,
    }
    for key in ("entity", "property", "condition", "method", "composition", "group", "note"):
        if lex[key]:
            scores[key] += 0.75
            reasons.append(f"row_label_lexicon:{key}")

    if looks_like_component_label(label):
        scores["composition"] += 0.6
        reasons.append("row_component_like")

    if label and not is_numeric_like(label) and not any(lex.values()):
        scores["entity"] += 0.35
        reasons.append("row_header_named_item")

    if nratio >= 0.6:
        if lex["condition"]:
            scores["condition"] += 0.25
        elif lex["property"]:
            scores["property"] += 0.25
        elif looks_like_component_label(label):
            scores["composition"] += 0.25
        reasons.append("row_has_numeric_values")

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    role, top = ranked[0]
    second = ranked[1][1]
    confidence = max(0.2, min(0.95, 0.55 + (top - second) * 0.35))
    if top < 0.25:
        role = "unknown"
        confidence = 0.25

    return AxisRoleDecision(
        table_id=grid.table_id,
        axis="row",
        index=row,
        label_path=labels,
        role=role,
        unit="",
        numeric_ratio=nratio,
        unique_ratio=uratio,
        confidence=confidence,
        reasons=reasons,
    )


def resolve_axis_roles(
    block: TableBlock,
    grid: TableGrid,
    header_tree: HeaderTree,
) -> List[AxisRoleDecision]:
    decisions: List[AxisRoleDecision] = []
    for column in range(grid.column_count):
        decisions.append(_column_role(block, grid, header_tree, column))
    for row in range(data_start_row(header_tree), grid.row_count):
        decisions.append(_row_role(block, grid, header_tree, row))
    return decisions


__all__ = ["resolve_axis_roles"]
