from __future__ import annotations

import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple

from book_engine.core.schemas import SourceLocation, TableBlock, TableCell, TableGrid
from book_engine.tables.table_detector import _split_markdown_row

_SPACE_RE = re.compile(r"[\t\r\f\v ]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


def normalize_cell_text(text: str) -> str:
    value = html.unescape(text or "")
    value = value.replace("\xa0", " ")
    value = _SPACE_RE.sub(" ", value)
    value = re.sub(r" *\n *", "\n", value)
    value = _MULTI_NEWLINE_RE.sub("\n\n", value)
    return value.strip()


@dataclass
class _RawCell:
    text: str
    rowspan: int
    colspan: int
    is_header: bool


class _TableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: List[List[_RawCell]] = []
        self._row: Optional[List[_RawCell]] = None
        self._cell_tag: Optional[str] = None
        self._cell_parts: List[str] = []
        self._rowspan = 1
        self._colspan = 1
        self.warnings: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        lower = tag.lower()
        attrs_dict = {str(k).lower(): str(v) for k, v in attrs if k}
        if lower == "tr":
            if self._row is not None:
                self.warnings.append("nested_or_unclosed_tr")
                if self._row:
                    self.rows.append(self._row)
            self._row = []
        elif lower in {"td", "th"}:
            if self._row is None:
                self._row = []
                self.warnings.append("cell_outside_tr")
            if self._cell_tag is not None:
                self.warnings.append("nested_or_unclosed_cell")
                self._finish_cell()
            self._cell_tag = lower
            self._cell_parts = []
            self._rowspan = _positive_int(attrs_dict.get("rowspan"), 1)
            self._colspan = _positive_int(attrs_dict.get("colspan"), 1)
        elif lower == "br" and self._cell_tag is not None:
            self._cell_parts.append("\n")
        elif lower in {"p", "div", "li"} and self._cell_tag is not None and self._cell_parts:
            self._cell_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        lower = tag.lower()
        if lower in {"td", "th"} and self._cell_tag is not None:
            self._finish_cell()
        elif lower == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
        elif lower == "table":
            if self._cell_tag is not None:
                self._finish_cell()
            if self._row is not None:
                self.rows.append(self._row)
                self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell_tag is not None:
            self._cell_parts.append(data)

    def close(self) -> None:
        super().close()
        if self._cell_tag is not None:
            self._finish_cell()
        if self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def _finish_cell(self) -> None:
        if self._row is None or self._cell_tag is None:
            return
        raw = "".join(self._cell_parts)
        self._row.append(
            _RawCell(
                text=raw,
                rowspan=self._rowspan,
                colspan=self._colspan,
                is_header=self._cell_tag == "th",
            )
        )
        self._cell_tag = None
        self._cell_parts = []
        self._rowspan = 1
        self._colspan = 1


def _positive_int(value: Optional[str], default: int) -> int:
    try:
        parsed = int(str(value).strip())
        return parsed if parsed > 0 else default
    except (TypeError, ValueError):
        return default


def _expand_raw_rows(block: TableBlock, rows: List[List[_RawCell]], warnings: List[str]) -> TableGrid:
    occupied: Dict[Tuple[int, int], TableCell] = {}
    raw_column_count = max((sum(cell.colspan for cell in row) for row in rows), default=0)
    has_rowspan = False
    has_colspan = False

    for raw_row_index, raw_row in enumerate(rows):
        column_index = 0
        for raw_cell in raw_row:
            while (raw_row_index, column_index) in occupied:
                column_index += 1
            has_rowspan = has_rowspan or raw_cell.rowspan > 1
            has_colspan = has_colspan or raw_cell.colspan > 1
            raw_text = raw_cell.text.strip()
            normalized = normalize_cell_text(raw_cell.text)
            source = SourceLocation(
                source_path="",
                block_id=block.table_id,
                table_id=block.table_id,
                line_start=block.line_start,
                line_end=block.line_end,
            )
            for row_offset in range(raw_cell.rowspan):
                for column_offset in range(raw_cell.colspan):
                    row = raw_row_index + row_offset
                    column = column_index + column_offset
                    if (row, column) in occupied:
                        warnings.append(f"span_collision:{row}:{column}")
                        continue
                    occupied[(row, column)] = TableCell(
                        table_id=block.table_id,
                        row_index=row,
                        column_index=column,
                        raw_text=raw_text,
                        normalized_text=normalized,
                        rowspan=raw_cell.rowspan,
                        colspan=raw_cell.colspan,
                        is_header=raw_cell.is_header,
                        origin_row=raw_row_index,
                        origin_column=column_index,
                        is_span_copy=(row_offset != 0 or column_offset != 0),
                        source=source,
                    )
            column_index += raw_cell.colspan

    row_count = max((row for row, _ in occupied), default=-1) + 1
    column_count = max((column for _, column in occupied), default=-1) + 1
    matrix: List[List[TableCell]] = []
    for row in range(row_count):
        matrix_row: List[TableCell] = []
        for column in range(column_count):
            cell = occupied.get((row, column))
            if cell is None:
                cell = TableCell(
                    table_id=block.table_id,
                    row_index=row,
                    column_index=column,
                    raw_text="",
                    normalized_text="",
                    origin_row=row,
                    origin_column=column,
                    source=SourceLocation(
                        source_path="",
                        block_id=block.table_id,
                        table_id=block.table_id,
                        line_start=block.line_start,
                        line_end=block.line_end,
                    ),
                )
            matrix_row.append(cell)
        matrix.append(matrix_row)

    return TableGrid(
        table_id=block.table_id,
        source_type=block.source_type,
        cells=matrix,
        raw_row_count=len(rows),
        raw_column_count=raw_column_count,
        row_count=row_count,
        column_count=column_count,
        has_rowspan=has_rowspan,
        has_colspan=has_colspan,
        warnings=warnings,
    )


def rebuild_html_grid(block: TableBlock) -> TableGrid:
    parser = _TableHTMLParser()
    parser.feed(block.raw_text)
    parser.close()
    return _expand_raw_rows(block, parser.rows, parser.warnings)


def rebuild_markdown_grid(block: TableBlock) -> TableGrid:
    lines = [line for line in block.raw_text.splitlines() if line.strip()]
    if len(lines) < 2:
        return TableGrid(
            table_id=block.table_id,
            source_type=block.source_type,
            cells=[],
            raw_row_count=0,
            raw_column_count=0,
            row_count=0,
            column_count=0,
            warnings=["markdown_table_too_short"],
        )
    data_lines = [lines[0]] + lines[2:]
    split_rows = [_split_markdown_row(line) for line in data_lines]
    width = max((len(row) for row in split_rows), default=0)
    matrix: List[List[TableCell]] = []
    for row_index, row_values in enumerate(split_rows):
        row: List[TableCell] = []
        for column_index in range(width):
            raw = row_values[column_index] if column_index < len(row_values) else ""
            row.append(
                TableCell(
                    table_id=block.table_id,
                    row_index=row_index,
                    column_index=column_index,
                    raw_text=raw,
                    normalized_text=normalize_cell_text(raw),
                    is_header=row_index == 0,
                    origin_row=row_index,
                    origin_column=column_index,
                    source=SourceLocation(
                        source_path="",
                        block_id=block.table_id,
                        table_id=block.table_id,
                        line_start=block.line_start,
                        line_end=block.line_end,
                    ),
                )
            )
        matrix.append(row)
    return TableGrid(
        table_id=block.table_id,
        source_type=block.source_type,
        cells=matrix,
        raw_row_count=len(split_rows),
        raw_column_count=width,
        row_count=len(split_rows),
        column_count=width,
    )


def rebuild_grid(block: TableBlock) -> TableGrid:
    if block.source_type == "html":
        return rebuild_html_grid(block)
    if block.source_type == "markdown":
        return rebuild_markdown_grid(block)
    raise ValueError(f"Unsupported table source type: {block.source_type}")
