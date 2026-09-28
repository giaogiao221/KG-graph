from __future__ import annotations

import json

from book_engine.document.block_segmenter import TextBlock
from book_engine.gates.text_fact_gate import gate_text_facts
from book_engine.ontology.property_alignment_engine import PropertyAlignmentEngine
from book_engine.quality.semantic_release_consistency import apply_semantic_release_guard
from book_engine.routing.heading_subject_resolver import classify_heading_subject
from book_engine.routing.subject_name_normalizer import normalize_subject_name
from book_engine.routing.text_subject_anchor_resolver import TextSubjectAnchor
from book_engine.text.text_fact_extractor import TextFactRecord
from book_engine.text.text_owner_value_binding import bind_numeric_facts


def _row(
    table_id: str,
    row_index: int,
    *,
    subject: str = "材料A",
    prop: str = "密度",
    raw: str | None = None,
    value: str = "1.2",
    unit: str = "g/cm3",
    conditions: list[tuple[str, str]] | None = None,
    topology: str = "entity_by_property",
):
    atoms = [
        {"name": name, "normalized_name": name, "value_text": val}
        for name, val in (conditions or [])
    ]
    return {
        "fact_id": f"f:{table_id}:{row_index}:{prop}",
        "graph_fact_key": f"g:{table_id}:{row_index}:{prop}",
        "来源类型": "table_conditional_record",
        "所属表格ID": table_id,
        "来源定位": f"L1-L1;R{row_index};C2",
        "主体名称": subject,
        "predicate_raw": raw or prop,
        "attribute_name": prop,
        "尾实体/取值文本": value,
        "单位": unit,
        "normalized_unit": unit,
        "条件文本": "；".join(f"{name}={val}" for name, val in (conditions or [])),
        "structured_condition_json": json.dumps(atoms, ensure_ascii=False),
        "table_semantic_type": topology,
    }


def test_repeated_unit_range_preserves_both_bounds():
    facts = bind_numeric_facts("PEG熔点为85 ℃ ～ 90 ℃。", "PEG")
    assert len(facts) == 1
    assert facts[0].value_text == "85～90 ℃"


def test_specific_owner_survives_generic_following_mention_and_carries_condition():
    text = (
        "丁羟复合火药的燃速催化剂有三氧化二铁和卡托辛。"
        "一般卡托辛性能较好，如用量为2%时，复合火药燃速可提高到35.5mm/s，压力指数为0.35。"
    )
    facts = bind_numeric_facts(text, "复合火药")
    selected = {(f.owner, f.property_name, f.value_text): f for f in facts}
    speed = selected[("丁羟复合火药", "燃速", "35.5mm/s")]
    exponent = selected[("丁羟复合火药", "压力指数", "0.35")]
    assert [(c.normalized_name, c.value_text) for c in speed.conditions] == [("用量", "2%")]
    assert [(c.normalized_name, c.value_text) for c in exponent.conditions] == [("用量", "2%")]


def test_catalyst_and_pressure_are_carried_to_following_result_clause():
    facts = bind_numeric_facts(
        "以二茂铁为催化剂时，在17～20MPa之间出现平台燃烧现象，平台区压力指数为0.2。",
        "AP",
    )
    fact = next(item for item in facts if item.property_name == "压力指数")
    assert fact.owner == "AP"
    assert {(c.normalized_name, c.value_text) for c in fact.conditions} == {
        ("催化剂", "二茂铁"),
        ("压力", "17～20MPa"),
    }


def test_descriptive_table_subject_is_trimmed_without_changing_heading_classification():
    assert normalize_subject_name("某OH预聚物的官能度分布").canonical_name == "某OH预聚物"
    heading = classify_heading_subject("5.2 HTPB推进剂的力学性能")
    assert heading.kind == "entity_with_topic"
    assert heading.entity == "HTPB推进剂"


def test_condition_response_axis_inversion_is_quarantined_as_a_table():
    rows = [
        _row(
            "TINV", i, prop="温度", value=str(220 + i * 5), unit="℃",
            conditions=[("O2", "0.5"), ("N2", "0.2"), ("HCl", "0.1")],
            topology="condition_by_property",
        )
        for i in range(1, 6)
    ]
    result = apply_semantic_release_guard(rows)
    assert not result.releasable_rows
    assert len(result.candidate_rows) == len(rows)
    assert "condition_response_axis_inversion" in result.table_candidate_reasons["TINV"]


def test_formulation_matrix_without_row_level_subject_is_quarantined():
    rows = [
        _row(
            "TFORM", i, subject="金属氢化物", prop="比冲", value=str(2500 + i), unit="N·s/kg",
            conditions=[("粘合剂", "HTPB(15)"), ("氧化剂", "AP(65)"), ("金属燃料", f"Al({i})")],
            topology="composition_and_performance",
        )
        for i in range(1, 6)
    ]
    result = apply_semantic_release_guard(rows)
    assert len(result.candidate_rows) == len(rows)
    assert "formulation_matrix_without_row_level_subject" in result.table_candidate_reasons["TFORM"]


def test_compound_header_leaf_collapse_is_quarantined():
    rows = [
        _row("TLEAF", 1, prop="抗拉强度", raw="抗拉强度 / 延伸率 / 低温", value="1.8/4.5", unit=""),
        _row("TLEAF", 2, prop="抗拉强度", raw="抗拉强度 / 延伸率 / 室温", value="0.5/175", unit=""),
    ]
    result = apply_semantic_release_guard(rows)
    assert len(result.candidate_rows) == 2
    assert "compound_header_leaf_collapse" in result.table_candidate_reasons["TLEAF"]


def test_small_table_with_repeated_comparator_values_is_quarantined():
    rows = [
        _row("TGLUE", 1, prop="冲击感度", value="5937", unit=""),
        _row("TGLUE", 1, prop="摩擦感度", value=">2075>2075", unit="g"),
        _row("TGLUE", 1, prop="静电感度", value=">1>1", unit="J"),
    ]
    result = apply_semantic_release_guard(rows)
    assert len(result.candidate_rows) == 3
    assert "table_column_alignment_anomaly" in result.table_candidate_reasons["TGLUE"]


def test_text_definition_is_not_released_as_material_measurement():
    block = TextBlock("b1", "静电感度：由静电火花点燃铝粉所需的最低能量。", 1, 1, ["铝粉"], "铝粉", 1)
    anchor = TextSubjectAnchor("b1", "铝粉", "材料", "nearest_entity_heading_anchor", 0.95, "confirmed", [])
    record = TextFactRecord(
        record_id="r1", block_id="b1", subject="铝粉", subject_type="材料",
        property_name="静电感度", value_text="由静电火花点燃铝粉所需的最低能量",
        source_type="text_labeled_field", record_status="ready", confidence=0.93,
        evidence=block.text, fact_clause=block.text, owner_source="leaf_entity_heading_field_owner",
    )
    accepted, decisions = gate_text_facts([record], {"b1": anchor})
    assert not accepted
    assert "definition_text_projected_as_material_property" in decisions[0].reasons


def test_formulation_condition_is_not_released_as_component_intrinsic_content():
    block = TextBlock("b1", "在铝粉含量为12%的丁羟复合火药中进行实验。", 1, 1, ["丁羟复合火药"], "丁羟复合火药", 1)
    anchor = TextSubjectAnchor("b1", "丁羟复合火药", "材料", "nearest_entity_heading_anchor", 0.95, "confirmed", [])
    record = TextFactRecord(
        record_id="r2", block_id="b1", subject="铝粉", subject_type="材料",
        property_name="含量", value_text="12%", unit="%", value_num=12,
        source_type="text_clause_bound_numeric_property", record_status="ready", confidence=0.93,
        evidence=block.text, fact_clause=block.text, owner_source="explicit_clause_owner",
    )
    accepted, decisions = gate_text_facts([record], {"b1": anchor})
    assert not accepted
    assert "experimental_condition_projected_as_attribute" in decisions[0].reasons


def test_specific_content_leaf_precedes_parent_content_alias():
    engine = PropertyAlignmentEngine.from_default_config()
    decision = engine.align_row({
        "fact_id": "f1", "predicate_raw": "氮含量", "attribute_name": "氮含量",
        "来源类型": "text_clause_bound_numeric_property", "单位": "%", "normalized_unit": "%",
        "尾实体/取值文本": "37.4%", "主体名称": "材料A",
    })
    assert decision.canonical_name == "氮含量"
    assert decision.status == "aligned_synthetic_leaf"
