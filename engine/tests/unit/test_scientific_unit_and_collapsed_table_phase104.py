from __future__ import annotations

from book_engine.core.schemas import TableBlock
from book_engine.tables.condition_extractor import extract_condition_candidates
from book_engine.tables.grid_rebuilder import rebuild_grid
from book_engine.tables.header_tree_builder import build_header_tree
from book_engine.tables.record_compiler import (
    _split_collapsed_numeric_series,
    _tokenize_collapsed_material_codes,
    compile_conditional_records,
)
from book_engine.tables.semantic_planner import build_semantic_plan
from book_engine.tables.value_parser import parse_value
from book_engine.text.text_owner_value_binding import bind_numeric_facts, normalize_scientific_text


def _compile(raw: str):
    block = TableBlock(
        table_id="T104",
        source_type="html",
        raw_text=raw,
        line_start=1,
        line_end=1,
        heading="1.1 导论",
        preceding_text="表1.1一些现用的和新的含能材料的性能",
    )
    grid = rebuild_grid(block)
    tree = build_header_tree(grid)
    plan, axis_roles, cell_roles, _ = build_semantic_plan(block, grid, tree)
    candidates = extract_condition_candidates(block, grid, tree, plan, axis_roles, cell_roles)
    records, bindings, unresolved, _ = compile_conditional_records(
        block, grid, tree, plan, axis_roles, cell_roles, candidates
    )
    return plan, records, bindings, unresolved


def test_latex_composite_units_survive_owner_value_binding():
    density_text = r"CL-20的密度可达 $2 . 0 4 \mathrm { g } \cdot \mathrm { c m } ^ { - 3 }$。"
    normalized = normalize_scientific_text(density_text)
    assert "2.04g/cm3" in normalized
    density = next(fact for fact in bind_numeric_facts(density_text) if fact.property_name == "密度")
    parsed_density = parse_value(density.value_text)
    assert density.owner == "CL-20"
    assert parsed_density.value_num == 2.04
    assert parsed_density.unit == "g/cm3"

    size_text = r"由此制得的TATB粒度范围为 $3 0 \mu \mathrm { m } \mathrm { \sim } 6 0 \mu \mathrm { m }$。"
    size = next(fact for fact in bind_numeric_facts(size_text) if fact.property_name == "粒度")
    parsed_size = parse_value(size.value_text)
    assert size.owner == "TATB"
    assert parsed_size.lower_bound == 30
    assert parsed_size.upper_bound == 60
    assert parsed_size.unit == "um"

    speed_text = r"低压时，复合推进剂的燃速为 $\mathrm { 1 c m ^ { * } s ^ { - 1 } }$ 数量级。"
    speed = next(fact for fact in bind_numeric_facts(speed_text) if fact.property_name == "燃速")
    assert parse_value(speed.value_text).unit == "cm/s"

    ppm = next(
        fact
        for fact in bind_numeric_facts("催化剂DabcoT-131含量为20ppm时，适用期约4h。")
        if fact.property_name == "含量"
    )
    assert parse_value(ppm.value_text).unit == "ppm"


def test_closed_set_collapsed_series_tokenization_is_count_bound():
    assert _tokenize_collapsed_material_codes(
        "现用含能材料TNTRDXHMX(β)PETNNTONGNCANAP"
    ) == ["TNT", "RDX", "HMX(β)", "PETN", "NTO", "NG", "NC", "AN", "AP"]
    assert _split_collapsed_numeric_series(
        "1.651.811.961.761.92", 5, "密度"
    ) == ["1.65", "1.81", "1.96", "1.76", "1.92"]
    assert _split_collapsed_numeric_series(
        "-45.492.6104.8-502.8-96.7-351.5", 6, "生成能"
    ) == ["-45.4", "92.6", "104.8", "-502.8", "-96.7", "-351.5"]
    assert not _split_collapsed_numeric_series("1.651.811.96", 7, "密度")
    assert not _tokenize_collapsed_material_codes("普通材料ABCXYZ")


def test_collapsed_rowspan_material_table_recovers_pairwise_facts():
    raw = (
        "<table><tr><td>缩写</td><td>化学名称</td><td>应用领域</td>"
        "<td>密度1gcm-3</td><td>氧平衡/%</td><td>生成能/kJ·mol-1</td></tr>"
        "<tr><td rowspan=5>现用含能材料TNTRDXHMX(β)PETNNTONGNCANAP</td>"
        "<td rowspan=5>2,4,6-三硝基甲苯环三亚甲基三硝胺环四亚甲基四硝胺季戊四醇四硝酸酯"
        "3-硝基-1,2,4-三唑-5-酮硝化甘油硝化棉(13%N)硝酸铵高氯酸铵</td>"
        "<td rowspan=5>HXHX,RP,GPHX,RP,GPHXHXRP,GPRP,GPHX,RPRP,HX</td>"
        "<td>1.651.811.961.761.92</td><td>-74.0-21.6-21.6-10.1-24.6</td>"
        "<td rowspan=2>-45.492.6104.8-502.8-96.7-351.5</td></tr>"
        "<tr><td rowspan=2>1.591.66</td><td>3.5</td></tr>"
        "<tr><td>-31.8</td><td>-669.8</td></tr>"
        "<tr><td>1.72</td><td>20.0</td><td>-354.6</td></tr>"
        "<tr><td>1.95</td><td>34.0</td><td>-283.1</td></tr>"
        "<tr><td>新含能材料TNAZCL-20(HNIW)FOX-7ONCADN</td>"
        "<td>1,3,3-三硝基氮杂环丁烷六硝基六氮杂异伍尔兹烷1,1-二氨基-2,2-二硝基乙烯八硝基立方烷二硝酰胺铵</td>"
        "<td>HX,RP,GPHX,RP,GPHX,RP,GPHXRP,HX,GP</td>"
        "<td>1.842.041.891.981.81</td><td>-16.7-11.0-21.60.025.8</td>"
        "<td>26.1460.0-118.9465.3-125.3</td></tr></table>"
    )
    plan, records, _, unresolved = _compile(raw)
    recovered = [
        record
        for record in records
        if record.subject_source == "collapsed_material_code_sequence_recovery_phase104"
    ]
    assert plan.topology == "entity_by_property"
    assert len(recovered) == 42
    assert {record.subject for record in recovered} == {
        "TNT", "RDX", "HMX(β)", "PETN", "NTO", "NG", "NC", "AN", "AP",
        "TNAZ", "CL-20(HNIW)", "FOX-7", "ONC", "ADN",
    }
    assert any(
        record.subject == "CL-20(HNIW)"
        and record.property_name == "密度"
        and record.value_text == "2.04"
        and record.unit == "g/cm3"
        for record in recovered
    )
    assert any(
        record.subject == "RDX"
        and record.property_name == "生成能"
        and record.value_text == "92.6"
        and record.unit == "kJ/mol"
        for record in recovered
    )
    assert any(item["reason"] == "collapsed_row_series_recovered_phase104" for item in unresolved)


def test_collapsed_series_registry_is_not_globally_promoted():
    from book_engine.routing.subject_registry_builder import SubjectEvidence, _score_group

    def evidence(name: str, source: str = "collapsed_material_code_sequence_recovery_phase104"):
        return SubjectEvidence(
            record_id=f"record:{name}:{source}",
            table_id="T104",
            raw_name=name,
            source=source,
            heading="1.1 含能材料性能",
            preceding_text="表1.1一些现用的和新的含能材料的性能",
            following_text="",
            record_status="ready",
            confidence=0.859,
        )

    score, reasons, _, hard_reject = _score_group(
        "CL-20(HNIW)", [evidence("CL-20(HNIW)") for _ in range(3)]
    )
    assert not hard_reject
    assert score < 0.70
    assert "count_bound_collapsed_series_material" not in reasons

    arbitrary_score, arbitrary_reasons, _, _ = _score_group(
        "ABCXYZ", [evidence("ABCXYZ") for _ in range(3)]
    )
    assert arbitrary_score < 0.70
    assert "count_bound_collapsed_series_material" not in arbitrary_reasons

