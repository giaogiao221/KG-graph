from __future__ import annotations

from book_engine.core.schemas import ConditionalFactRecord, TableBlock
from book_engine.routing.subject_name_normalizer import normalize_subject_name
from book_engine.tables.context_metadata_extractor import ContextMetadata, extract_context_metadata
from book_engine.tables.grid_rebuilder import rebuild_grid
from book_engine.tables.header_tree_builder import build_header_tree
from book_engine.tables.record_refiner import refine_record
from book_engine.tables.semantic_planner import build_semantic_plan
from book_engine.tables.value_parser import parse_value


def _record(**kwargs) -> ConditionalFactRecord:
    data = dict(
        record_id="r1",
        table_id="T1",
        row_index=1,
        column_index=2,
        subject="A",
        subject_type="formulation_or_sample",
        subject_source="row_header",
        sample_id="A",
        property_name="分散度",
        property_role="property",
        value_text="1. 37",
        normalized_value_text="1. 37",
        value_num=1.0,
        unit="",
        value_role="measured_result",
        evidence="<table></table>",
        confidence=0.88,
        record_status="ready",
    )
    data.update(kwargs)
    return ConditionalFactRecord(**data)


def test_ocr_decimal_whitespace_is_repaired_before_numeric_parse():
    assert parse_value("1. 32").value_num == 1.32
    ratio = parse_value("1. 2/0. 76")
    assert ratio.is_ratio
    assert ratio.lower_bound == 1.2
    assert ratio.upper_bound == 0.76


def test_bare_sample_code_is_rebound_to_context_subject():
    output, events, unresolved = refine_record(
        _record(),
        metadata=ContextMetadata(
            subject="BAMO-AMMO-BAMO三嵌段ETPE",
            subject_source="preceding_text",
            subject_confidence=0.92,
        ),
    )
    item = output[0]
    assert not unresolved
    assert item.subject == "BAMO-AMMO-BAMO三嵌段ETPE"
    assert item.sample_id == "A"
    assert item.value_num == 1.37
    assert any(atom.normalized_name == "样品代号" and atom.value_text == "A" for atom in item.conditions)
    assert any("rebound_bare_sample_code_to_context_subject" in event.reasons for event in events)


def test_identity_formula_is_not_projected_as_numeric_measurement():
    output, _, _ = refine_record(
        _record(
            subject="NG",
            sample_id="",
            property_name="化学式",
            value_text="C3.04H5.45O.22N2.75",
            normalized_value_text="C3.04H5.45O.22N2.75",
            value_num=3.04,
        ),
        metadata=ContextMetadata(),
    )
    item = output[0]
    assert item.normalized_value_text == "C3.04H5.45O.22N2.75"
    assert item.value_num is None
    assert item.lower_bound is None
    assert item.upper_bound is None
    assert item.value_role == "identity_text"


def test_dotted_heading_number_is_removed_as_a_whole():
    block = TableBlock(
        "T1", "html", "<table></table>", 1, 1,
        heading="4.6.2 BAMO-AMMO基 ETPE",
    )
    metadata = extract_context_metadata(block)
    assert metadata.subject == "BAMO-AMMO基 ETPE"


def test_parenthesized_product_subject_beats_sentence_caption():
    block = TableBlock(
        "T1", "html", "<table></table>", 1, 1,
        heading="反应产物",
        preceding_text="表3-8 对未分馏等规(RS)-PECH进行叠氮化反应得到产物(RS)-GAP的得率",
    )
    metadata = extract_context_metadata(block)
    assert metadata.subject == "(RS)-GAP"
    assert metadata.subject_confidence >= 0.9


def test_orphan_closing_punctuation_is_removed_from_subject():
    normalized = normalize_subject_name(")GAP/AN推进剂")
    assert normalized.canonical_name == "GAP/AN推进剂"


def test_numeric_second_level_headers_are_not_compiled_as_data_rows():
    raw = """
    <table>
      <tr><td rowspan="2">ETPEs</td><td colspan="3">燃速/(英寸/s)</td></tr>
      <tr><td>3.45</td><td>6.90</td><td>13.8</td></tr>
      <tr><td>GAP-BAMO</td><td>0.463</td><td>0.760</td><td>0.986</td></tr>
    </table>
    """
    grid = rebuild_grid(TableBlock("T1", "html", raw, 1, 5))
    tree = build_header_tree(grid)
    assert tree.header_rows == [0, 1]
    assert tree.row_header_columns == [0]
    assert tree.column_paths[1].labels == ["燃速/(英寸/s)", "3.45"]
    assert tree.row_paths[0].labels == ["GAP-BAMO"]


def test_chain_segment_column_is_available_as_composition_condition():
    raw = """
    <table>
      <tr><th>ETPE</th><th>链段</th><th>分散度</th></tr>
      <tr><td>A</td><td>AMMO</td><td>1.37</td></tr>
      <tr><td>A</td><td>BAMO</td><td>1.38</td></tr>
    </table>
    """
    block = TableBlock("T1", "html", raw, 1, 5, heading="BAMO-AMMO-BAMO三嵌段ETPE")
    grid = rebuild_grid(block)
    tree = build_header_tree(grid)
    plan, axis_roles, _, _ = build_semantic_plan(block, grid, tree)
    role_by_column = {item.index: item.role for item in axis_roles if item.axis == "column"}
    assert role_by_column[1] == "composition"
    assert any(int(item["index"]) == 1 for item in plan.composition_axes)


def test_experiment_factor_row_is_held_when_matrix_orientation_is_unresolved():
    output, _, unresolved = refine_record(
        _record(
            subject="引发剂",
            subject_source="table_entity_axis",
            sample_id="",
            property_name="BAMO",
            value_text="BDO",
            normalized_value_text="BDO",
            value_num=None,
            value_role="qualitative_description",
        ),
        metadata=ContextMetadata(),
    )
    assert output[0].record_status == "unresolved"
    assert "experimental_factor_axis_misread_as_subject" in output[0].unresolved_reasons
    assert any(item["reason"] == "experimental_factor_axis_misread_as_subject" for item in unresolved)


def test_experiment_factor_row_with_ocr_zero_material_code_is_held():
    output, _, unresolved = refine_record(
        _record(
            subject="引发剂",
            subject_source="table_entity_axis",
            sample_id="",
            property_name="BAM0",
            value_text="PhCH2OH",
            normalized_value_text="PhCH2OH",
            value_num=None,
            value_role="qualitative_description",
        ),
        metadata=ContextMetadata(),
    )
    assert output[0].record_status == "unresolved"
    assert "experimental_factor_axis_misread_as_subject" in output[0].unresolved_reasons
    assert any(item["reason"] == "experimental_factor_axis_misread_as_subject" for item in unresolved)


def test_latex_process_title_subject_is_hard_rejected():
    from book_engine.quality.evidence_consistency import assess_row

    subject = r"$\\mathrm { H B F _ { 4 } }$/醇复合引发体系引发的阳离子开环聚合"
    assessment = assess_row({
        "主体名称": subject,
        "attribute_name": "分子量",
        "predicate_raw": "分子量",
        "尾实体/取值文本": "3000",
        "证据文本": subject + "，相对分子质量为3000。",
        "来源类型": "text_explicit_numeric_property",
        "置信度": "0.90",
        "attribute_category": "物理属性",
        "_property_alignment_status": "aligned_exact",
    })
    assert "latex_process_title_used_as_subject" in assessment.hard_reject_reasons


def test_reaction_sentence_subject_is_hard_rejected():
    from book_engine.quality.evidence_consistency import assess_row

    subject = "对未分馏等规(RS)-PECH进行叠氮化反应得到产物(RS)-GAP"
    assessment = assess_row({
        "主体名称": subject,
        "attribute_name": "叠氮化率",
        "predicate_raw": "叠氮化率",
        "尾实体/取值文本": "95%",
        "证据文本": subject + "的叠氮化率为95%。",
        "来源类型": "table_conditional_record",
        "置信度": "0.90",
        "attribute_category": "反应属性",
        "_property_alignment_status": "aligned_exact",
    })
    assert "reaction_sentence_used_as_subject" in assessment.hard_reject_reasons
