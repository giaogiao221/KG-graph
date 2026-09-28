import csv
import json
from pathlib import Path

from book_engine.export.schema59_columns import SCHEMA59_FALLBACK_COLUMNS
from book_engine.export.schema59_contract import (
    compare_headers,
    header_fingerprint,
    load_contract,
    validate_graph_file,
)


def test_fallback_schema_has_exactly_59_unique_columns():
    assert len(SCHEMA59_FALLBACK_COLUMNS) == 59
    assert len(set(SCHEMA59_FALLBACK_COLUMNS)) == 59


def test_header_comparison_detects_order_drift():
    changed = list(SCHEMA59_FALLBACK_COLUMNS)
    changed[0], changed[1] = changed[1], changed[0]
    report = compare_headers(SCHEMA59_FALLBACK_COLUMNS, changed)
    assert not report["ok"]
    assert report["mismatches"][0]["position"] == 1


def test_contract_round_trip_and_graph_validation(tmp_path: Path):
    contract_path = tmp_path / "contract.json"
    payload = {
        "columns": SCHEMA59_FALLBACK_COLUMNS,
        "column_count": 59,
        "header_sha256": header_fingerprint(SCHEMA59_FALLBACK_COLUMNS),
    }
    contract_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    contract = load_contract(contract_path)
    assert contract["column_count"] == 59

    graph_path = tmp_path / "graph_import_ready.tsv"
    with graph_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SCHEMA59_FALLBACK_COLUMNS, delimiter="\t")
        writer.writeheader()
        writer.writerow({column: "" for column in SCHEMA59_FALLBACK_COLUMNS})
    report = validate_graph_file(graph_path, contract["columns"], check_rows=True)
    assert report["ok"]
    assert report["row_count"] == 1
