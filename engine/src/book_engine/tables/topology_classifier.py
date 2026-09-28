from __future__ import annotations

from collections import Counter
from typing import List

from book_engine.core.schemas import AxisRoleDecision, HeaderTree, TableBlock, TableGrid, TopologyDecision
from book_engine.tables.semantic_features import (
    COMPARISON_WORDS,
    exact_or_contains,
    header_label,
    same_label_set,
)


def _column_roles(decisions: List[AxisRoleDecision]) -> List[AxisRoleDecision]:
    return [decision for decision in decisions if decision.axis == "column"]


def _row_roles(decisions: List[AxisRoleDecision]) -> List[AxisRoleDecision]:
    return [decision for decision in decisions if decision.axis == "row"]


def classify_table_topology(
    block: TableBlock,
    grid: TableGrid,
    header_tree: HeaderTree,
    axis_roles: List[AxisRoleDecision],
) -> TopologyDecision:
    columns = _column_roles(axis_roles)
    rows = _row_roles(axis_roles)
    ccount = Counter(decision.role for decision in columns)
    rcount = Counter(decision.role for decision in rows)
    reasons: List[str] = []
    unresolved: List[str] = []
    topology = "unresolved"
    orientation = "row_subject"
    confidence = 0.35

    column_labels = [header_label(path) for path in header_tree.column_paths]
    row_labels = ["/".join(path.labels) for path in header_tree.row_paths]
    heading_context = " ".join((block.heading, block.preceding_text[:300], block.following_text[:200]))

    # Symmetric material-by-material matrices are typically compatibility or pairwise results.
    data_columns = [
        path.labels[-1] if path.labels else ""
        for path in header_tree.column_paths
        if path.index not in header_tree.row_header_columns
    ]
    if grid.row_count >= 3 and grid.column_count >= 3 and same_label_set(row_labels, data_columns):
        topology = "pairwise_compatibility"
        orientation = "pairwise_entity"
        confidence = 0.92
        reasons.append("row_and_column_entity_labels_match")
        return TopologyDecision(grid.table_id, topology, orientation, confidence, reasons, unresolved)

    comparison_columns = sum(
        exact_or_contains(label, COMPARISON_WORDS) for label in column_labels
    )
    if comparison_columns >= 2 and any("误差" in label or "偏差" in label for label in column_labels):
        topology = "model_validation"
        orientation = "row_subject"
        confidence = 0.9
        reasons.append("measured_predicted_error_columns")
        return TopologyDecision(grid.table_id, topology, orientation, confidence, reasons, unresolved)

    composition_count = ccount["composition"] + sum(
        decision.role == "property_value" and any("%" in label for label in decision.label_path)
        for decision in columns
    )
    property_count = ccount["property"] + ccount["property_value"]
    condition_count = ccount["condition"]
    method_count = ccount["method"]
    entity_count = ccount["entity"]
    identifier_count = ccount["identifier"]
    note_count = ccount["note"]
    group_count = ccount["group"]

    if composition_count >= 2 and property_count >= 1:
        topology = "composition_and_performance"
        orientation = "row_subject"
        confidence = 0.9
        reasons.extend(["multiple_composition_axes", "performance_axis_present"])
    elif composition_count >= 2 and (entity_count + identifier_count) >= 1:
        topology = "formulation_matrix"
        orientation = "row_subject"
        confidence = 0.85
        reasons.extend(["multiple_composition_axes", "row_formula_or_sample_axis"])
    elif condition_count >= 1 and property_count >= 1:
        topology = "condition_by_property"
        orientation = "row_condition" if rcount["condition"] > 0 else "row_subject"
        confidence = 0.86
        reasons.extend(["condition_axis_present", "property_result_axis_present"])
    elif method_count >= 1 and (property_count >= 1 or note_count + group_count >= 1):
        topology = "method_comparison"
        orientation = "row_method" if rcount["method"] > 0 else "row_subject"
        confidence = 0.82
        reasons.append("method_axis_with_characteristics")
    elif rcount["property"] >= 2 and entity_count >= 1:
        topology = "entity_by_property"
        orientation = "column_subject"
        confidence = 0.84
        reasons.extend(["property_rows", "entity_columns"])
    elif entity_count >= 1 and property_count >= 1:
        topology = "entity_by_property"
        orientation = "row_subject"
        confidence = 0.86
        reasons.extend(["entity_axis_present", "property_axis_present"])
    elif rcount["entity"] >= 2 and property_count >= 1:
        topology = "entity_by_property"
        orientation = "row_subject"
        confidence = 0.78
        reasons.extend(["named_entity_rows", "property_columns"])
    elif condition_count >= 1 and ccount["value"] >= 1:
        topology = "experiment_result"
        orientation = "row_subject"
        confidence = 0.72
        reasons.extend(["condition_axis_present", "numeric_value_axis_present"])
    elif (note_count + group_count) >= 1 and sum(d.numeric_ratio for d in columns) / max(1, len(columns)) < 0.35:
        topology = "qualitative_taxonomy"
        orientation = "row_subject"
        confidence = 0.75
        reasons.append("mostly_qualitative_columns")
    elif property_count >= 1 and ccount["value"] >= 1:
        topology = "entity_by_property"
        orientation = "row_subject"
        confidence = 0.62
        reasons.append("property_and_value_columns_without_clear_entity_header")
        unresolved.append("subject_axis_not_explicit")
    elif method_count >= 1:
        topology = "method_comparison"
        orientation = "row_method"
        confidence = 0.62
        reasons.append("method_axis_detected")
        unresolved.append("comparison_targets_not_explicit")
    elif grid.row_count >= 2 and grid.column_count >= 2:
        topology = "qualitative_taxonomy"
        orientation = "row_subject"
        confidence = 0.5
        reasons.append("fallback_rectangular_table")
        unresolved.append("semantic_axes_low_confidence")
    else:
        unresolved.append("table_too_small_or_semantically_empty")

    if header_tree.confidence < 0.55:
        confidence = max(0.3, confidence - 0.15)
        unresolved.append("header_tree_low_confidence")
    if not header_tree.row_header_columns and orientation in {"row_subject", "row_condition", "row_method"}:
        confidence = max(0.3, confidence - 0.08)
        unresolved.append("row_header_axis_not_explicit")

    return TopologyDecision(
        table_id=grid.table_id,
        topology=topology,
        orientation=orientation,
        confidence=min(confidence, 0.98),
        reasons=reasons,
        unresolved_reasons=sorted(set(unresolved)),
    )


__all__ = ["classify_table_topology"]
