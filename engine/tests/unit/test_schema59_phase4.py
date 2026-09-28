import csv
import json
from pathlib import Path

from book_engine.core.schemas import ConditionAtom, ConditionalFactRecord, TableBlock, TableSemanticPlan
from book_engine.export.schema59_columns import SCHEMA59_COLUMNS
from book_engine.export.schema59_exporter import export_schema59
from book_engine.gates.table_record_gate import RecordGateDecision


def test_schema59_export_numeric_visibility_and_conditions(tmp_path: Path):
    record = ConditionalFactRecord(
        record_id="r1", table_id="T00001", row_index=1, column_index=2,
        subject="HMX", subject_type="材料", subject_source="column_header",
        property_name="燃速", value_text="8.1", normalized_value_text="8.1",
        value_num=8.1, unit="mm/s", confidence=0.91, record_status="ready",
        conditions=[ConditionAtom(
            condition_id="c1", name="压力", normalized_name="压力", condition_type="environment",
            value_text="5 MPa", value_num=5.0, unit="MPa", scope="row", confidence=0.9,
        )], evidence="HMX | 燃速 | 8.1",
    )
    decision = RecordGateDecision(
        record_id="r1", table_id="T00001", accepted=True, action="export",
        canonical_subject="HMX", subject_id="subj:1", subject_type="材料",
        original_status="ready", final_status="ready", confidence=0.9,
    )
    plan = TableSemanticPlan(table_id="T00001", topology="condition_by_property", confidence=0.9)
    block = TableBlock(
        table_id="T00001", source_type="html", raw_text="", line_start=10, line_end=12,
        heading="HMX燃速",
    )
    input_path = tmp_path / "测试书.md"
    input_path.write_text("# 测试书", encoding="utf-8")
    report = export_schema59(tmp_path, input_path, [(record, decision, plan)], {"T00001": block})
    assert report["exported_rows"] == 1

    with (tmp_path / "step_graph_guard" / "graph_import_ready.tsv").open(encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        assert reader.fieldnames == SCHEMA59_COLUMNS
        row = next(reader)
    assert row["value_hidden"] == "是"
    assert row["条件文本"] == "压力=5 MPa"
    assert json.loads(row["structured_condition_json"])[0]["normalized_name"] == "压力"
    assert len(row) == 59
