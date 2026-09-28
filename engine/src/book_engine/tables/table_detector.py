from __future__ import annotations

import re
from typing import Iterable, List, Sequence, Tuple

from book_engine.core.schemas import TableBlock
from book_engine.document.markdown_loader import MarkdownDocument
from book_engine.document.heading_rebuilder import rebuild_headings

_HTML_TABLE_RE = re.compile(r"<table\b[^>]*>.*?</table\s*>", re.IGNORECASE | re.DOTALL)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*(.*?)\s*$")
_MD_SEPARATOR_RE = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _nearest_heading(lines: Sequence[str], line_start: int) -> str:
    for idx in range(min(line_start - 2, len(lines) - 1), -1, -1):
        match = _HEADING_RE.match(lines[idx])
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip()
    return ""


def _paragraph_before(lines: Sequence[str], line_start: int, limit: int = 12) -> str:
    parts: List[str] = []
    idx = line_start - 2
    while idx >= 0 and len(parts) < limit:
        line = lines[idx].strip()
        if not line:
            if parts:
                break
            idx -= 1
            continue
        if _HEADING_RE.match(line) or "</table" in line.lower() or "<table" in line.lower():
            break
        parts.append(line)
        idx -= 1
    parts.reverse()
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _paragraph_after(lines: Sequence[str], line_end: int, limit: int = 12) -> str:
    parts: List[str] = []
    idx = line_end
    while idx < len(lines) and len(parts) < limit:
        line = lines[idx].strip()
        if not line:
            if parts:
                break
            idx += 1
            continue
        if _HEADING_RE.match(line) or "<table" in line.lower():
            break
        parts.append(line)
        idx += 1
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _html_blocks(doc: MarkdownDocument) -> List[TableBlock]:
    blocks: List[TableBlock] = []
    for index, match in enumerate(_HTML_TABLE_RE.finditer(doc.text), start=1):
        line_start = _line_number(doc.text, match.start())
        line_end = _line_number(doc.text, match.end())
        blocks.append(
            TableBlock(
                table_id=f"T{index:05d}",
                source_type="html",
                raw_text=match.group(0),
                line_start=line_start,
                line_end=line_end,
                heading=_nearest_heading(doc.lines, line_start),
                preceding_text=_paragraph_before(doc.lines, line_start),
                following_text=_paragraph_after(doc.lines, line_end),
            )
        )
    return blocks


def _split_markdown_row(line: str) -> List[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    cells: List[str] = []
    current: List[str] = []
    escaped = False
    code = False
    for ch in stripped:
        if escaped:
            current.append(ch)
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            current.append(ch)
            continue
        if ch == "`":
            code = not code
            current.append(ch)
            continue
        if ch == "|" and not code:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    cells.append("".join(current).strip())
    return cells


def _markdown_blocks(doc: MarkdownDocument, start_index: int, occupied: Iterable[Tuple[int, int]]) -> List[TableBlock]:
    occupied_ranges = list(occupied)

    def is_occupied(line_number: int) -> bool:
        return any(start <= line_number <= end for start, end in occupied_ranges)

    blocks: List[TableBlock] = []
    lines = doc.lines
    i = 1
    while i < len(lines):
        current_line_no = i + 1
        if is_occupied(current_line_no):
            i += 1
            continue
        if _MD_SEPARATOR_RE.match(lines[i]) and "|" in lines[i - 1]:
            start = i - 1
            end = i
            while end + 1 < len(lines) and "|" in lines[end + 1] and lines[end + 1].strip():
                end += 1
            raw = "\n".join(lines[start : end + 1])
            table_no = start_index + len(blocks)
            blocks.append(
                TableBlock(
                    table_id=f"T{table_no:05d}",
                    source_type="markdown",
                    raw_text=raw,
                    line_start=start + 1,
                    line_end=end + 1,
                    heading=_nearest_heading(lines, start + 1),
                    preceding_text=_paragraph_before(lines, start + 1),
                    following_text=_paragraph_after(lines, end + 1),
                )
            )
            i = end + 1
        else:
            i += 1
    return blocks


def detect_tables(doc: MarkdownDocument) -> List[TableBlock]:
    html = _html_blocks(doc)
    occupied = [(block.line_start, block.line_end) for block in html]
    markdown = _markdown_blocks(doc, len(html) + 1, occupied)
    blocks = html + markdown
    blocks.sort(key=lambda block: (block.line_start, block.line_end))

    # Preserve the full hierarchical heading path.  The nearest heading may be
    # a topic such as “力学性能”, while its parent is the actual material
    # entity.  Table context resolution can therefore use the nearest entity
    # heading rather than blindly using the nearest textual heading.
    headings = rebuild_headings(doc)
    heading_index = 0
    active_path: List[str] = []
    for index, block in enumerate(blocks, start=1):
        while heading_index < len(headings) and headings[heading_index].line_index < block.line_start:
            active_path = list(headings[heading_index].path)
            heading_index += 1
        block.table_id = f"T{index:05d}"
        block.heading_path = list(active_path)
        if active_path:
            block.heading = active_path[-1]
    return blocks


__all__ = ["detect_tables", "_split_markdown_row"]
