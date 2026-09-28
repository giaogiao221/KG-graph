from __future__ import annotations

import csv
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from book_engine.core.schemas import (
    AxisRoleDecision,
    CellRoleDecision,
    TableBlock,
    TableGrid,
    TableSemanticPlan,
    TopologyDecision,
)


def _write_tsv(path: Path, fieldnames: List[str], rows: Iterable[Dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_table_semantic_outputs(
    output_dir: Path,
    results: List[
        Tuple[
            TableBlock,
            TableGrid,
            TableSemanticPlan,
            List[AxisRoleDecision],
            List[CellRoleDecision],
            TopologyDecision,
        ]
    ],
    errors: List[Dict[str, object]],
) -> Dict[str, object]:
    step_dir = output_dir / "step_table_semantics"
    step_dir.mkdir(parents=True, exist_ok=True)

    topology_rows: List[Dict[str, object]] = []
    axis_rows: List[Dict[str, object]] = []
    cell_rows: List[Dict[str, object]] = []
    unresolved_rows: List[Dict[str, object]] = []
    topology_counts: Counter[str] = Counter()
    axis_role_counts: Counter[str] = Counter()
    cell_role_counts: Counter[str] = Counter()

    plan_path = step_dir / "table_semantic_plan.jsonl"
    with plan_path.open("w", encoding="utf-8") as plan_handle:
        for block, grid, plan, axis_roles, cell_roles, topology in results:
            topology_counts[topology.topology] += 1
            topology_rows.append(
                {
                    "table_id": grid.table_id,
                    "heading": block.heading,
                    "topology": topology.topology,
                    "orientation": topology.orientation,
                    "confidence": f"{topology.confidence:.4f}",
                    "reasons": " | ".join(topology.reasons),
                    "unresolved_reasons": " | ".join(topology.unresolved_reasons),
                    "llm_used": "是" if plan.llm_used else "否",
                    "llm_status": plan.llm_status,
                    "llm_confidence": f"{plan.llm_confidence:.4f}",
                    "llm_reason": plan.llm_reason,
                }
            )
            for decision in axis_roles:
                axis_role_counts[decision.role] += 1
                axis_rows.append(
                    {
                        "table_id": decision.table_id,
                        "axis": decision.axis,
                        "index": decision.index,
                        "label_path": " > ".join(decision.label_path),
                        "role": decision.role,
                        "unit": decision.unit,
                        "numeric_ratio": f"{decision.numeric_ratio:.4f}",
                        "unique_ratio": f"{decision.unique_ratio:.4f}",
                        "confidence": f"{decision.confidence:.4f}",
                        "reasons": " | ".join(decision.reasons),
                    }
                )
            for decision in cell_roles:
                cell_role_counts[decision.role] += 1
                cell = grid.cells[decision.row_index][decision.column_index]
                cell_rows.append(
                    {
                        "table_id": decision.table_id,
                        "row_index": decision.row_index,
                        "column_index": decision.column_index,
                        "text": cell.normalized_text,
                        "role": decision.role,
                        "confidence": f"{decision.confidence:.4f}",
                        "reasons": " | ".join(decision.reasons),
                    }
                )
            if plan.unresolved_reasons or plan.confidence < 0.6:
                unresolved_rows.append(
                    {
                        "table_id": plan.table_id,
                        "heading": block.heading,
                        "topology": plan.topology,
                        "orientation": plan.orientation,
                        "confidence": f"{plan.confidence:.4f}",
                        "unresolved_reasons": " | ".join(plan.unresolved_reasons),
                    }
                )
            payload = asdict(plan)
            payload["heading"] = block.heading
            payload["preceding_text"] = block.preceding_text
            payload["following_text"] = block.following_text
            plan_handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    for error in errors:
        unresolved_rows.append(
            {
                "table_id": error.get("table_id", ""),
                "heading": error.get("heading", ""),
                "topology": "error",
                "orientation": "",
                "confidence": "0.0000",
                "unresolved_reasons": error.get("error", ""),
            }
        )

    _write_tsv(
        step_dir / "table_topology.tsv",
        [
            "table_id",
            "heading",
            "topology",
            "orientation",
            "confidence",
            "reasons",
            "unresolved_reasons",
            "llm_used",
            "llm_status",
            "llm_confidence",
            "llm_reason",
        ],
        topology_rows,
    )
    _write_tsv(
        step_dir / "table_axis_roles.tsv",
        [
            "table_id",
            "axis",
            "index",
            "label_path",
            "role",
            "unit",
            "numeric_ratio",
            "unique_ratio",
            "confidence",
            "reasons",
        ],
        axis_rows,
    )
    _write_tsv(
        step_dir / "table_cell_roles.tsv",
        [
            "table_id",
            "row_index",
            "column_index",
            "text",
            "role",
            "confidence",
            "reasons",
        ],
        cell_rows,
    )
    _write_tsv(
        step_dir / "table_semantic_unresolved.tsv",
        [
            "table_id",
            "heading",
            "topology",
            "orientation",
            "confidence",
            "unresolved_reasons",
        ],
        unresolved_rows,
    )

    total = len(results) + len(errors)
    low_confidence = sum(plan.confidence < 0.6 for _, _, plan, _, _, _ in results)
    unresolved_numeric = cell_role_counts.get("unresolved_numeric", 0)
    report = {
        "ok": len(errors) == 0,
        "stage": "table_semantics_v2_phase2",
        "tables_total": total,
        "tables_planned": len(results),
        "tables_failed": len(errors),
        "tables_low_confidence": low_confidence,
        "tables_with_unresolved_reasons": sum(bool(plan.unresolved_reasons) for _, _, plan, _, _, _ in results),
        "unresolved_numeric_cells": unresolved_numeric,
        "tables_sent_to_closed_set_llm": sum(1 for _, _, plan, _, _, _ in results if plan.llm_used),
        "tables_changed_by_closed_set_llm": sum(1 for _, _, plan, _, _, _ in results if plan.llm_status == "selected"),
        "topology_counts": dict(sorted(topology_counts.items())),
        "axis_role_counts": dict(sorted(axis_role_counts.items())),
        "cell_role_counts": dict(sorted(cell_role_counts.items())),
        "errors": errors,
        "note": "Deterministic table planning is primary. Ambiguous axes may be closed-set LLM adjudicated without modifying any cell content. Conditions are bound later.",
    }
    (step_dir / "table_semantic_validation_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


__all__ = ["write_table_semantic_outputs"]
