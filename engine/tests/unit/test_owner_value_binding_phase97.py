from __future__ import annotations

from pathlib import Path

from book_engine.document.block_segmenter import TextBlock
from book_engine.gates.text_fact_gate import gate_text_facts
from book_engine.routing.text_subject_anchor_resolver import TextSubjectAnchor
from book_engine.text.text_fact_extractor import extract_text_facts
from book_engine.text.text_owner_value_binding import bind_numeric_facts, normalize_scientific_text


def _block(text: str, heading: str = "复合火药") -> TextBlock:
    return TextBlock("b1", text, 1, 1, [heading], heading, 1, role="property", role_confidence=0.9)


def _anchor(subject: str = "复合火药") -> TextSubjectAnchor:
    return TextSubjectAnchor("b1", subject, "材料", "nearest_entity_heading_anchor", 0.92, "confirmed", [])


def test_formula_owner_overrides_chapter_anchor():
    facts = bind_numeric_facts("Al2O的生成热为142.36 kJ/mol。", "铝")
    assert [(f.owner, f.property_name, f.value_text) for f in facts] == [
        ("Al2O", "生成热", "142.36kJ/mol")
    ]


def test_compare_sentence_binds_each_owner_to_its_value():
    facts = bind_numeric_facts(
        "复合火药的密度约为1.64～1.74 g/cm3，双基火药的密度为1.58～1.60 g/cm3。",
        "复合火药",
    )
    assert {(f.owner, f.property_name, f.value_text) for f in facts} == {
        ("复合火药", "密度", "1.64～1.74g/cm3"),
        ("双基火药", "密度", "1.58～1.60g/cm3"),
    }


def test_condition_before_result_is_not_used_as_property_value():
    facts = bind_numeric_facts("GAP/AN复合火药燃速6.86 kPa压力下为2.80 mm/s。", "复合火药")
    assert len(facts) == 1
    fact = facts[0]
    assert fact.owner == "GAP/AN复合火药"
    assert fact.property_name == "燃速"
    assert fact.value_text == "2.80mm/s"
    assert [(c.normalized_name, c.value_text) for c in fact.conditions] == [("压力", "6.86kPa")]


def test_postposed_owner_is_bound():
    facts = bind_numeric_facts("相对分子质量为6000的PCP是蜡状固体。", "NEPE推进剂的粘合剂体系")
    assert any(f.owner == "PCP" and f.property_name == "相对分子质量" and f.value_text == "6000" for f in facts)


def test_latex_formula_is_compacted_for_owner_binding():
    text = r"$\mathbf { A l } _ { 2 } \mathbf { O }$ 的生成热是142.36$\mathbf { k J } / \mathbf { m o l }$"
    normalized = normalize_scientific_text(text)
    assert "Al2O" in normalized
    facts = bind_numeric_facts(text, "铝")
    assert any(f.owner == "Al2O" and f.value_text.startswith("142.36") for f in facts)


def test_leaf_entity_heading_owns_labeled_field():
    block = _block("（4）毒性：铝粉为惰性物质。", heading="铝粉")
    anchor = _anchor("复合火药")
    records = extract_text_facts([block], {block.block_id: anchor})
    toxicity = [r for r in records if r.property_name == "毒性"]
    assert toxicity
    assert all(r.subject == "铝粉" for r in toxicity)


def test_high_recall_application_relation_is_candidate_or_ready():
    block = _block("振能磨用于粉碎高氯酸铵。", heading="振能磨")
    anchor = _anchor("振能磨")
    records = extract_text_facts([block], {block.block_id: anchor})
    application = [r for r in records if r.property_name == "用途"]
    assert len(application) == 1
    assert application[0].subject == "振能磨"
    assert application[0].value_text == "粉碎高氯酸铵"
    accepted, decisions = gate_text_facts(application, {block.block_id: anchor})
    assert accepted or decisions[0].action == "hold"


def test_decimal_comma_and_longest_units_are_normalized():
    from book_engine.tables.value_parser import parse_value

    pressure = parse_value("6,86 kPa")
    speed = parse_value("2.80 mm/s")
    impulse = parse_value("2470 N·s/kg")
    assert pressure.value_num == 6.86 and pressure.unit == "kPa"
    assert speed.value_num == 2.80 and speed.unit == "mm/s"
    assert impulse.value_num == 2470 and impulse.unit == "N·s/kg"


def test_generic_temperature_without_temperature_unit_is_held():
    from book_engine.text.text_fact_extractor import TextFactRecord

    block = _block("异丁基二羟基二茂铁的温度为50。", heading="异丁基二羟基二茂铁")
    anchor = _anchor("异丁基二羟基二茂铁")
    record = TextFactRecord(
        record_id="r-temp", block_id=block.block_id, subject="异丁基二羟基二茂铁", subject_type="材料",
        property_name="温度", value_text="50", value_num=50.0,
        normalized_value_text="50", source_type="text_clause_bound_numeric_property",
        evidence=block.text, fact_clause=block.text, confidence=0.9, record_status="ready",
        owner_source="explicit_clause_owner",
    )
    accepted, decisions = gate_text_facts([record], {block.block_id: anchor})
    assert not accepted
    assert "property_unit_dimension_mismatch" in decisions[0].reasons


def test_structural_formula_fragment_without_explicit_ownership_is_held():
    from book_engine.text.text_fact_extractor import TextFactRecord

    block = _block("其结构可写作CH2N3CH2N3，官能度为1.98。", heading="叠氮聚合物")
    anchor = _anchor("叠氮聚合物")
    record = TextFactRecord(
        record_id="r-structure", block_id=block.block_id, subject="CH2N3CH2N3", subject_type="材料",
        property_name="官能度", value_text="1.98", value_num=1.98,
        normalized_value_text="1.98", source_type="text_clause_bound_numeric_property",
        evidence=block.text, fact_clause="官能度为1.98", confidence=0.9, record_status="ready",
        owner_source="explicit_clause_owner",
    )
    accepted, decisions = gate_text_facts([record], {block.block_id: anchor})
    assert not accepted
    assert "structural_formula_fragment_used_as_owner" in decisions[0].reasons


def test_quantified_generic_subject_surface_is_held():
    from book_engine.text.text_fact_extractor import TextFactRecord

    block = _block("大多数复合火药的粒度为100微米。")
    anchor = _anchor("复合火药")
    record = TextFactRecord(
        record_id="r-quant", block_id=block.block_id, subject="大多数复合火药", subject_type="材料",
        property_name="粒度", value_text="100μm", value_num=100.0, unit="μm",
        normalized_value_text="100μm", source_type="text_clause_bound_numeric_property",
        evidence=block.text, fact_clause=block.text, confidence=0.9, record_status="ready",
        owner_source="explicit_clause_owner",
    )
    accepted, decisions = gate_text_facts([record], {block.block_id: anchor})
    assert not accepted
    assert "quantified_generic_subject_surface" in decisions[0].reasons
