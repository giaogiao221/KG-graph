from __future__ import annotations

import csv
import json
import re
import shutil
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .markdown_units import SourceUnit, parse_markdown_units


TABLE_ID_COLUMN = "所属表格ID"
TABLE_TITLE_COLUMN = "所属表格标题"
SOURCE_LOCATOR_COLUMN = "来源定位"
SOURCE_TYPE_COLUMN = "来源类型"
EXTRACTION_SOURCE_COLUMN = "抽取来源"

_LINE_LOCATOR = re.compile(r"^L(\d+)-L(\d+)")
_HTML_TAG = re.compile(r"<[^>]+>")
_NON_SEARCH = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


@dataclass(frozen=True)
class TableCandidate:
    table_id: str
    table_title: str
    title_source: str
    table_context: str
    caption_line_start: int
    caption_line_end: int
    line_start: int
    line_end: int
    text: str


@dataclass(frozen=True)
class BookRepairJob:
    book_title: str
    source_markdown: Path
    source_book_dir: Path
    relative_output_dir: Path


class TableCatalog:
    def __init__(self, tables: Sequence[TableCandidate]) -> None:
        self.tables = tuple(tables)
        self.by_id = {table.table_id: table for table in self.tables}

    @classmethod
    def from_markdown(cls, path: Path) -> "TableCatalog":
        _, units = parse_markdown_units(path)
        grouped: dict[str, list[SourceUnit]] = {}
        for unit in units:
            grouped.setdefault(unit.table_id, []).append(unit)
        tables = []
        for table_id, chunks in grouped.items():
            first = chunks[0]
            tables.append(
                TableCandidate(
                    table_id=table_id,
                    table_title=first.table_title,
                    title_source=first.table_title_source,
                    table_context=first.table_context,
                    caption_line_start=first.caption_line_start,
                    caption_line_end=first.caption_line_end,
                    line_start=min(chunk.line_start for chunk in chunks),
                    line_end=max(chunk.line_end for chunk in chunks),
                    text="\n".join(chunk.text for chunk in chunks),
                )
            )
        return cls(tables)

    def candidates_for_locator(self, locator: str) -> list[TableCandidate]:
        match = _LINE_LOCATOR.match((locator or "").strip())
        if not match:
            return []
        line_start, line_end = int(match.group(1)), int(match.group(2))
        return [
            table
            for table in self.tables
            if not (table.line_end < line_start or table.line_start > line_end)
        ]

    def to_provenance_catalog(self, book_title: str) -> dict[str, object]:
        return {
            "book_title": book_title,
            "tables": [
                {
                    "table_id": table.table_id,
                    "table_title": _canonical_title(table),
                    "table_title_source": table.title_source,
                    "table_context": table.table_context,
                    "caption_line_start": table.caption_line_start,
                    "caption_line_end": table.caption_line_end,
                    "line_start": table.line_start,
                    "line_end": table.line_end,
                }
                for table in self.tables
            ],
        }


def _canonical_title(table: TableCandidate) -> str:
    return table.table_title if table.title_source == "source_caption" else ""


def _title_key(value: str) -> str:
    return _NON_SEARCH.sub("", unicodedata.normalize("NFKC", value).lower())


def discover_book_jobs(
    root: Path,
    *,
    source_books_root: Path | None = None,
    results_root: Path | None = None,
    title_map: Mapping[str, str] | None = None,
) -> list[BookRepairJob]:
    explicit_roots = source_books_root is not None or results_root is not None
    if explicit_roots and (source_books_root is None or results_root is None):
        raise ValueError("source_books_root and results_root must be provided together")

    originals: dict[str, Path] = {}
    original_search_root = source_books_root or root
    for markdown in original_search_root.rglob("*.md"):
        if source_books_root is None and "原书汇总" not in markdown.parent.name:
            continue
        if markdown.stem in originals:
            raise ValueError(f"duplicate original book title: {markdown.stem}")
        originals[markdown.stem] = markdown

    normalized_originals: dict[str, list[Path]] = {}
    for title, markdown in originals.items():
        normalized_originals.setdefault(_title_key(title), []).append(markdown)

    aliases = dict(title_map or {})
    report_search_root = results_root or root
    relative_root = results_root or root

    jobs: list[BookRepairJob] = []
    seen_result_dirs: set[Path] = set()
    for report in report_search_root.rglob("v106_qwen3_max_book_report.json"):
        book_dir = report.parent
        if "v105_generalized_all_books" in book_dir.parts or book_dir in seen_result_dirs:
            continue
        seen_result_dirs.add(book_dir)
        book_title = book_dir.name
        mapped_title = aliases.get(book_title, book_title)
        if mapped_title.lower().endswith(".md"):
            mapped_title = Path(mapped_title).stem
        source = originals.get(mapped_title)
        if source is None and book_title not in aliases:
            normalized_matches = normalized_originals.get(_title_key(book_title), [])
            if len(normalized_matches) == 1:
                source = normalized_matches[0]
            elif len(normalized_matches) > 1:
                names = ", ".join(path.name for path in normalized_matches)
                raise ValueError(f"ambiguous normalized original title for result book {book_title}: {names}")
        if source is None:
            raise ValueError(f"missing original Markdown for result book: {book_title}")
        jobs.append(
            BookRepairJob(
                book_title=book_title,
                source_markdown=source,
                source_book_dir=book_dir,
                relative_output_dir=book_dir.relative_to(relative_root),
            )
        )
    return sorted(jobs, key=lambda job: str(job.relative_output_dir))


def _normalized(value: object) -> str:
    return _NON_SEARCH.sub("", str(value or "").lower())


def _searchable_table_text(table: TableCandidate) -> str:
    return _normalized(_HTML_TAG.sub(" ", table.text))


def _content_score(row: Mapping[str, str], table: TableCandidate) -> int:
    text = _searchable_table_text(table)
    score = 0
    for field, weight in (
        ("主体名称", 5),
        ("尾实体/取值文本", 4),
        ("predicate_raw", 2),
        ("attribute_name", 2),
        ("数值", 1),
        ("normalized_value_text", 1),
    ):
        value = _normalized(row.get(field, ""))
        if len(value) >= 2 and value in text:
            score += weight
    return score


def _is_missing_direct_table_row(row: Mapping[str, str]) -> bool:
    if str(row.get(TABLE_ID_COLUMN, "") or "").strip():
        return False
    source_type = str(row.get(SOURCE_TYPE_COLUMN, "") or "").strip()
    extraction_source = str(row.get(EXTRACTION_SOURCE_COLUMN, "") or "").strip()
    return source_type == "llm_table_direct" or extraction_source == "qwen3_max_table_direct_v106"


def _unresolved_record(row: Mapping[str, str], candidates: Sequence[TableCandidate], reason: str) -> dict[str, str]:
    return {
        "fact_id": str(row.get("fact_id", "") or ""),
        "书名": str(row.get("书名", "") or ""),
        "来源定位": str(row.get(SOURCE_LOCATOR_COLUMN, "") or ""),
        "主体名称": str(row.get("主体名称", "") or ""),
        "predicate_raw": str(row.get("predicate_raw", "") or ""),
        "attribute_name": str(row.get("attribute_name", "") or ""),
        "尾实体/取值文本": str(row.get("尾实体/取值文本", "") or ""),
        "数值": str(row.get("数值", "") or ""),
        "条件文本": str(row.get("条件文本", "") or ""),
        "candidate_table_ids": "|".join(table.table_id for table in candidates),
        "candidate_table_titles": "|".join(table.table_title for table in candidates),
        "resolution_reason": reason,
    }


def build_resolution_payload(
    book_title: str,
    source_locator: str,
    unresolved_rows: Sequence[Mapping[str, str]],
    catalog: TableCatalog,
    *,
    max_table_chars: int = 16000,
) -> dict[str, object]:
    candidates = catalog.candidates_for_locator(source_locator)
    return {
        "task": "Assign every extracted fact to exactly one candidate source table.",
        "book_title": book_title,
        "source_locator": source_locator,
        "rules": [
            "Use only a table_id listed in candidate_tables.",
            "Return exactly one assignment for every fact_id.",
            "Match the fact's subject, property, value, unit, and condition to table cells and headers.",
            "Do not create, merge, or rename table IDs.",
        ],
        "candidate_tables": [
            {
                "table_id": table.table_id,
                "table_title": table.table_title,
                "line_start": table.line_start,
                "line_end": table.line_end,
                "table_text": table.text[:max_table_chars],
            }
            for table in candidates
        ],
        "facts": [
            {
                "fact_id": str(row.get("fact_id", "") or ""),
                "subject": str(row.get("主体名称", "") or ""),
                "property": str(row.get("predicate_raw", "") or row.get("attribute_name", "") or ""),
                "value": str(row.get("尾实体/取值文本", "") or row.get("数值", "") or ""),
                "numeric_value": str(row.get("数值", "") or ""),
                "condition": str(row.get("条件文本", "") or ""),
            }
            for row in unresolved_rows
        ],
        "response_schema": {
            "assignments": [
                {
                    "fact_id": "string",
                    "table_id": "one candidate table_id",
                    "confidence": "number 0..1",
                    "reason": "short string",
                }
            ]
        },
    }


def validate_resolution_response(
    response: Mapping[str, object],
    unresolved_rows: Sequence[Mapping[str, str]],
) -> tuple[dict[str, str], dict[str, str]]:
    expected = {
        str(row.get("fact_id", "") or ""): {
            item for item in str(row.get("candidate_table_ids", "") or "").split("|") if item
        }
        for row in unresolved_rows
    }
    received: dict[str, list[str]] = {}
    assignments = response.get("assignments", [])
    if isinstance(assignments, list):
        for item in assignments:
            if not isinstance(item, Mapping):
                continue
            fact_id = str(item.get("fact_id", "") or "")
            table_id = str(item.get("table_id", "") or "")
            if fact_id in expected:
                received.setdefault(fact_id, []).append(table_id)

    resolutions: dict[str, str] = {}
    rejected: dict[str, str] = {}
    for fact_id, candidate_ids in expected.items():
        choices = received.get(fact_id, [])
        if not choices:
            rejected[fact_id] = "missing_assignment"
        elif len(choices) != 1:
            rejected[fact_id] = "duplicate_assignment"
        elif choices[0] not in candidate_ids:
            rejected[fact_id] = "invalid_table_id"
        else:
            resolutions[fact_id] = choices[0]
    return resolutions, rejected


def repair_rows(
    rows: Iterable[Mapping[str, str]],
    catalog: TableCatalog,
    *,
    resolutions: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, str]], Counter[str], list[dict[str, str]]]:
    resolutions = resolutions or {}
    repaired_rows: list[dict[str, str]] = []
    audit: Counter[str] = Counter()
    unresolved: list[dict[str, str]] = []

    for original in rows:
        row = dict(original)
        existing_id = str(row.get(TABLE_ID_COLUMN, "") or "").strip()
        if existing_id:
            canonical = catalog.by_id.get(existing_id)
            if canonical and row.get(TABLE_TITLE_COLUMN, "") != _canonical_title(canonical):
                row[TABLE_TITLE_COLUMN] = _canonical_title(canonical)
                audit["canonicalized_existing_title" if canonical.title_source == "source_caption" else "cleared_non_caption_title"] += 1
            repaired_rows.append(row)
            continue

        if not _is_missing_direct_table_row(row):
            repaired_rows.append(row)
            continue

        candidates = catalog.candidates_for_locator(str(row.get(SOURCE_LOCATOR_COLUMN, "") or ""))
        chosen: TableCandidate | None = None
        fill_reason = ""
        fact_id = str(row.get("fact_id", "") or "")
        resolved_id = str(resolutions.get(fact_id, "") or "").strip()

        if resolved_id:
            chosen = next((table for table in candidates if table.table_id == resolved_id), None)
            if chosen:
                fill_reason = "filled_external_resolution"
            else:
                audit["invalid_external_resolution"] += 1
        elif len(candidates) == 1:
            chosen = candidates[0]
            fill_reason = "filled_single_candidate"
        elif len(candidates) > 1:
            scores = [_content_score(row, table) for table in candidates]
            best = max(scores)
            if best > 0 and scores.count(best) == 1:
                chosen = candidates[scores.index(best)]
                fill_reason = "filled_content_match"

        if chosen:
            row[TABLE_ID_COLUMN] = chosen.table_id
            row[TABLE_TITLE_COLUMN] = _canonical_title(chosen)
            audit[fill_reason] += 1
        else:
            reason = "no_candidate" if not candidates else "ambiguous_candidates"
            unresolved.append(_unresolved_record(row, candidates, reason))
            audit["unresolved"] += 1
        repaired_rows.append(row)

    audit["rows"] = len(repaired_rows)
    return repaired_rows, audit, unresolved


def repair_tsv(
    source_path: Path,
    output_path: Path,
    catalog: TableCatalog,
    *,
    resolutions: Mapping[str, str] | None = None,
) -> dict[str, object]:
    with source_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    repaired, audit, unresolved = repair_rows(rows, catalog, resolutions=resolutions)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(repaired)
    return {**dict(audit), "unresolved_rows": unresolved}


def repair_book_directory(
    source_book_dir: Path,
    output_book_dir: Path,
    source_markdown: Path,
    *,
    resolutions: Mapping[str, str] | None = None,
) -> dict[str, object]:
    catalog = TableCatalog.from_markdown(source_markdown)
    shutil.copytree(source_book_dir, output_book_dir, dirs_exist_ok=True)
    catalog_path = output_book_dir / "table_provenance_catalog.json"
    temp_catalog_path = catalog_path.with_suffix(catalog_path.suffix + ".tmp")
    temp_catalog_path.write_text(
        json.dumps(catalog.to_provenance_catalog(source_markdown.stem), ensure_ascii=False, indent=2),
        encoding="utf-8-sig",
    )
    temp_catalog_path.replace(catalog_path)
    audit: Counter[str] = Counter()
    unresolved_rows: list[dict[str, str]] = []

    for source_tsv in source_book_dir.rglob("*.tsv"):
        with source_tsv.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle, delimiter="\t")
            fields = next(reader, [])
        if not {
            TABLE_ID_COLUMN,
            TABLE_TITLE_COLUMN,
            SOURCE_TYPE_COLUMN,
        }.issubset(fields):
            continue
        output_tsv = output_book_dir / source_tsv.relative_to(source_book_dir)
        report = repair_tsv(source_tsv, output_tsv, catalog, resolutions=resolutions)
        audit["tsv_files_repaired"] += 1
        for key, value in report.items():
            if key == "unresolved_rows":
                for item in value:  # type: ignore[union-attr]
                    unresolved_rows.append({"source_file": str(source_tsv), **item})
            elif isinstance(value, int):
                audit[key] += value
    return {**dict(audit), "unresolved_rows": unresolved_rows}


__all__ = [
    "TableCandidate",
    "TableCatalog",
    "BookRepairJob",
    "build_resolution_payload",
    "discover_book_jobs",
    "repair_rows",
    "repair_book_directory",
    "repair_tsv",
    "validate_resolution_response",
]
