from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Iterable, List, Sequence, Set, Tuple

from book_engine.core.schemas import TableBlock
from book_engine.document.heading_rebuilder import HeadingNode, heading_by_line
from book_engine.document.markdown_loader import MarkdownDocument

_IMAGE_RE = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")
_RULE_RE = re.compile(r"^\s*[-*_]{3,}\s*$")
_EQUATION_ONLY_RE = re.compile(r"^\s*(?:\$\$.*\$\$|\\\[.*\\\])\s*$", re.S)


@dataclass
class TextBlock:
    block_id: str
    text: str
    line_start: int
    line_end: int
    heading_path: List[str] = field(default_factory=list)
    heading_title: str = ""
    heading_level: int = 0
    role: str = "unknown"
    role_confidence: float = 0.0
    role_reasons: List[str] = field(default_factory=list)


def _table_lines(table_blocks: Sequence[TableBlock]) -> Set[int]:
    result: Set[int] = set()
    for block in table_blocks:
        result.update(range(block.line_start, block.line_end + 1))
    return result


def _clean_paragraph(lines: List[str]) -> str:
    cleaned = []
    for line in lines:
        value = line.strip()
        if not value or _IMAGE_RE.match(value) or _RULE_RE.match(value):
            continue
        cleaned.append(value)
    text = "\n".join(cleaned).strip()
    return text


def segment_text_blocks(
    document: MarkdownDocument,
    headings: Sequence[HeadingNode],
    table_blocks: Sequence[TableBlock],
) -> List[TextBlock]:
    heading_map = heading_by_line(headings)
    table_line_set = _table_lines(table_blocks)
    blocks: List[TextBlock] = []

    current_path: List[str] = []
    current_title = ""
    current_level = 0
    skip_noise_level: int | None = None
    paragraph_lines: List[str] = []
    paragraph_start = 0

    def flush(end_line: int) -> None:
        nonlocal paragraph_lines, paragraph_start
        text = _clean_paragraph(paragraph_lines)
        if text and not _EQUATION_ONLY_RE.match(text):
            digest = hashlib.sha1(
                f"{document.path}|{paragraph_start}|{end_line}|{text}".encode("utf-8")
            ).hexdigest()[:16]
            blocks.append(
                TextBlock(
                    block_id=f"TB:{digest}",
                    text=text,
                    line_start=paragraph_start,
                    line_end=end_line,
                    heading_path=list(current_path),
                    heading_title=current_title,
                    heading_level=current_level,
                )
            )
        paragraph_lines = []
        paragraph_start = 0

    for line_number, raw_line in enumerate(document.lines, start=1):
        node = heading_map.get(line_number)
        if node is not None:
            flush(line_number - 1)
            if skip_noise_level is not None and node.logical_level <= skip_noise_level:
                skip_noise_level = None
            if node.is_noise:
                skip_noise_level = node.logical_level
            current_path = list(node.path)
            current_title = node.title
            current_level = node.logical_level
            continue

        if skip_noise_level is not None:
            continue
        if line_number in table_line_set:
            flush(line_number - 1)
            continue
        if not raw_line.strip():
            flush(line_number - 1)
            continue
        if not paragraph_lines:
            paragraph_start = line_number
        paragraph_lines.append(raw_line)

    flush(len(document.lines))
    return blocks


__all__ = ["TextBlock", "segment_text_blocks"]
