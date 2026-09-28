from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


@dataclass(frozen=True)
class SourceUnit:
    unit_id: str
    kind: str  # text_window | table
    book_title: str
    heading_path: tuple[str, ...]
    line_start: int
    line_end: int
    text: str
    table_id: str = ""
    table_title: str = ""
    table_title_source: str = ""
    table_context: str = ""
    caption_line_start: int = 0
    caption_line_end: int = 0


@dataclass(frozen=True)
class TableMetadata:
    title: str
    title_source: str
    context: str
    caption_line_start: int = 0
    caption_line_end: int = 0


_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$")
_HTML_TABLE_START = re.compile(r"<table\b", re.I)
_HTML_TABLE_END = re.compile(r"</table\s*>", re.I)
_PIPE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
_IMAGE_ONLY = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")
_TOCISH = re.compile(r"^(?:目录|contents?)\s*$", re.I)
_REFERENCE_HEADING = re.compile(r"^(?:参考文献|references?|索引|index)\s*$", re.I)
_TABLE_CAPTION = re.compile(
    r"^\s*(?:续\s*表|表\s*(?:\d|[一二三四五六七八九十百]+|\$)|table\s*(?:\d|[ivxlcdm]+))",
    re.I,
)


def decode_hash_u_name(name: str) -> str:
    def repl(match: re.Match[str]) -> str:
        try:
            return chr(int(match.group(1), 16))
        except Exception:
            return match.group(0)
    return re.sub(r"#U([0-9a-fA-F]{4,6})", repl, name)


def _unit_id(book: str, kind: str, start: int, end: int, text: str) -> str:
    digest = hashlib.sha1(f"{book}|{kind}|{start}|{end}|{text}".encode("utf-8")).hexdigest()
    return f"u:{digest[:24]}"


def _is_pipe_table(lines: Sequence[str], index: int) -> bool:
    if index + 1 >= len(lines):
        return False
    return "|" in lines[index] and bool(_PIPE_SEPARATOR.match(lines[index + 1]))


def _clean_paragraph(text: str) -> str:
    value = re.sub(r"\s+", " ", text).strip()
    return value


def _table_metadata(lines: Sequence[str], table_index: int, headings: Sequence[str]) -> TableMetadata:
    context = " > ".join(item for item in headings if item)
    for index in range(table_index - 1, max(-1, table_index - 5), -1):
        candidate = _clean_paragraph(lines[index])
        if not candidate:
            continue
        if _TABLE_CAPTION.match(candidate):
            return TableMetadata(
                title=candidate,
                title_source="source_caption",
                context=context,
                caption_line_start=index + 1,
                caption_line_end=index + 1,
            )
        break
    return TableMetadata(
        title="",
        title_source="context_only" if context else "unnamed",
        context=context,
    )


def parse_markdown_units(
    path: Path,
    *,
    max_window_chars: int = 6500,
    min_paragraph_chars: int = 35,
    overlap_paragraphs: int = 1,
    max_table_chars: int = 30000,
) -> tuple[list[SourceUnit], list[SourceUnit]]:
    """Build heading-aware text windows and complete/chunked tables.

    Line numbers are 1-based and remain tied to the original Markdown file.
    HTML tables are kept as raw evidence. Oversized tables are split at row
    boundaries while retaining the first row as a header prefix.
    """

    text = path.read_text(encoding="utf-8-sig", errors="replace")
    lines = text.splitlines()
    book_title = decode_hash_u_name(path.stem)
    headings: list[str] = []
    paragraphs: list[tuple[int, int, tuple[str, ...], str]] = []
    tables: list[SourceUnit] = []
    paragraph_buffer: list[str] = []
    paragraph_start = 0
    paragraph_heading: tuple[str, ...] = ()
    table_number = 0

    def flush_paragraph(end_line: int) -> None:
        nonlocal paragraph_buffer, paragraph_start, paragraph_heading
        if not paragraph_buffer:
            return
        value = _clean_paragraph("\n".join(paragraph_buffer))
        if (
            len(value) >= min_paragraph_chars
            and not _IMAGE_ONLY.match(value)
            and not _TOCISH.match(value)
        ):
            paragraphs.append((paragraph_start, end_line, paragraph_heading, value))
        paragraph_buffer = []
        paragraph_start = 0
        paragraph_heading = tuple(headings)

    index = 0
    while index < len(lines):
        line = lines[index]
        line_no = index + 1
        heading_match = _HEADING.match(line)
        if heading_match:
            flush_paragraph(line_no - 1)
            level = len(heading_match.group(1))
            title = _clean_paragraph(heading_match.group(2))
            headings = headings[: level - 1]
            headings.append(title)
            paragraph_heading = tuple(headings)
            index += 1
            continue

        if _HTML_TABLE_START.search(line):
            flush_paragraph(line_no - 1)
            table_number += 1
            table_id = f"T{table_number:05d}"
            table_metadata = _table_metadata(lines, index, headings)
            start = line_no
            block = [line]
            index += 1
            if not _HTML_TABLE_END.search(line):
                while index < len(lines):
                    block.append(lines[index])
                    if _HTML_TABLE_END.search(lines[index]):
                        index += 1
                        break
                    index += 1
            raw = "\n".join(block).strip()
            end = start + len(block) - 1
            for chunk_idx, chunk in enumerate(_chunk_html_table(raw, max_table_chars), start=1):
                suffix = "" if len(chunk) == len(raw) else f"#chunk{chunk_idx}"
                tables.append(
                    SourceUnit(
                        unit_id=_unit_id(book_title, "table", start, end, chunk + suffix),
                        kind="table",
                        book_title=book_title,
                        heading_path=tuple(headings),
                        line_start=start,
                        line_end=end,
                        text=chunk,
                        table_id=table_id,
                        table_title=table_metadata.title,
                        table_title_source=table_metadata.title_source,
                        table_context=table_metadata.context,
                        caption_line_start=table_metadata.caption_line_start,
                        caption_line_end=table_metadata.caption_line_end,
                    )
                )
            continue

        if _is_pipe_table(lines, index):
            flush_paragraph(line_no - 1)
            table_number += 1
            table_id = f"T{table_number:05d}"
            table_metadata = _table_metadata(lines, index, headings)
            start = line_no
            block = [line, lines[index + 1]]
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                block.append(lines[index])
                index += 1
            raw = "\n".join(block).strip()
            end = start + len(block) - 1
            for chunk in _chunk_pipe_table(raw, max_table_chars):
                tables.append(
                    SourceUnit(
                        unit_id=_unit_id(book_title, "table", start, end, chunk),
                        kind="table",
                        book_title=book_title,
                        heading_path=tuple(headings),
                        line_start=start,
                        line_end=end,
                        text=chunk,
                        table_id=table_id,
                        table_title=table_metadata.title,
                        table_title_source=table_metadata.title_source,
                        table_context=table_metadata.context,
                        caption_line_start=table_metadata.caption_line_start,
                        caption_line_end=table_metadata.caption_line_end,
                    )
                )
            continue

        if not line.strip():
            flush_paragraph(line_no - 1)
            index += 1
            continue

        if paragraph_start == 0:
            paragraph_start = line_no
            paragraph_heading = tuple(headings)
        paragraph_buffer.append(line)
        index += 1

    flush_paragraph(len(lines))

    # Stop direct scanning after a references/index heading where possible.
    filtered: list[tuple[int, int, tuple[str, ...], str]] = []
    for item in paragraphs:
        if any(_REFERENCE_HEADING.match(h or "") for h in item[2]):
            continue
        filtered.append(item)

    windows: list[SourceUnit] = []
    i = 0
    while i < len(filtered):
        start_i = i
        chars = 0
        selected: list[tuple[int, int, tuple[str, ...], str]] = []
        base_heading = filtered[i][2]
        while i < len(filtered):
            item = filtered[i]
            # Do not mix unrelated chapters in one window once enough context exists.
            if selected and item[2] != base_heading and chars >= max_window_chars // 2:
                break
            projected = chars + len(item[3]) + 2
            if selected and projected > max_window_chars:
                break
            selected.append(item)
            chars = projected
            i += 1
        if not selected:
            selected = [filtered[i]]
            i += 1
        raw = "\n\n".join(item[3] for item in selected)
        windows.append(
            SourceUnit(
                unit_id=_unit_id(book_title, "text_window", selected[0][0], selected[-1][1], raw),
                kind="text_window",
                book_title=book_title,
                heading_path=selected[-1][2] or selected[0][2],
                line_start=selected[0][0],
                line_end=selected[-1][1],
                text=raw,
            )
        )
        if i < len(filtered):
            i = max(start_i + 1, i - max(0, overlap_paragraphs))

    return windows, tables


def _chunk_html_table(raw: str, max_chars: int) -> list[str]:
    if len(raw) <= max_chars:
        return [raw]
    rows = re.findall(r"<tr\b.*?</tr\s*>", raw, flags=re.I | re.S)
    if len(rows) < 2:
        return [raw[i : i + max_chars] for i in range(0, len(raw), max_chars)]
    header = rows[0]
    chunks: list[str] = []
    current = [header]
    current_len = len(header)
    for row in rows[1:]:
        if len(current) > 1 and current_len + len(row) > max_chars:
            chunks.append("<table>\n" + "\n".join(current) + "\n</table>")
            current = [header]
            current_len = len(header)
        current.append(row)
        current_len += len(row)
    if len(current) > 1:
        chunks.append("<table>\n" + "\n".join(current) + "\n</table>")
    return chunks or [raw]


def _chunk_pipe_table(raw: str, max_chars: int) -> list[str]:
    if len(raw) <= max_chars:
        return [raw]
    lines = raw.splitlines()
    if len(lines) < 3:
        return [raw]
    header = lines[:2]
    chunks: list[str] = []
    current = list(header)
    current_len = sum(map(len, current))
    for row in lines[2:]:
        if len(current) > 2 and current_len + len(row) > max_chars:
            chunks.append("\n".join(current))
            current = list(header)
            current_len = sum(map(len, current))
        current.append(row)
        current_len += len(row)
    if len(current) > 2:
        chunks.append("\n".join(current))
    return chunks or [raw]


__all__ = ["SourceUnit", "TableMetadata", "parse_markdown_units", "decode_hash_u_name"]
