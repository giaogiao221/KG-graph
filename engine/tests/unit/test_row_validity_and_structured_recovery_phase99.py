from __future__ import annotations

from book_engine.core.schemas import ConditionalFactRecord, TableBlock, TableSemanticPlan
from book_engine.document.block_segmenter import TextBlock
from book_engine.quality.semantic_release_consistency import apply_semantic_release_guard
from book_engine.tables.grid_rebuilder import rebuild_html_grid
from book_engine.tables.record_refiner import refine_condition_results, refine_record
from book_engine.text.deictic_subject_resolver import resolve_deictic_text_subjects
from book_engine.text.text_fact_extractor import TextFactRecord


def _text_record(subject: str, block_id: str = "b2") -> TextFactRecord:
    return TextFactRecord(
        record_id="r1", block_id=block_id, subject=subject, subject_type="材料",
        property_name="氮含量", value_text="49.6%", source_type="text_clause_bound_numeric_property",
        heading_path=["BAMO及其共聚物"], line_start=12, line_end=12,
        evidence="该粘合剂的氮含量为49.6%", confidence=0.9, record_status="ready",
    )


def test_deictic_subject_resolves_to_nearest_explicit_local_owner():
    blocks = [
        TextBlock("b1", "二羟基BAMO的分子结构式为：", 10, 10, ["BAMO及其共聚物"]),
        TextBlock("b2", "该粘合剂的氮含量为49.6%。", 12, 12, ["BAMO及其共聚物"]),
    ]
    records, audits = resolve_deictic_text_subjects([_text_record("该粘合剂")], blocks)
    assert records[0].subject == "二羟基BAMO"
    assert records[0].owner_source == "local_deictic_antecedent_phase99"
    assert audits[0]["decision"] == "resolve"


def test_deictic_subject_without_local_antecedent_remains_held_candidate():
    blocks = [TextBlock("b2", "该粘合剂的氮含量为49.6%。", 12, 12, ["BAMO及其共聚物"])]
    records, audits = resolve_deictic_text_subjects([_text_record("该粘合剂")], blocks)
    assert records[0].subject == "该粘合剂"
    assert audits[0]["decision"] == "hold"


def test_invalid_formula_and_numeric_placeholder_are_quarantined():
    rows = [
        {
            "fact_id": "f1", "graph_fact_key": "g1", "来源类型": "table_conditional_record",
            "所属表格ID": "T1", "主体名称": "硝化甘油", "attribute_name": "分子式",
            "predicate_raw": "分子式", "尾实体/取值文本": "C3H5(ONOzD3", "条件文本": "",
        },
        {
            "fact_id": "f2", "graph_fact_key": "g2", "来源类型": "table_conditional_record",
            "所属表格ID": "T2", "主体名称": "MgHz", "attribute_name": "熔点",
            "predicate_raw": "熔点", "尾实体/取值文本": "二", "单位": "℃", "条件文本": "",
        },
    ]
    result = apply_semantic_release_guard(rows)
    assert len(result.releasable_rows) == 0
    reasons = {reason for audit in result.audits for reason in audit.reasons}
    assert "invalid_chemical_formula_syntax" in reasons
    assert "numeric_placeholder_or_missing_value" in reasons
    assert "invalid_formula_like_subject" in reasons


def test_valid_formula_is_not_rejected_by_formula_syntax_gate():
    row = {
        "fact_id": "f1", "graph_fact_key": "g1", "来源类型": "table_conditional_record",
        "所属表格ID": "T1", "主体名称": "硝酸铵", "attribute_name": "分子式",
        "predicate_raw": "分子式", "尾实体/取值文本": "NH4NO3", "条件文本": "",
    }
    result = apply_semantic_release_guard([row])
    assert len(result.releasable_rows) == 1


def test_generic_formulation_subject_becomes_row_level_entity():
    record = ConditionalFactRecord(
        subject="基本配方", subject_type="材料", property_name="粒度", value_text="40～60目",
        unit="目", sample_id="N", table_id="T001", row_index=5, column_index=4,
        record_id="r1", subject_source="row_header", record_status="ready", confidence=0.9,
    )
    records, events, _ = refine_record(record, metadata=None)
    assert records[0].subject == "配方:T001:R5"
    assert records[0].subject_source == "row_level_formulation_entity_phase99"
    assert any("synthesized_row_level_formulation_entity" in event.reasons for event in events)


def test_grouped_property_condition_matrix_is_recovered_generically():
    raw = (
        "<table><tr><td rowspan=3>材料</td><td colspan=4>贮存期（月）</td></tr>"
        "<tr><td colspan=2>参数A/(%)</td><td colspan=2>参数B/(%)</td></tr>"
        "<tr><td>5</td><td>10</td><td>5</td><td>10</td></tr>"
        "<tr><td>配方甲</td><td>1</td><td>2</td><td>3</td><td>4</td></tr>"
        "<tr><td>配方乙</td><td>5</td><td>6</td><td>7</td><td>8</td></tr></table>"
    )
    block = TableBlock("T9", "html", raw, 1, 1, heading="贮存期试验")
    grid = rebuild_html_grid(block)
    plan = TableSemanticPlan(table_id="T9", topology="qualitative_taxonomy", confidence=0.5)
    refined, summary = refine_condition_results([(block, grid, plan, [], [], [], [], None)])
    records = refined[0][4]
    assert len(records) == 8
    assert {r.subject for r in records} == {"配方甲", "配方乙"}
    assert {r.property_name for r in records} == {"参数A", "参数B"}
    assert all(any(a.normalized_name == "贮存期" for a in r.conditions) for r in records)
    assert any(event.action == "recover_grouped_property_condition_matrix" for event in summary.events)
