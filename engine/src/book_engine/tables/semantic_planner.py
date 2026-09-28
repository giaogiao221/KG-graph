from __future__ import annotations

import os
from statistics import mean
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

from book_engine.core.schemas import (
    AxisRoleDecision,
    CellRoleDecision,
    HeaderTree,
    TableBlock,
    TableGrid,
    TableSemanticPlan,
    TopologyDecision,
)
from book_engine.tables.axis_role_resolver import resolve_axis_roles
from book_engine.tables.table_axis_llm_adjudicator import TableAxisLLMAdjudicator
from book_engine.tables.topology_classifier import classify_table_topology
from book_engine.tables.value_role_classifier import classify_cell_roles


def _axis_payload(decision: AxisRoleDecision) -> Dict[str, object]:
    return {
        "axis": decision.axis,
        "index": decision.index,
        "label_path": decision.label_path,
        "unit": decision.unit,
        "role": decision.role,
        "confidence": decision.confidence,
        "reasons": decision.reasons,
    }


def _make_plan(
    grid: TableGrid,
    header_tree: HeaderTree,
    topology: TopologyDecision,
    axis_roles: Sequence[AxisRoleDecision],
    cell_roles: Sequence[CellRoleDecision],
    *,
    llm_used: bool = False,
    llm_status: str = "",
    llm_confidence: float = 0.0,
    llm_reason: str = "",
) -> TableSemanticPlan:
    role_map: Dict[str, List[Dict[str, object]]] = {
        "subject": [], "property": [], "condition": [], "value": [], "method": [],
        "composition": [], "identifier": [], "group": [], "note": [],
    }
    for decision in axis_roles:
        if decision.axis != "column":
            continue
        payload = _axis_payload(decision)
        if decision.role == "entity":
            role_map["subject"].append(payload)
        elif decision.role == "identifier":
            role_map["identifier"].append(payload)
        elif decision.role in {"property", "property_value"}:
            role_map["property"].append(payload)
            if decision.role == "property_value":
                role_map["value"].append(payload)
        elif decision.role == "condition":
            role_map["condition"].append(payload)
        elif decision.role == "value":
            role_map["value"].append(payload)
        elif decision.role == "method":
            role_map["method"].append(payload)
        elif decision.role == "composition":
            role_map["composition"].append(payload)
        elif decision.role == "group":
            role_map["group"].append(payload)
        elif decision.role == "note":
            role_map["note"].append(payload)

    unresolved = list(topology.unresolved_reasons)
    if topology.orientation == "row_subject" and not role_map["subject"] and not header_tree.row_header_columns:
        unresolved.append("no_subject_axis")
    if not role_map["property"] and topology.topology not in {
        "qualitative_taxonomy", "pairwise_compatibility", "method_comparison"
    }:
        unresolved.append("no_property_axis")
    unresolved_numeric = sum(cell.role == "unresolved_numeric" for cell in cell_roles)
    if unresolved_numeric:
        unresolved.append(f"unresolved_numeric_cells:{unresolved_numeric}")

    column_confidences = [decision.confidence for decision in axis_roles if decision.axis == "column"]
    confidence = mean([topology.confidence, header_tree.confidence] + column_confidences) if column_confidences else topology.confidence
    if unresolved:
        confidence = max(0.2, confidence - min(0.2, 0.03 * len(set(unresolved))))
    if llm_used and llm_status == "selected":
        confidence = min(0.96, max(confidence, llm_confidence * 0.94))

    return TableSemanticPlan(
        table_id=grid.table_id,
        topology=topology.topology,
        orientation=topology.orientation,
        subject_axes=role_map["subject"],
        property_axes=role_map["property"],
        condition_axes=role_map["condition"],
        value_axes=role_map["value"],
        method_axes=role_map["method"],
        composition_axes=role_map["composition"],
        identifier_axes=role_map["identifier"],
        group_axes=role_map["group"],
        note_axes=role_map["note"],
        confidence=min(confidence, 0.98),
        unresolved_reasons=sorted(set(unresolved)),
        llm_used=llm_used,
        llm_status=llm_status,
        llm_confidence=llm_confidence,
        llm_reason=llm_reason,
    )


def _ambiguous(plan: TableSemanticPlan, axis_roles: Sequence[AxisRoleDecision]) -> bool:
    column_roles = [item for item in axis_roles if item.axis == "column"]
    low_margin = sum(item.confidence < 0.66 for item in column_roles) >= 2
    conflicting = bool(plan.subject_axes and any(item["index"] in {p["index"] for p in plan.property_axes} for item in plan.subject_axes))
    return plan.confidence < 0.68 or bool(plan.unresolved_reasons) or low_margin or conflicting


def _columns_for_llm(grid: TableGrid, axis_roles: Sequence[AxisRoleDecision]) -> List[Dict[str, object]]:
    result: List[Dict[str, object]] = []
    for decision in axis_roles:
        if decision.axis != "column":
            continue
        samples = []
        for row in range(min(grid.row_count, 8)):
            text = grid.cells[row][decision.index].normalized_text
            if text and text not in samples:
                samples.append(text)
        result.append(
            {
                "index": decision.index,
                "label_path": decision.label_path,
                "current_role": decision.role,
                "unit": decision.unit,
                "numeric_ratio": decision.numeric_ratio,
                "unique_ratio": decision.unique_ratio,
                "confidence": decision.confidence,
                "sample_cells": samples[:6],
            }
        )
    return result


def _apply_llm_roles(
    axis_roles: Sequence[AxisRoleDecision],
    selected_roles: Dict[int, str],
    confidence: float,
) -> List[AxisRoleDecision]:
    result: List[AxisRoleDecision] = []
    for item in axis_roles:
        role = selected_roles.get(item.index) if item.axis == "column" else None
        if role and role != item.role:
            result.append(
                AxisRoleDecision(
                    table_id=item.table_id,
                    axis=item.axis,
                    index=item.index,
                    label_path=list(item.label_path),
                    role=role,
                    unit=item.unit,
                    numeric_ratio=item.numeric_ratio,
                    unique_ratio=item.unique_ratio,
                    confidence=max(item.confidence, min(0.95, confidence)),
                    reasons=list(item.reasons) + [f"closed_set_table_axis_llm:{item.role}->{role}"],
                )
            )
        else:
            result.append(item)
    return result


def build_semantic_plan(
    block: TableBlock,
    grid: TableGrid,
    header_tree: HeaderTree,
) -> Tuple[TableSemanticPlan, List[AxisRoleDecision], List[CellRoleDecision], TopologyDecision]:
    axis_roles = resolve_axis_roles(block, grid, header_tree)
    topology = classify_table_topology(block, grid, header_tree, axis_roles)
    cell_roles = classify_cell_roles(grid, header_tree, axis_roles, topology)
    plan = _make_plan(grid, header_tree, topology, axis_roles, cell_roles)

    model_root = Path(__file__).resolve().parents[2]
    adjudicator = TableAxisLLMAdjudicator(
        enabled=os.getenv("KGCHOUQU_TABLE_AXIS_LLM_ENABLED") == "1",
        cache_dir=model_root / "cache" / "table_axis_phase91",
    )
    if _ambiguous(plan, axis_roles) and adjudicator.available:
        selection = adjudicator.adjudicate(
            table_id=block.table_id,
            heading=block.heading,
            raw_table_text=block.raw_text,
            current_topology=topology.topology,
            current_orientation=topology.orientation,
            columns=_columns_for_llm(grid, axis_roles),
            unresolved_reasons=plan.unresolved_reasons,
        )
        if selection is not None and selection.status == "selected" and selection.confidence >= 0.74:
            axis_roles = _apply_llm_roles(axis_roles, dict(selection.column_roles), selection.confidence)
            topology = TopologyDecision(
                table_id=topology.table_id,
                topology=selection.topology,
                orientation=selection.orientation,
                confidence=max(topology.confidence, min(0.95, selection.confidence)),
                reasons=list(topology.reasons) + ["closed_set_table_axis_llm"],
                unresolved_reasons=[],
            )
            cell_roles = classify_cell_roles(grid, header_tree, axis_roles, topology)
            plan = _make_plan(
                grid, header_tree, topology, axis_roles, cell_roles,
                llm_used=True, llm_status=selection.status,
                llm_confidence=selection.confidence, llm_reason=selection.reason,
            )
        elif selection is not None:
            plan.llm_used = True
            plan.llm_status = selection.status
            plan.llm_confidence = selection.confidence
            plan.llm_reason = selection.reason
    return plan, list(axis_roles), list(cell_roles), topology


__all__ = ["build_semantic_plan"]
