from __future__ import annotations

import re
from typing import List, Sequence

from book_engine.core.schemas import HeaderPath, HeaderTree, TableGrid

_NUMERIC_RE = re.compile(
    r"^[\s<>≤≥≈~～±+\-−]?(?:\d+(?:[.,]\d+)?|[.,]\d+)(?:\s*(?:%|‰|[a-zA-Z°℃·/^\-0-9]+))?$"
)
_UNIT_RE = re.compile(
    r"(?:/|（|\()\s*(%|‰|℃|°C|K|Pa|kPa|MPa|GPa|bar|g/cm(?:3|³)|kg/m(?:3|³)|"
    r"V|mV|kV|A|mA|nm|μm|um|mm|cm|m|ns|μs|us|ms|s|min|h|d|Hz|kHz|MHz|rpm|r/min|K/min|℃/min|"
    r"°C/min|mm/s|cm/s|m/s|km/s|J/g|kJ/kg|kJ/mol)\s*[）)]?\s*$"
)


def _is_numeric_like(value: str) -> bool:
    text = re.sub(r"\s+", "", value or "")
    if not text:
        return False
    return bool(_NUMERIC_RE.match(text)) or any(ch.isdigit() for ch in text) and len(text) <= 24


def _nonempty(values: Sequence[str]) -> List[str]:
    return [value for value in values if value.strip()]


def _row_numeric_ratio(grid: TableGrid, row: int) -> float:
    values = _nonempty([cell.normalized_text for cell in grid.cells[row]])
    if not values:
        return 0.0
    return sum(_is_numeric_like(value) for value in values) / len(values)


def _column_numeric_ratio(grid: TableGrid, column: int, start_row: int) -> float:
    values = _nonempty([grid.cells[row][column].normalized_text for row in range(start_row, grid.row_count)])
    if not values:
        return 0.0
    return sum(_is_numeric_like(value) for value in values) / len(values)


def _infer_header_rows(grid: TableGrid) -> List[int]:
    header_rows: List[int] = []
    for row_index, row in enumerate(grid.cells):
        nonempty = [cell for cell in row if cell.normalized_text]
        if nonempty and any(cell.is_header for cell in nonempty):
            header_rows.append(row_index)
        elif header_rows:
            break
        else:
            break

    if header_rows:
        return header_rows
    if grid.row_count < 2:
        return [0] if grid.row_count == 1 else []

    first_ratio = _row_numeric_ratio(grid, 0)
    later_ratios = [_row_numeric_ratio(grid, row) for row in range(1, min(grid.row_count, 6))]
    later_numeric = sum(later_ratios) / len(later_ratios) if later_ratios else 0.0
    first_has_span = any(cell.colspan > 1 or cell.rowspan > 1 for cell in grid.cells[0])
    first_short_text = [cell.normalized_text for cell in grid.cells[0] if cell.normalized_text]
    short_ratio = (
        sum(len(value) <= 30 for value in first_short_text) / len(first_short_text)
        if first_short_text
        else 0.0
    )
    if first_ratio < 0.5 and (later_numeric > first_ratio + 0.2 or first_has_span or short_ratio >= 0.8):
        header_rows = [0]
        # Include an additional row when the top row is a spanning super-header.
        # Numeric leaves can also be legitimate second-level headers (for
        # example pressure/temperature levels above a result matrix).  Require
        # a real top-row span and at least two independent cells on row 1 so a
        # normal first data row is not swallowed as a header.
        if first_has_span and grid.row_count > 2:
            second_ratio = _row_numeric_ratio(grid, 1)
            independent_second_cells = [
                cell for cell in grid.cells[1]
                if cell.normalized_text and not cell.is_span_copy
            ]
            top_has_multi_column_span = any(cell.colspan > 1 for cell in grid.cells[0])
            if second_ratio < 0.5 or (
                top_has_multi_column_span
                and second_ratio >= 0.6
                and len(independent_second_cells) >= 2
            ):
                header_rows.append(1)
    return header_rows


def _infer_row_header_columns(grid: TableGrid, header_rows: List[int]) -> List[int]:
    start_row = max(header_rows) + 1 if header_rows else 0
    if start_row >= grid.row_count or grid.column_count == 0:
        return []

    explicit: List[int] = []
    for column in range(grid.column_count):
        cells = [grid.cells[row][column] for row in range(start_row, grid.row_count)]
        nonempty = [cell for cell in cells if cell.normalized_text]
        if nonempty and sum(cell.is_header for cell in nonempty) / len(nonempty) >= 0.6:
            explicit.append(column)
        elif explicit:
            break
    if explicit:
        return explicit

    inferred: List[int] = []
    max_candidates = min(3, grid.column_count)
    for column in range(max_candidates):
        ratio = _column_numeric_ratio(grid, column, start_row)
        values = _nonempty(
            [grid.cells[row][column].normalized_text for row in range(start_row, grid.row_count)]
        )
        unique_ratio = len(set(values)) / len(values) if values else 0.0
        if ratio <= 0.35 and unique_ratio >= 0.4:
            inferred.append(column)
        else:
            break
    if len(inferred) == grid.column_count:
        return inferred[:1]
    if inferred:
        return inferred

    # Spanning super-header matrices frequently use a numeric second-level
    # header row and a textual first data column. The broad numeric heuristic
    # above can mistake formulation labels containing percentages for values.
    # Recover only this narrow, structural case: at least two header rows, a
    # top-left row span, and a predominantly lexical first data column.
    if (
        len(header_rows) >= 2
        and grid.column_count >= 2
        and grid.cells[0][0].rowspan > 1
    ):
        values = [
            grid.cells[row][0].normalized_text.strip()
            for row in range(start_row, grid.row_count)
            if grid.cells[row][0].normalized_text.strip()
            and not grid.cells[row][0].normalized_text.strip().startswith(("注", "说明", "备注"))
        ]
        lexical = [
            value for value in values
            if re.search(r"[A-Za-z\u4e00-\u9fff]", value)
            and not _NUMERIC_RE.fullmatch(re.sub(r"\s+", "", value))
        ]
        if values and len(lexical) / len(values) >= 0.6:
            return [0]
    return []


def _dedupe_path(labels: List[str]) -> List[str]:
    result: List[str] = []
    for label in labels:
        value = re.sub(r"\s+", " ", label).strip()
        if value and (not result or result[-1] != value):
            result.append(value)
    return result


def _extract_unit(labels: List[str]) -> str:
    for label in reversed(labels):
        match = _UNIT_RE.search(label)
        if match:
            return match.group(1).strip()
    return ""


def build_header_tree(grid: TableGrid) -> HeaderTree:
    warnings: List[str] = []
    if grid.row_count == 0 or grid.column_count == 0:
        return HeaderTree(table_id=grid.table_id, warnings=["empty_grid"])

    header_rows = _infer_header_rows(grid)
    row_header_columns = _infer_row_header_columns(grid, header_rows)

    column_paths: List[HeaderPath] = []
    for column in range(grid.column_count):
        labels = _dedupe_path(
            [grid.cells[row][column].normalized_text for row in header_rows]
        )
        column_paths.append(
            HeaderPath(
                axis="column",
                index=column,
                labels=labels,
                unit=_extract_unit(labels),
                confidence=0.95 if header_rows and any(grid.cells[row][column].is_header for row in header_rows) else 0.7 if labels else 0.2,
            )
        )

    start_row = max(header_rows) + 1 if header_rows else 0
    row_paths: List[HeaderPath] = []
    for row in range(start_row, grid.row_count):
        labels = _dedupe_path(
            [grid.cells[row][column].normalized_text for column in row_header_columns]
        )
        row_paths.append(
            HeaderPath(
                axis="row",
                index=row,
                labels=labels,
                confidence=0.85 if labels and row_header_columns else 0.2,
            )
        )

    if not header_rows:
        warnings.append("header_rows_not_confident")
    if not row_header_columns:
        warnings.append("row_header_columns_not_confident")

    explicit_header_count = sum(
        1
        for row in header_rows
        for cell in grid.cells[row]
        if cell.normalized_text and cell.is_header
    )
    confidence = 0.5
    if header_rows:
        confidence += 0.2
    if row_header_columns:
        confidence += 0.15
    if explicit_header_count:
        confidence += 0.15
    confidence = min(confidence, 1.0)

    return HeaderTree(
        table_id=grid.table_id,
        header_rows=header_rows,
        row_header_columns=row_header_columns,
        column_paths=column_paths,
        row_paths=row_paths,
        confidence=confidence,
        warnings=warnings,
    )
