from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence


def header_fingerprint(columns: Sequence[str]) -> str:
    payload = "\t".join(columns).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def read_tsv_header(path: Path) -> List[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle, delimiter="\t")
        try:
            return next(reader)
        except StopIteration as exc:
            raise ValueError(f"TSV file is empty: {path}") from exc


def compare_headers(expected: Sequence[str], actual: Sequence[str]) -> Dict[str, object]:
    max_len = max(len(expected), len(actual))
    mismatches = []
    for index in range(max_len):
        expected_value = expected[index] if index < len(expected) else None
        actual_value = actual[index] if index < len(actual) else None
        if expected_value != actual_value:
            mismatches.append(
                {
                    "position": index + 1,
                    "expected": expected_value,
                    "actual": actual_value,
                }
            )
    return {
        "ok": not mismatches and len(expected) == len(actual),
        "expected_count": len(expected),
        "actual_count": len(actual),
        "expected_fingerprint": header_fingerprint(expected),
        "actual_fingerprint": header_fingerprint(actual),
        "mismatches": mismatches,
    }


def validate_columns(columns: Sequence[str], required_count: int = 59) -> None:
    if len(columns) != required_count:
        raise ValueError(f"Schema must contain exactly {required_count} columns; got {len(columns)}")
    if any(not str(column).strip() for column in columns):
        raise ValueError("Schema contains a blank column name")
    duplicates = sorted({column for column in columns if columns.count(column) > 1})
    if duplicates:
        raise ValueError(f"Schema contains duplicate columns: {duplicates}")


def load_contract(path: Path) -> Dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    columns = payload.get("columns")
    if not isinstance(columns, list):
        raise ValueError(f"Invalid schema contract, missing columns list: {path}")
    columns = [str(item) for item in columns]
    validate_columns(columns)
    expected_fingerprint = payload.get("header_sha256")
    actual_fingerprint = header_fingerprint(columns)
    if expected_fingerprint and expected_fingerprint != actual_fingerprint:
        raise ValueError(
            f"Schema contract fingerprint mismatch: expected {expected_fingerprint}, got {actual_fingerprint}"
        )
    payload["columns"] = columns
    payload["header_sha256"] = actual_fingerprint
    payload["column_count"] = len(columns)
    return payload


def validate_graph_file(
    path: Path,
    expected_columns: Sequence[str],
    *,
    check_rows: bool = True,
    max_row_errors: int = 20,
) -> Dict[str, object]:
    validate_columns(list(expected_columns))
    actual_header = read_tsv_header(path)
    header_report = compare_headers(expected_columns, actual_header)
    row_count = 0
    row_errors: List[Dict[str, object]] = []

    if check_rows:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle, delimiter="\t")
            next(reader, None)
            for line_number, row in enumerate(reader, start=2):
                row_count += 1
                if len(row) != len(expected_columns) and len(row_errors) < max_row_errors:
                    row_errors.append(
                        {
                            "line": line_number,
                            "expected_count": len(expected_columns),
                            "actual_count": len(row),
                        }
                    )

    return {
        "ok": bool(header_report["ok"]) and not row_errors,
        "path": str(path),
        "column_count": len(actual_header),
        "row_count": row_count,
        "header": actual_header,
        "header_report": header_report,
        "row_errors": row_errors,
    }


__all__ = [
    "compare_headers",
    "header_fingerprint",
    "load_contract",
    "read_tsv_header",
    "validate_columns",
    "validate_graph_file",
]
