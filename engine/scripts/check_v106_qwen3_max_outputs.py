from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.exists():
        return [], []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        return list(reader.fieldnames or []), [dict(row) for row in reader]


def validate_process_rows(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("process_id", "") or "")].append(row)
    for process_id, items in grouped.items():
        if not process_id:
            errors.append({"reason": "empty_process_id"})
            continue
        try:
            ordered = sorted(items, key=lambda row: int(row.get("step_index", 0) or 0))
            indices = [int(row.get("step_index", 0) or 0) for row in ordered]
        except Exception:
            errors.append({"process_id": process_id, "reason": "invalid_step_index"})
            continue
        if len(ordered) < 2 or indices != list(range(1, len(ordered) + 1)):
            errors.append({"process_id": process_id, "reason": "non_contiguous_steps", "indices": indices})
            continue
        ids = [str(row.get("step_id", "") or "") for row in ordered]
        if len(set(ids)) != len(ids) or any(not item for item in ids):
            errors.append({"process_id": process_id, "reason": "invalid_step_ids"})
            continue
        for offset, row in enumerate(ordered):
            expected_prev = ids[offset - 1] if offset > 0 else ""
            expected_next = ids[offset + 1] if offset + 1 < len(ids) else ""
            if str(row.get("previous_step_id", "") or "") != expected_prev:
                errors.append({"process_id": process_id, "step": offset + 1, "reason": "previous_link_mismatch"})
            if str(row.get("next_step_id", "") or "") != expected_next:
                errors.append({"process_id": process_id, "step": offset + 1, "reason": "next_link_mismatch"})
            if str(row.get("抽取来源", "") or "") != "qwen3_max_process_flow_specialized_v106p":
                errors.append({"process_id": process_id, "step": offset + 1, "reason": "wrong_process_writer"})
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--expected-books", type=int, default=8)
    args = parser.parse_args()
    reports = sorted(args.root.glob("*/v106_qwen3_max_book_report.json"))
    errors: list[dict[str, Any]] = []
    books: list[dict[str, Any]] = []
    total_processes = 0
    total_steps = 0
    for report_path in reports:
        report = json.loads(report_path.read_text(encoding="utf-8-sig"))
        book = str(report.get("book_title", "") or report_path.parent.name)
        guard = report_path.parent / "step_graph_guard"
        final_header, final_rows = read_rows(guard / "graph_import_ready.tsv")
        process_header, process_rows = read_rows(guard / "graph_process_flow_ready.tsv")
        if len(final_header) != 59:
            errors.append({"book": book, "reason": "final_column_count", "actual": len(final_header)})
        if len(process_header) != 59:
            errors.append({"book": book, "reason": "process_column_count", "actual": len(process_header)})
        if len(final_rows) != int(report.get("final_rows", -1)):
            errors.append({"book": book, "reason": "final_row_count_mismatch", "actual": len(final_rows)})
        if len(process_rows) != int(report.get("accepted_process_step_rows", -1)):
            errors.append({"book": book, "reason": "process_row_count_mismatch", "actual": len(process_rows)})
        if int(report.get("llm_error_jobs", 0) or 0) > 0:
            errors.append({"book": book, "reason": "llm_error_jobs", "count": report.get("llm_error_jobs")})
        missing_table_ids = [
            {
                "fact_id": str(row.get("fact_id", "") or ""),
                "source_locator": str(row.get("来源定位", "") or ""),
            }
            for row in final_rows
            if str(row.get("来源类型", "") or "") == "llm_table_direct"
            and not str(row.get("所属表格ID", "") or "").strip()
        ]
        if missing_table_ids:
            errors.append(
                {
                    "book": book,
                    "reason": "direct_table_id_missing",
                    "count": len(missing_table_ids),
                    "examples": missing_table_ids[:20],
                }
            )
        generic_process_rows = [
            row for row in final_rows
            if (str(row.get("事实类型", "") or "") == "方法步骤事实" or str(row.get("step_action", "") or "").strip())
            and str(row.get("抽取来源", "") or "") != "qwen3_max_process_flow_specialized_v106p"
        ]
        if generic_process_rows:
            errors.append({"book": book, "reason": "generic_process_rows_in_final", "count": len(generic_process_rows)})
        process_errors = validate_process_rows(process_rows)
        if process_errors:
            errors.append({"book": book, "reason": "process_graph_invalid", "examples": process_errors[:20], "count": len(process_errors)})
        process_count = len({str(row.get("process_id", "") or "") for row in process_rows if str(row.get("process_id", "") or "")})
        total_processes += process_count
        total_steps += len(process_rows)
        books.append(
            {
                "book": book,
                "rows": len(final_rows),
                "columns": len(final_header),
                "process_graphs": process_count,
                "process_steps": len(process_rows),
                "process_review_spans": int(report.get("process_manual_review_spans", 0) or 0),
                "ok": report.get("ok"),
            }
        )
    if len(reports) != args.expected_books:
        errors.append({"reason": "book_count", "expected": args.expected_books, "actual": len(reports)})
    result = {
        "ok": not errors,
        "revision": "v106_process_flow_specialized",
        "book_count": len(reports),
        "process_graphs": total_processes,
        "process_steps": total_steps,
        "books": books,
        "errors": errors,
    }
    out = args.root / "v106_qwen3_max_acceptance_report.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8-sig")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
