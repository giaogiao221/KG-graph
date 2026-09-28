from __future__ import annotations

from book_engine.core.schemas import ConditionCandidate, TableBlock
from book_engine.tables.condition_extractor import extract_condition_candidates
from book_engine.tables.condition_scope_resolver import bind_conditions
from book_engine.tables.grid_rebuilder import rebuild_grid
from book_engine.tables.header_tree_builder import build_header_tree
from book_engine.tables.record_compiler import compile_conditional_records
from book_engine.tables.semantic_planner import build_semantic_plan
from book_engine.tables.value_parser import parse_value


def _prepare(raw_table: str, heading: str = "", preceding: str = "", following: str = ""):
    block = TableBlock(
        table_id="T00001",
        source_type="html",
        raw_text=raw_table,
        line_start=1,
        line_end=5,
        heading=heading,
        preceding_text=preceding,
        following_text=following,
    )
    grid = rebuild_grid(block)
    tree = build_header_tree(grid)
    plan, axis_roles, cell_roles, topology = build_semantic_plan(block, grid, tree)
    candidates = extract_condition_candidates(block, grid, tree, plan, axis_roles, cell_roles)
    records, bindings, unresolved, metadata = compile_conditional_records(
        block, grid, tree, plan, axis_roles, cell_roles, candidates
    )
    return block, grid, tree, plan, candidates, records, bindings, unresolved, metadata


def test_value_parser_range_comparator_and_ratio():
    value = parse_value("≥ 7.5 MPa")
    assert value.value_num == 7.5
    assert value.comparator == ">="
    assert value.unit == "MPa"

    value = parse_value("70～80 ℃")
    assert value.lower_bound == 70
    assert value.upper_bound == 80
    assert value.unit == "℃"

    value = parse_value("1:1")
    assert value.is_ratio
    assert value.lower_bound == 1
    assert value.upper_bound == 1


def test_context_conditions_are_extracted_with_sources():
    raw = """<table><tr><th>样品</th><th>峰顶温度/℃</th></tr><tr><td>炸药A/F2311</td><td>380</td></tr></table>"""
    _, _, _, _, candidates, records, _, _, metadata = _prepare(
        raw,
        heading="炸药A/F2311的DTA测试结果",
        preceding="试验温度为100℃，环境温度为23℃，混合质量比为1:1，采用DTA法。",
    )
    names = {item.normalized_name for item in candidates}
    assert "试验温度" in names
    assert "环境温度" in names
    assert "质量比" in names
    assert metadata.method
    assert records
    assert any(atom.normalized_name == "试验温度" for atom in records[0].conditions)


def test_condition_by_property_binds_row_pressure_to_result():
    raw = """<table>
    <tr><th>压力/MPa</th><th>燃速/(mm/s)</th></tr>
    <tr><td>5</td><td>8.1</td></tr>
    <tr><td>10</td><td>12.4</td></tr>
    </table>"""
    _, _, _, plan, candidates, records, bindings, unresolved, _ = _prepare(
        raw,
        heading="推进剂P1的燃速性能",
    )
    assert plan.topology == "condition_by_property"
    burning_records = [record for record in records if "燃速" in record.property_name]
    assert len(burning_records) == 2
    assert {record.subject for record in burning_records} == {"推进剂P1"}
    assert any(atom.value_num == 5 for atom in burning_records[0].conditions)
    assert any(binding.applied for binding in bindings)


def test_composition_and_performance_carries_composition_conditions():
    raw = """<table>
    <tr><th>配方号</th><th>AP/%</th><th>Al/%</th><th>HTPB/%</th><th>比冲/s</th></tr>
    <tr><td>P1</td><td>70</td><td>15</td><td>15</td><td>260</td></tr>
    </table>"""
    _, _, _, plan, candidates, records, _, _, _ = _prepare(raw)
    assert plan.topology == "composition_and_performance"
    performance = [record for record in records if "比冲" in record.property_name]
    assert len(performance) == 1
    assert performance[0].subject == "P1"
    composition_names = {atom.normalized_name for atom in performance[0].conditions if atom.condition_type == "composition"}
    assert any("AP" in name for name in composition_names)
    assert any("Al" in name for name in composition_names)
    assert any("HTPB" in name for name in composition_names)


def test_more_specific_condition_overrides_table_condition():
    candidates = [
        ConditionCandidate(
            condition_id="T-C1",
            table_id="T",
            name="温度",
            normalized_name="试验温度",
            condition_type="environment",
            value_text="100℃",
            scope="table",
            priority=200,
            confidence=0.9,
        ),
        ConditionCandidate(
            condition_id="T-C2",
            table_id="T",
            name="温度",
            normalized_name="试验温度",
            condition_type="environment",
            value_text="120℃",
            scope="cell",
            target_row=1,
            target_column=2,
            priority=400,
            confidence=0.9,
        ),
    ]
    atoms, bindings, unresolved = bind_conditions(candidates, "R1", "T", 1, 2)
    assert len(atoms) == 1
    assert atoms[0].value_text == "120℃"
    assert not unresolved
    assert any(binding.overridden for binding in bindings)


def test_no_subject_does_not_force_record_generation():
    raw = """<table><tr><th>压力/MPa</th><th>燃速/(mm/s)</th></tr><tr><td>5</td><td>8.1</td></tr></table>"""
    _, _, _, _, _, records, _, unresolved, _ = _prepare(raw, heading="压力与燃速关系")
    assert not records
    assert any(item["reason"] == "no_subject_for_row" for item in unresolved)


def test_vertical_property_value_method_table_uses_context_subject():
    raw = """<table>
    <tr><th>项目</th><th>理化指标</th><th>检验方法</th></tr>
    <tr><td>熔点/℃</td><td>≥77.0</td><td>熔点仪法</td></tr>
    <tr><td>外观</td><td>无肉眼可见杂质</td><td>目视法</td></tr>
    </table>"""
    _, _, _, _, _, records, _, _, _ = _prepare(
        raw,
        preceding="表1-1 TPB理化指标和检验方法",
    )
    assert {record.subject for record in records} == {"TPB"}
    assert {record.property_name for record in records} == {"熔点", "外观"}
    assert any(record.method == "熔点仪法" for record in records)


def test_property_rows_with_material_columns_are_transposed_correctly():
    raw = """<table>
    <tr><th>性能</th><th>木材</th><th>HMX</th></tr>
    <tr><td>放热/(J/g)</td><td>29260</td><td>5664</td></tr>
    <tr><td>释能时间/s</td><td>60</td><td>1</td></tr>
    </table>"""
    _, _, _, _, _, records, _, _, _ = _prepare(raw)
    assert {(record.subject, record.property_name) for record in records} == {
        ("木材", "放热"), ("HMX", "放热"), ("木材", "释能时间"), ("HMX", "释能时间")
    }


def test_condition_like_property_axis_is_not_emitted_as_result():
    raw = """<table>
    <tr><th>组分名称</th><th>平均粒径/um</th><th>级配比例</th><th>分形维数</th><th>相关系数</th></tr>
    <tr><td>AP_I:AP_II</td><td>435:285</td><td>70:30</td><td>0.034</td><td>0.94</td></tr>
    </table>"""
    _, _, _, _, _, records, _, _, _ = _prepare(raw, preceding="表3-4 AP粒度分布及分形计算结果")
    assert {record.property_name for record in records} == {"分形维数", "相关系数"}
    assert all(any("平均粒径" in atom.normalized_name for atom in record.conditions) for record in records)
