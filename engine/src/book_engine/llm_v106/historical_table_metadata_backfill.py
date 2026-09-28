from __future__ import annotations

import csv
import json
import re
import unicodedata
import zipfile
from collections import Counter
from pathlib import Path
from typing import Mapping, Sequence
from xml.etree import ElementTree

from .table_metadata_backfill import TableCatalog, repair_rows


def _normalized_title(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").lower()
    value = re.sub(r"\.(?:md|tsv|csv|xlsx)$", "", value)
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", value)


def _decode_fact_file(path: Path) -> str:
    raw = path.read_bytes()
    candidates: list[tuple[int, str]] = []
    for encoding in ("utf-8-sig", "gb18030", "utf-8", "gbk"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        score = sum(text.count(marker) for marker in ("fact_id", "书名", "来源类型", "证据文本")) * 100
        candidates.append((score - text.count("�"), text))
    if not candidates:
        raise ValueError(f"unable to decode fact file: {path}")
    return max(candidates, key=lambda item: item[0])[1]


def read_fact_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if path.suffix.lower() == ".xlsx":
        return _read_xlsx_rows(path)
    if path.suffix.lower() not in {".tsv", ".csv"}:
        raise ValueError(f"unsupported fact file format: {path}")
    text = _decode_fact_file(path)
    lines = text.splitlines()
    if not lines:
        return [], []
    delimiter = "\t" if lines[0].count("\t") >= lines[0].count(",") else ","
    reader = csv.DictReader(lines, delimiter=delimiter)
    fields = list(reader.fieldnames or [])
    return fields, [dict(row) for row in reader]


_SHEET_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _column_index(reference: str) -> int:
    index = 0
    for character in re.match(r"[A-Z]+", reference).group(0):
        index = index * 26 + ord(character) - 64
    return index - 1


def _read_xlsx_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with zipfile.ZipFile(path) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = [
                "".join(node.text or "" for node in item.findall(".//m:t", _SHEET_NS))
                for item in shared_root.findall("m:si", _SHEET_NS)
            ]
        worksheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))

    matrix: list[dict[int, str]] = []
    for source_row in worksheet.findall(".//m:sheetData/m:row", _SHEET_NS):
        values: dict[int, str] = {}
        for cell in source_row.findall("m:c", _SHEET_NS):
            value_node = cell.find("m:v", _SHEET_NS)
            value = value_node.text if value_node is not None and value_node.text else ""
            if cell.attrib.get("t") == "s" and value:
                value = shared[int(value)]
            elif cell.attrib.get("t") == "inlineStr":
                value = "".join(node.text or "" for node in cell.findall(".//m:t", _SHEET_NS))
            values[_column_index(cell.attrib["r"])] = value
        matrix.append(values)
    if not matrix:
        return [], []
    width = max(matrix[0], default=-1) + 1
    fields = [matrix[0].get(index, "") for index in range(width)]
    return fields, [
        {field: row.get(index, "") for index, field in enumerate(fields)}
        for row in matrix[1:]
    ]


def resolve_source_book(
    book_title: str,
    result_path: Path,
    markdown_roots: Sequence[Path],
    aliases: Mapping[str, str],
) -> Path:
    markdowns = [path for root in markdown_roots for path in root.rglob("*.md")]
    by_title: dict[str, list[Path]] = {}
    for markdown in markdowns:
        by_title.setdefault(_normalized_title(markdown.stem), []).append(markdown)

    requested = aliases.get(book_title, book_title)
    candidates = by_title.get(_normalized_title(requested), [])
    if not candidates:
        candidates = by_title.get(_normalized_title(result_path.stem), [])
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise ValueError(f"ambiguous original Markdown for book: {book_title}")
    raise ValueError(f"missing original Markdown for book: {book_title}")


def repair_historical_rows(
    rows: Sequence[Mapping[str, str]],
    catalog: TableCatalog,
    *,
    resolutions: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, str]], object, list[dict[str, str]]]:
    """Apply the current provenance rules to rows from a historical export."""
    prepared: list[dict[str, str]] = []
    for original in rows:
        row = dict(original)
        table_id = str(row.get("所属表格ID", "") or "").strip()
        if table_id and table_id not in catalog.by_id:
            row["所属表格ID"] = ""
            row["所属表格标题"] = ""
            row["__historical_original_source_type"] = str(row.get("来源类型", "") or "")
            row["来源类型"] = "llm_table_direct"
        prepared.append(row)

    repaired, audit, unresolved = repair_rows(prepared, catalog, resolutions=resolutions)
    normalized = 0
    for row in repaired:
        original_source_type = row.pop("__historical_original_source_type", None)
        if original_source_type is not None:
            row["来源类型"] = original_source_type
            if str(row.get("所属表格ID", "") or "").strip():
                normalized += 1
    if normalized:
        audit["normalized_legacy_table_id"] += normalized
    return repaired, audit, unresolved


def _write_tsv(path: Path, fields: Sequence[str], rows: Sequence[Mapping[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _book_title(rows: Sequence[Mapping[str, str]], path: Path) -> str:
    title_counts = Counter(str(row.get("书名", "") or "").strip() for row in rows)
    title_counts.pop("", None)
    if len(title_counts) == 1:
        return next(iter(title_counts))
    if not title_counts:
        return path.stem
    document_ids = {str(row.get("文档ID", "") or "").strip() for row in rows}
    document_ids.discard("")
    non_placeholder = {
        title: count
        for title, count in title_counts.items()
        if not re.fullmatch(r"input_\d+", title, flags=re.IGNORECASE)
    }
    if len(document_ids) == 1 and non_placeholder:
        best_count = max(non_placeholder.values())
        best_titles = [title for title, count in non_placeholder.items() if count == best_count]
        if len(best_titles) == 1:
            return best_titles[0]
    raise ValueError(f"multiple book titles in one result file: {path}")


def run_batch(
    input_root: Path,
    markdown_roots: Sequence[Path],
    output_root: Path,
    aliases: Mapping[str, str],
    *,
    included_files: Sequence[Path] | None = None,
    resolutions: Mapping[str, str] | None = None,
) -> dict[str, object]:
    input_root = input_root.resolve()
    output_root = output_root.resolve()
    if input_root == output_root:
        raise ValueError("input-root and output-root must differ")

    summary: Counter[str] = Counter()
    unresolved: list[dict[str, str]] = []
    mappings: list[dict[str, str]] = []
    processed_sources: set[Path] = set()
    allowed = {Path(path) for path in included_files} if included_files is not None else None
    for source_path in sorted(input_root.rglob("*")):
        if source_path.suffix.lower() not in {".tsv", ".csv", ".xlsx"}:
            continue
        if allowed is not None and source_path.relative_to(input_root) not in allowed:
            continue
        fields, rows = read_fact_rows(source_path)
        if not {"所属表格ID", "所属表格标题", "来源类型"}.issubset(fields):
            summary["skipped_non_schema_files"] += 1
            continue
        book_title = _book_title(rows, source_path)
        source_markdown = resolve_source_book(book_title, source_path, markdown_roots, aliases)
        catalog = TableCatalog.from_markdown(source_markdown)
        repaired, audit, pending = repair_historical_rows(rows, catalog, resolutions=resolutions)
        relative = source_path.relative_to(input_root).with_suffix(".tsv")
        _write_tsv(output_root / relative, fields, repaired)
        mappings.append(
            {
                "source_file": str(source_path.relative_to(input_root)),
                "output_file": str(relative),
                "book_title": book_title,
                "source_markdown": str(source_markdown),
            }
        )
        for key, value in audit.items():
            summary[key] += value
        for item in pending:
            unresolved.append({"source_file": str(source_path.relative_to(input_root)), **item})
        if source_markdown not in processed_sources:
            catalog_path = output_root / "table_provenance_catalogs" / source_markdown.stem / "table_provenance_catalog.json"
            _write_json(catalog_path, catalog.to_provenance_catalog(source_markdown.stem))
            processed_sources.add(source_markdown)
        summary["processed_files"] += 1
        summary["input_rows"] += len(rows)

    summary["resolved_rows"] = sum(
        summary[key] for key in ("filled_single_candidate", "filled_content_match", "filled_external_resolution")
    )
    _write_tsv(
        output_root / "source_book_mapping.tsv",
        ["source_file", "output_file", "book_title", "source_markdown"],
        mappings,
    )
    _write_tsv(
        output_root / "table_metadata_backfill_unresolved.tsv",
        [
            "source_file", "fact_id", "书名", "来源定位", "主体名称", "predicate_raw", "attribute_name",
            "尾实体/取值文本", "数值", "条件文本", "candidate_table_ids", "candidate_table_titles", "resolution_reason",
        ],
        unresolved,
    )
    report = {
        "input_root": str(input_root),
        "markdown_roots": [str(path.resolve()) for path in markdown_roots],
        "output_root": str(output_root),
        "aliases": dict(aliases),
        "included_file_count": len(allowed) if allowed is not None else None,
        **dict(summary),
        "unresolved_rows": len(unresolved),
    }
    _write_json(output_root / "table_metadata_backfill_report.json", report)
    return report
