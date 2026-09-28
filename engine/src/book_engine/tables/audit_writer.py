from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from book_engine.core.schemas import HeaderTree, TableBlock, TableGrid


def _write_tsv(path: Path, fieldnames: List[str], rows: Iterable[Dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_table_structure_outputs(
    output_dir: Path,
    results: List[Tuple[TableBlock, TableGrid, HeaderTree]],
    errors: List[Dict[str, object]],
) -> Dict[str, object]:
    step_dir = output_dir / "step_table_semantics"
    step_dir.mkdir(parents=True, exist_ok=True)

    inventory_rows: List[Dict[str, object]] = []
    cell_rows: List[Dict[str, object]] = []
    header_rows: List[Dict[str, object]] = []

    for block, grid, header_tree in results:
        inventory_rows.append(
            {
                "table_id": block.table_id,
                "source_type": block.source_type,
                "line_start": block.line_start,
                "line_end": block.line_end,
                "heading": block.heading,
                "preceding_text": block.preceding_text,
                "following_text": block.following_text,
                "raw_row_count": grid.raw_row_count,
                "raw_column_count": grid.raw_column_count,
                "row_count": grid.row_count,
                "column_count": grid.column_count,
                "has_rowspan": grid.has_rowspan,
                "has_colspan": grid.has_colspan,
                "header_rows": ",".join(str(value) for value in header_tree.header_rows),
                "row_header_columns": ",".join(str(value) for value in header_tree.row_header_columns),
                "header_confidence": f"{header_tree.confidence:.4f}",
                "warnings": " | ".join(grid.warnings + header_tree.warnings),
                "parse_status": "ok",
                "parse_error": "",
            }
        )
        for cell in grid.iter_cells():
            cell_rows.append(
                {
                    "table_id": cell.table_id,
                    "row_index": cell.row_index,
                    "column_index": cell.column_index,
                    "origin_row": cell.origin_row,
                    "origin_column": cell.origin_column,
                    "is_span_copy": cell.is_span_copy,
                    "is_header": cell.is_header,
                    "rowspan": cell.rowspan,
                    "colspan": cell.colspan,
                    "raw_text": cell.raw_text,
                    "normalized_text": cell.normalized_text,
                    "line_start": cell.source.line_start if cell.source else "",
                    "line_end": cell.source.line_end if cell.source else "",
                }
            )
        header_rows.append(
            {
                "table_id": header_tree.table_id,
                "header_rows": header_tree.header_rows,
                "row_header_columns": header_tree.row_header_columns,
                "column_paths": [asdict(path) for path in header_tree.column_paths],
                "row_paths": [asdict(path) for path in header_tree.row_paths],
                "confidence": header_tree.confidence,
                "warnings": header_tree.warnings,
            }
        )

    for error in errors:
        inventory_rows.append(
            {
                "table_id": error.get("table_id", ""),
                "source_type": error.get("source_type", ""),
                "line_start": error.get("line_start", ""),
                "line_end": error.get("line_end", ""),
                "heading": error.get("heading", ""),
                "parse_status": "error",
                "parse_error": error.get("error", ""),
            }
        )

    _write_tsv(
        step_dir / "table_inventory.tsv",
        [
            "table_id",
            "source_type",
            "line_start",
            "line_end",
            "heading",
            "preceding_text",
            "following_text",
            "raw_row_count",
            "raw_column_count",
            "row_count",
            "column_count",
            "has_rowspan",
            "has_colspan",
            "header_rows",
            "row_header_columns",
            "header_confidence",
            "warnings",
            "parse_status",
            "parse_error",
        ],
        inventory_rows,
    )
    _write_tsv(
        step_dir / "table_cells.tsv",
        [
            "table_id",
            "row_index",
            "column_index",
            "origin_row",
            "origin_column",
            "is_span_copy",
            "is_header",
            "rowspan",
            "colspan",
            "raw_text",
            "normalized_text",
            "line_start",
            "line_end",
        ],
        cell_rows,
    )
    with (step_dir / "table_header_tree.jsonl").open("w", encoding="utf-8") as handle:
        for row in header_rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    parsed = len(results)
    empty_grids = sum(grid.row_count == 0 or grid.column_count == 0 for _, grid, _ in results)
    report = {
        "ok": len(errors) == 0 and empty_grids == 0,
        "stage": "table_structure_v2_phase1",
        "tables_detected": parsed + len(errors),
        "tables_parsed": parsed,
        "tables_failed": len(errors),
        "empty_grids": empty_grids,
        "html_tables": sum(block.source_type == "html" for block, _, _ in results),
        "markdown_tables": sum(block.source_type == "markdown" for block, _, _ in results),
        "tables_with_rowspan": sum(grid.has_rowspan for _, grid, _ in results),
        "tables_with_colspan": sum(grid.has_colspan for _, grid, _ in results),
        "cells_emitted": len(cell_rows),
        "errors": errors,
    }
    (step_dir / "table_structure_validation_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report
