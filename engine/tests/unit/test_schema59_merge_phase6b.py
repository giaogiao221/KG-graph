import csv
import json
from pathlib import Path

from book_engine.export.schema59_columns import SCHEMA59_COLUMNS
from book_engine.export.schema59_exporter import export_schema59
from book_engine.gates.text_fact_gate import TextGateDecision
from book_engine.text.text_fact_extractor import TextFactRecord


def test_text_only_export_uses_exact_legacy_59_columns(tmp_path: Path):
    input_path = tmp_path / "book.md"
    input_path.write_text("# 1.1 RDX\n密度为1.80 g/cm3。", encoding="utf-8")
    record = TextFactRecord(
        record_id="txt:1",
        block_id="TB:1",
        subject="RDX",
        subject_type="单质炸药",
        property_name="密度",
        value_text="1.80 g/cm3",
        unit="g/cm3",
        value_num=1.8,
        normalized_value_text="1.80 g/cm3",
        source_type="text_explicit_numeric_property",
        heading_path=["1.1 RDX"],
        line_start=2,
        line_end=2,
        evidence="密度为1.80 g/cm3。",
        confidence=0.9,
        record_status="ready",
    )
    decision = TextGateDecision(
        record_id="txt:1",
        accepted=True,
        action="export",
        canonical_subject="RDX",
        subject_type="单质炸药",
        confidence=0.9,
    )
    report = export_schema59(tmp_path / "out", input_path, [], {}, accepted_text=[(record, decision)])
    graph = tmp_path / "out" / "step_graph_guard" / "graph_import_ready.tsv"
    with graph.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle, delimiter="\t")
        header = next(reader)
        row = next(reader)
    assert header == SCHEMA59_COLUMNS
    assert len(header) == 59
    assert len(row) == 59
    assert report["column_count"] == 59
    assert report["merged_rows"] == 1
