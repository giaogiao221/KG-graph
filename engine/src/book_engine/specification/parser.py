from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

from book_engine.document.markdown_loader import MarkdownDocument
from book_engine.handbook.common import normalize_math_text, normalize_subject_name

_MATERIAL_HEADING_RE = re.compile(
    r"^\s*#{1,6}\s*(?P<section>\d+(?:\s*[.．]\s*\d+){1,4})\s*(?P<title>.+?)\s*$"
)
_IDENTITY_LABEL_RE = re.compile(
    r"(?:中文名称|英文名称|中文别称|中文别名|英文别称|英文别名|分子式|化学式|分子量|相对分子质量|CAS\s*(?:登记号|号)?)\s*[：:]",
    re.I,
)
_ENGLISH_NAME_RE = re.compile(r"英文名称\s*[：:]\s*(?P<value>[^\n]+)", re.I)
_CHINESE_NAME_RE = re.compile(r"中文名称\s*[：:]\s*(?P<value>[^\n]+)", re.I)
_SUBSECTION_RE = re.compile(r"^\s*#{1,6}\s*(?:\d+\s*[.．、])?\s*(?P<title>[^#\n]+?)\s*$")


@dataclass(frozen=True)
class SpecificationEntry:
    section_id: str
    canonical_subject: str
    english_name_raw: str
    title_raw: str
    start_line: int
    end_line: int
    block_text: str


@dataclass(frozen=True)
class SpecificationSubsection:
    entry_section_id: str
    subject: str
    title: str
    line_start: int
    line_end: int
    text: str


def _clean_heading_title(value: str) -> str:
    text = normalize_subject_name(value)
    text = re.sub(r"\s+[.．·…]+\s*\d{1,4}\s*$", "", text)
    text = re.sub(r"\s+\d{1,4}\s*$", "", text)
    return text.strip(" ：:·.…")


def _candidate_has_identity(lines: List[str], index: int) -> bool:
    sample: List[str] = []
    for raw in lines[index + 1 : min(len(lines), index + 35)]:
        if raw.lstrip().startswith("#") and sample:
            break
        sample.append(raw)
    text = "\n".join(sample)
    return len(_IDENTITY_LABEL_RE.findall(text)) >= 2


def parse_specification_entries(document: MarkdownDocument) -> List[SpecificationEntry]:
    lines = document.lines
    candidates: List[tuple[int, str, str]] = []
    for index, raw in enumerate(lines):
        match = _MATERIAL_HEADING_RE.match(raw.replace("$", ""))
        if not match:
            continue
        if not _candidate_has_identity(lines, index):
            continue
        section = re.sub(r"\s+", "", match.group("section")).replace("．", ".")
        candidates.append((index, section, match.group("title").strip()))

    entries: List[SpecificationEntry] = []
    for ordinal, (index, section, raw_title) in enumerate(candidates):
        end_index = candidates[ordinal + 1][0] - 1 if ordinal + 1 < len(candidates) else len(lines) - 1
        block_text = "\n".join(lines[index : end_index + 1])
        chinese_match = _CHINESE_NAME_RE.search(block_text)
        english_match = _ENGLISH_NAME_RE.search(block_text)
        subject = _clean_heading_title(chinese_match.group("value") if chinese_match else raw_title)
        english = normalize_math_text(english_match.group("value")).strip() if english_match else ""
        if not subject or len(subject) > 100:
            continue
        entries.append(
            SpecificationEntry(
                section_id=section,
                canonical_subject=subject,
                english_name_raw=english,
                title_raw=raw_title,
                start_line=index + 1,
                end_line=end_index + 1,
                block_text=block_text,
            )
        )
    return entries


def split_entry_subsections(entry: SpecificationEntry, document: MarkdownDocument) -> List[SpecificationSubsection]:
    lines = document.lines
    start = entry.start_line - 1
    end = entry.end_line
    section_points: List[tuple[int, str]] = []
    for index in range(start + 1, end):
        raw = lines[index]
        if not raw.lstrip().startswith("#"):
            continue
        match = _SUBSECTION_RE.match(raw)
        if not match:
            continue
        title = normalize_math_text(match.group("title")).strip(" ：:·.…")
        # Material headings are handled by entry boundaries. Here we keep only
        # local numbered/semantic subsections such as 物理性质、制备方法、用途。
        if title and not re.match(r"^\d+(?:[.．]\d+)+", title):
            section_points.append((index, title))

    parts: List[SpecificationSubsection] = []
    first_end = section_points[0][0] - 1 if section_points else end - 1
    preamble = "\n".join(lines[start + 1 : first_end + 1]).strip()
    if preamble:
        parts.append(
            SpecificationSubsection(
                entry_section_id=entry.section_id,
                subject=entry.canonical_subject,
                title="身份信息",
                line_start=start + 2,
                line_end=first_end + 1,
                text=preamble,
            )
        )
    for ordinal, (point, title) in enumerate(section_points):
        next_point = section_points[ordinal + 1][0] if ordinal + 1 < len(section_points) else end
        text = "\n".join(lines[point + 1 : next_point]).strip()
        if not text:
            continue
        parts.append(
            SpecificationSubsection(
                entry_section_id=entry.section_id,
                subject=entry.canonical_subject,
                title=title,
                line_start=point + 2,
                line_end=next_point,
                text=text,
            )
        )
    return parts


def entry_for_line(entries: List[SpecificationEntry], line_number: int) -> SpecificationEntry | None:
    for entry in entries:
        if entry.start_line <= line_number <= entry.end_line:
            return entry
    return None


__all__ = [
    "SpecificationEntry",
    "SpecificationSubsection",
    "parse_specification_entries",
    "split_entry_subsections",
    "entry_for_line",
]
