from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Dict, List, Mapping, Sequence

from book_engine.core.schemas import ConditionalFactRecord, TableBlock, TableGrid


def escape_single_line(value: object) -> str:
    text = str(value or "")
    text = text.replace("\\", "\\\\")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.replace("\t", "\\t").replace("\n", "\\n")


def unescape_single_line(value: object) -> str:
    text = str(value or "")
    result: List[str] = []
    index = 0
    while index < len(text):
        if text[index] == "\\" and index + 1 < len(text):
            nxt = text[index + 1]
            if nxt == "n":
                result.append("\n")
                index += 2
                continue
            if nxt == "t":
                result.append("\t")
                index += 2
                continue
            if nxt == "\\":
                result.append("\\")
                index += 2
                continue
        result.append(text[index])
        index += 1
    return "".join(result)


def _write_tsv(path: Path, rows: Sequence[Mapping[str, object]], columns: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), delimiter="\t", extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: escape_single_line(row.get(key, "")) for key in columns})


def write_table_evidence_outputs(
    output_dir: Path,
    table_blocks: Sequence[TableBlock],
    condition_results: Sequence[tuple],
) -> Dict[str, object]:
    root = output_dir / "step_table_evidence"
    root.mkdir(parents=True, exist_ok=True)
    block_by_id = {block.table_id: block for block in table_blocks}

    store_path = root / "table_evidence_store.jsonl"
    with store_path.open("w", encoding="utf-8", newline="\n") as handle:
        for block in table_blocks:
            payload = {
                "table_id": block.table_id,
                "source_type": block.source_type,
                "line_start": block.line_start,
                "line_end": block.line_end,
                "heading": block.heading,
                "raw_table_text": block.raw_text,
                "raw_sha256": hashlib.sha256(block.raw_text.encode("utf-8")).hexdigest(),
            }
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    local_rows: List[Dict[str, object]] = []
    evidence_errors: List[Dict[str, object]] = []
    records_total = 0
    for result in condition_results:
        block: TableBlock = result[0]
        grid: TableGrid = result[1]
        records: Sequence[ConditionalFactRecord] = result[4]
        for record in records:
            records_total += 1
            value_cell = ""
            if record.row_index is not None and record.column_index is not None:
                if 0 <= record.row_index < grid.row_count and 0 <= record.column_index < grid.column_count:
                    value_cell = grid.cells[record.row_index][record.column_index].raw_text
            local_rows.append(
                {
                    "record_id": record.record_id,
                    "table_id": record.table_id,
                    "row_index": "" if record.row_index is None else record.row_index,
                    "column_index": "" if record.column_index is None else record.column_index,
                    "subject": record.subject,
                    "property_name": record.property_name,
                    "value_text": record.value_text,
                    "value_cell_raw": value_cell,
                    "row_header_path": " > ".join(record.row_header_path),
                    "column_header_path": " > ".join(record.column_header_path),
                    "condition_text": "；".join(f"{item.normalized_name or item.name}={item.value_text}" for item in record.conditions),
                    "source_location": f"L{block.line_start}-L{block.line_end};R{record.row_index};C{record.column_index}",
                    "local_evidence": f"主体={record.subject}；属性={record.property_name}；值={record.value_text}；行表头={' > '.join(record.row_header_path)}；列表头={' > '.join(record.column_header_path)}",
                    "table_raw_sha256": hashlib.sha256(block.raw_text.encode("utf-8")).hexdigest(),
                }
            )
            if record.evidence != block.raw_text:
                evidence_errors.append(
                    {
                        "record_id": record.record_id,
                        "table_id": record.table_id,
                        "reason": "record_evidence_not_equal_raw_table",
                    }
                )

    columns = [
        "record_id", "table_id", "row_index", "column_index", "subject", "property_name", "value_text",
        "value_cell_raw", "row_header_path", "column_header_path", "condition_text", "source_location",
        "local_evidence", "table_raw_sha256",
    ]
    _write_tsv(root / "table_fact_local_evidence.tsv", local_rows, columns)
    _write_tsv(root / "table_evidence_errors.tsv", evidence_errors, ["record_id", "table_id", "reason"])

    report = {
        "ok": not evidence_errors,
        "stage": "authoritative_full_table_evidence_v2_phase91",
        "tables": len(table_blocks),
        "records": records_total,
        "evidence_errors": len(evidence_errors),
        "evidence_store": str(store_path),
        "local_evidence": str(root / "table_fact_local_evidence.tsv"),
        "policy": "证据文本=完整原始表格块；局部行列证据=独立审计旁表",
    }
    (root / "table_evidence_validation_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


__all__ = ["escape_single_line", "unescape_single_line", "write_table_evidence_outputs"]
