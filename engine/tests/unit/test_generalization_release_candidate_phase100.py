from __future__ import annotations

from book_engine.core.schemas import (
    ConditionAtom,
    ConditionalFactRecord,
    TableBlock,
    TableSemanticPlan,
)
from book_engine.gates.table_record_gate import gate_table_records
from book_engine.ontology.property_alignment_engine import PropertyAlignmentEngine
from book_engine.quality.semantic_release_consistency import apply_semantic_release_guard
from book_engine.tables.grid_rebuilder import rebuild_html_grid
from book_engine.tables.record_refiner import refine_condition_results
from book_engine.text.text_owner_value_binding import bind_numeric_facts


def test_row_level_formulation_matrix_recovers_components_and_leaf_properties():
    raw = (
        "<table><tr><td rowspan=2>样品代号</td><td colspan=3>wi×100</td>"
        "<td colspan=2>粒度</td><td rowspan=2>备注</td></tr>"
        "<tr><td>氧化剂A</td><td>金属B</td><td>粘合剂C</td><td>氧化剂A</td><td>金属B</td></tr>"
        "<tr><td>I</td><td>70</td><td>20</td><td>10</td><td>40～60目</td><td>5.7μm</td><td>基准</td></tr>"
        "<tr><td>II</td><td>65</td><td>20</td><td>15</td><td>&lt;180目</td><td>30μm</td><td>细氧化剂</td></tr></table>"
    )
    block = TableBlock("T100", "html", raw, 1, 1, heading="实验配方")
    grid = rebuild_html_grid(block)
    plan = TableSemanticPlan(table_id="T100", topology="entity_by_property", confidence=0.72)
    refined, summary = refine_condition_results([(block, grid, plan, [], [], [], [], None)])
    records = refined[0][4]
    assert len(records) == 10
    assert {r.subject for r in records} == {"配方:T100:R2", "配方:T100:R3"}
    assert {r.property_name for r in records if r.property_role == "composition"} == {
        "氧化剂A", "金属B", "粘合剂C"
    }
    assert {r.property_name for r in records if r.property_role == "measurement"} == {
        "氧化剂A粒度", "金属B粒度"
    }
    assert any(event.action == "recover_row_level_formulation_matrix" for event in summary.events)


def test_mechanical_matrix_is_not_misclassified_as_formulation():
    raw = (
        "<table><tr><td rowspan=2 colspan=2>配方代号</td><td colspan=3>25℃</td><td rowspan=2>特点</td></tr>"
        "<tr><td>模量</td><td>强度</td><td>延伸率</td></tr>"
        "<tr><td>A</td><td>对照</td><td>4.8</td><td>0.7</td><td>36</td><td>基准</td></tr>"
        "<tr><td>B</td><td>改性</td><td>5.1</td><td>0.8</td><td>43</td><td>改性</td></tr></table>"
    )
    block = TableBlock("T101", "html", raw, 1, 1, heading="力学性能")
    grid = rebuild_html_grid(block)
    plan = TableSemanticPlan(table_id="T101", topology="entity_by_property", confidence=0.72)
    refined, summary = refine_condition_results([(block, grid, plan, [], [], [], [], None)])
    assert not refined[0][4]
    assert not any(event.action == "recover_row_level_formulation_matrix" for event in summary.events)


def test_structural_row_formulation_can_bypass_global_registry():
    atom = ConditionAtom(name="样品代号", condition_type="identifier", normalized_name="样品代号", value_text="I")
    record = ConditionalFactRecord(
        subject="配方:T100:R2", subject_type="配方/材料体系", property_name="氧化剂A",
        value_text="70%", unit="%", value_num=70.0, value_role="formulation_component",
        conditions=[atom], confidence=0.94, table_id="T100", row_index=2, column_index=1,
        record_id="r100", property_role="composition", record_status="ready",
        subject_source="row_level_formulation_entity_phase100",
    )
    block = TableBlock("T100", "html", "<table></table>", 1, 1)
    plan = TableSemanticPlan(table_id="T100", topology="formulation_matrix", confidence=0.40)
    accepted, decisions = gate_table_records([(block, None, plan, [], [record], [], [], None)], {})
    assert len(accepted) == 1
    assert decisions[0].accepted
    assert "structurally_recovered_row_formulation_entity" in decisions[0].reasons


def test_component_alignment_precedes_fuzzy_property_alias():
    engine = PropertyAlignmentEngine.from_default_config()
    row = {
        "fact_id": "f100", "predicate_raw": "Al", "attribute_name": "Al",
        "component_name": "Al", "事实类型": "组成事实", "来源类型": "table_conditional_record",
        "normalized_unit": "%", "单位": "%",
    }
    decision = engine.align_row(row)
    assert decision.accepted
    assert decision.canonical_name == "Al"
    assert decision.attribute_category == "组成/配方"


def test_density_with_volume_only_unit_is_quarantined():
    row = {
        "fact_id": "f1", "graph_fact_key": "g1", "来源类型": "text_labeled_field",
        "主体名称": "金属粉", "attribute_name": "密度", "predicate_raw": "密度",
        "尾实体/取值文本": r"$2.7 \\mathrm { c m } ^ { 3 }$", "单位": "", "normalized_unit": "",
    }
    result = apply_semantic_release_guard([row])
    assert not result.releasable_rows
    assert "raw_unit_semantics_lost_or_incomplete" in result.audits[0].reasons


def test_formula_ratio_context_remains_named_copolymer_owner_and_condition():
    facts = bind_numeric_facts(
        "报道了A/B共聚物粘合剂。当A/B=70/30摩尔比时，分子式为C5H8N4O2，氮含量为37.4%，密度为1.26g/cm3。",
        "材料体系",
    )
    selected = [fact for fact in facts if fact.property_name in {"氮含量", "密度"}]
    assert selected
    assert all(fact.owner == "A/B共聚物" for fact in selected)
    assert all(any(atom.normalized_name == "A/B摩尔比" and atom.value_text == "70/30" for atom in fact.conditions) for fact in selected)


def test_derived_formulation_content_belongs_to_named_binder_formulation():
    facts = bind_numeric_facts(
        "端羟基聚丁二烯是燃料粘合剂。未固化的羟基聚丁二烯粘度较小，因此可制成固体含量可达90%的复合火药。",
        "聚氨基甲酸酯",
    )
    assert any(
        fact.owner == "端羟基聚丁二烯复合火药"
        and fact.property_name == "固体含量"
        and fact.value_text == "90%"
        for fact in facts
    )


def test_experiment_result_is_bound_to_condition_not_released_as_condition_property():
    facts = bind_numeric_facts(
        "RDX的卡片间隙实验：当密度为1.53g/cm3时，为336张；当密度为1.64g/cm3时，为284张。",
        "RDX",
    )
    assert {(f.property_name, f.value_text) for f in facts} == {("卡片间隙", "336张"), ("卡片间隙", "284张")}
    assert {tuple((a.normalized_name, a.value_text) for a in f.conditions) for f in facts} == {
        (("密度", "1.53g/cm3"),), (("密度", "1.64g/cm3"),)
    }


def test_sentence_fragment_owner_is_preserved_only_in_candidate_layer():
    row = {
        "fact_id": "f-sentence", "graph_fact_key": "g-sentence",
        "来源类型": "text_clause_bound_numeric_property",
        "主体名称": "把单官能度和三官能度预聚物混合起来",
        "attribute_name": "官能度", "predicate_raw": "官能度",
        "尾实体/取值文本": "2", "normalized_value_num": "2",
        "证据文本": "把单官能度和三官能度预聚物混合起来，也能得到平均官能度为2。",
    }
    result = apply_semantic_release_guard([row])
    assert not result.releasable_rows
    assert "sentence_fragment_or_role_phrase_owner" in result.audits[0].reasons


def test_formulation_ratio_condition_cannot_leak_to_different_owner_pair():
    row = {
        "fact_id": "f-ratio", "graph_fact_key": "g-ratio",
        "来源类型": "text_clause_bound_numeric_property",
        "主体名称": "GAP/AN复合火药", "attribute_name": "燃速", "predicate_raw": "燃速",
        "尾实体/取值文本": "2.80mm/s", "单位": "mm/s", "normalized_unit": "mm/s",
        "条件文本": "GAP/AP摩尔比=20/80；压力=6.86kPa",
        "证据文本": "GAP/AP=20/80时……而GAP/AN复合火药在6.86kPa下燃速为2.80mm/s。",
    }
    result = apply_semantic_release_guard([row])
    assert not result.releasable_rows
    assert "formulation_ratio_condition_conflicts_with_owner" in result.audits[0].reasons


def test_unparsed_temperature_unit_is_not_silently_released():
    row = {
        "fact_id": "f-temp", "graph_fact_key": "g-temp",
        "来源类型": "text_labeled_field", "主体名称": "金属A",
        "attribute_name": "熔点", "predicate_raw": "熔点",
        "尾实体/取值文本": r"$658.9 \\mathrm { { \\bar { C } } }$",
        "单位": "", "normalized_unit": "", "证据文本": r"熔点：$658.9 \\mathrm { { \\bar { C } } }$",
    }
    result = apply_semantic_release_guard([row])
    assert not result.releasable_rows
    assert "raw_unit_semantics_lost_or_incomplete" in result.audits[0].reasons


def test_formula_name_element_mismatch_is_quarantined():
    rows = [
        {
            "fact_id": "f-cu", "graph_fact_key": "g-cu", "来源类型": "text_labeled_field",
            "主体名称": "酸铵", "attribute_name": "化学式", "predicate_raw": "化学式",
            "尾实体/取值文本": r"$\mathrm { { C u O } }$", "证据文本": r"化学式：$\mathrm { { C u O } }$",
        },
        {
            "fact_id": "f-li", "graph_fact_key": "g-li", "来源类型": "text_labeled_field",
            "主体名称": "100284高氯酸锂二肼盐", "attribute_name": "化学式", "predicate_raw": "化学式",
            "尾实体/取值文本": r"$\mathbf { M } \mathbf { g }$", "证据文本": r"化学式：$\mathbf { M } \mathbf { g }$",
        },
        {
            "fact_id": "f-sn", "graph_fact_key": "g-sn", "来源类型": "text_labeled_field",
            "主体名称": "100476二氯化锡stannous chloride", "attribute_name": "化学式", "predicate_raw": "化学式",
            "尾实体/取值文本": r"$\mathrm { S n O }$", "证据文本": r"化学式：$\mathrm { S n O }$",
        },
    ]
    result = apply_semantic_release_guard(rows)
    assert not result.releasable_rows
    assert len(result.candidate_rows) == 3
    assert all("chemical_name_formula_element_mismatch" in audit.reasons for audit in result.audits)


def test_valid_name_formula_pairs_remain_releasable():
    rows = [
        {
            "fact_id": "f-cuo", "graph_fact_key": "g-cuo", "来源类型": "text_labeled_field",
            "主体名称": "氧化铜", "attribute_name": "化学式", "predicate_raw": "化学式",
            "尾实体/取值文本": r"$\mathrm { C u O }$", "证据文本": r"化学式：$\mathrm { C u O }$",
        },
        {
            "fact_id": "f-sncl2", "graph_fact_key": "g-sncl2", "来源类型": "text_labeled_field",
            "主体名称": "二氯化锡", "attribute_name": "化学式", "predicate_raw": "化学式",
            "尾实体/取值文本": "SnCl2", "证据文本": "化学式：SnCl2",
        },
    ]
    result = apply_semantic_release_guard(rows)
    assert len(result.releasable_rows) == 2
    assert not result.candidate_rows
