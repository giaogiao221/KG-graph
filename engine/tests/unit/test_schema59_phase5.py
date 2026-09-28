import csv
from pathlib import Path

from book_engine.core.schemas import ConditionalFactRecord, TableBlock, TableSemanticPlan
from book_engine.export.schema59_exporter import export_schema59
from book_engine.gates.table_record_gate import RecordGateDecision


def test_composition_record_populates_component_columns(tmp_path: Path):
    record = ConditionalFactRecord(
        record_id="r1", table_id="T1", row_index=1, column_index=2,
        subject="配方P1", subject_type="配方/材料体系", subject_source="row_header",
        property_name="AP", property_role="composition", value_text="70%",
        normalized_value_text="70%", value_num=70.0, unit="%",
        value_role="formulation_component", confidence=0.9, record_status="ready",
    )
    decision = RecordGateDecision(
        record_id="r1", table_id="T1", accepted=True, action="export",
        canonical_subject="配方P1", subject_id="subj:1", subject_type="配方/材料体系",
        original_status="ready", final_status="ready", confidence=0.9,
    )
    plan = TableSemanticPlan(table_id="T1", topology="formulation_matrix", confidence=0.9)
    block = TableBlock(table_id="T1", source_type="html", raw_text="", line_start=1, line_end=3, heading="配方组成")
    input_path = tmp_path / "book.md"
    input_path.write_text("# book", encoding="utf-8")
    report = export_schema59(tmp_path, input_path, [(record, decision, plan)], {"T1": block})
    assert report["exported_rows"] == 1
    with (tmp_path / "step_graph_guard" / "graph_import_ready.tsv").open(encoding="utf-8-sig", newline="") as handle:
        row = next(csv.DictReader(handle, delimiter="\t"))
    assert row["事实类型"] == "组成事实"
    assert row["edge_verb"] == "包含组分"
    assert row["component_name"] == "AP"
    assert row["component_amount_value"] == "70.0"
    assert row["component_amount_unit"] == "%"
    assert row["component_amount_attribute"] == "质量分数"
