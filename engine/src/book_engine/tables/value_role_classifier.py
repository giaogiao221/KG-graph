from __future__ import annotations

from typing import Dict, List, Tuple

from book_engine.core.schemas import AxisRoleDecision, CellRoleDecision, HeaderTree, TableGrid, TopologyDecision
from book_engine.tables.semantic_features import data_start_row, exact_or_contains, is_numeric_like


def _column_map(axis_roles: List[AxisRoleDecision]) -> Dict[int, AxisRoleDecision]:
    return {decision.index: decision for decision in axis_roles if decision.axis == "column"}


def classify_cell_roles(
    grid: TableGrid,
    header_tree: HeaderTree,
    axis_roles: List[AxisRoleDecision],
    topology: TopologyDecision,
) -> List[CellRoleDecision]:
    decisions: List[CellRoleDecision] = []
    column_roles = _column_map(axis_roles)
    header_rows = set(header_tree.header_rows)
    row_header_columns = set(header_tree.row_header_columns)
    start = data_start_row(header_tree)

    for row in range(grid.row_count):
        for column in range(grid.column_count):
            cell = grid.cells[row][column]
            text = cell.normalized_text
            reasons: List[str] = []
            if row in header_rows or cell.is_header and row < start:
                role = "header"
                confidence = 0.99
                reasons.append("header_cell")
            elif not text:
                role = "empty"
                confidence = 0.99
                reasons.append("empty_cell")
            else:
                axis = column_roles.get(column)
                axis_role = axis.role if axis else "unknown"
                label = "/".join(axis.label_path) if axis else ""

                if column in row_header_columns:
                    if topology.orientation == "row_condition" or axis_role == "condition":
                        role = "condition_value"
                    elif topology.orientation == "row_method" or axis_role == "method":
                        role = "method_name"
                    elif axis_role == "property":
                        role = "property_name"
                    elif axis_role == "identifier":
                        role = "identifier"
                    else:
                        role = "subject_value"
                    confidence = 0.88
                    reasons.append("row_header_axis")
                elif axis_role == "condition":
                    role = "condition_value"
                    confidence = 0.9
                    reasons.append("condition_axis")
                elif axis_role == "composition":
                    role = "formulation_component_value"
                    confidence = 0.9
                    reasons.append("composition_axis")
                elif axis_role == "method":
                    role = "method_name"
                    confidence = 0.88
                    reasons.append("method_axis")
                elif axis_role == "identifier":
                    role = "identifier"
                    confidence = 0.9
                    reasons.append("identifier_axis")
                elif axis_role == "entity":
                    role = "subject_value"
                    confidence = 0.9
                    reasons.append("entity_axis")
                elif axis_role in {"property", "property_value", "value"}:
                    if exact_or_contains(label, {"理论值"}):
                        role = "theoretical_result"
                    elif exact_or_contains(label, {"计算值", "预测值"}):
                        role = "calculated_result"
                    elif exact_or_contains(label, {"实验值", "实测值", "测定值"}):
                        role = "measured_result"
                    elif exact_or_contains(label, {"误差", "偏差", "差值"}):
                        role = "comparison_metric"
                    elif is_numeric_like(text):
                        role = "measured_result"
                    else:
                        role = "qualitative_result"
                    confidence = 0.82 if axis_role == "value" else 0.9
                    reasons.append(f"{axis_role}_axis")
                elif axis_role in {"group", "note"}:
                    role = "qualitative_context"
                    confidence = 0.8
                    reasons.append(f"{axis_role}_axis")
                elif is_numeric_like(text):
                    role = "unresolved_numeric"
                    confidence = 0.45
                    reasons.append("numeric_without_resolved_axis")
                else:
                    role = "qualitative_context"
                    confidence = 0.45
                    reasons.append("text_without_resolved_axis")

            decisions.append(
                CellRoleDecision(
                    table_id=grid.table_id,
                    row_index=row,
                    column_index=column,
                    role=role,
                    confidence=confidence,
                    reasons=reasons,
                )
            )
    return decisions


__all__ = ["classify_cell_roles"]
