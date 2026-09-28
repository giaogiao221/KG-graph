from __future__ import annotations

from book_engine.core.schemas import ConditionAtom, ConditionalFactRecord, TableBlock
from book_engine.document.block_segmenter import TextBlock
from book_engine.routing.block_role_classifier import classify_blocks
from book_engine.routing.heading_subject_resolver import classify_heading_subject, strip_heading_number
from book_engine.routing.subject_name_normalizer import normalize_subject_name
from book_engine.routing.text_subject_anchor_resolver import resolve_text_subject_anchors_with_audit
from book_engine.tables.context_metadata_extractor import ContextMetadata, extract_context_metadata
from book_engine.tables.record_refiner import (
    _normalize_analytical_property,
    _repair_causal_response_records,
    refine_record,
)
from book_engine.tables.semantic_features import analytical_property_leaf, lexical_scores
from book_engine.text.text_fact_extractor import _looks_like_toc_page_line


def _record(subject: str, prop: str, value: str, column: int, *, conditions=None) -> ConditionalFactRecord:
    return ConditionalFactRecord(
        record_id=f"T1-R1-C{column}", table_id="T1", row_index=1, column_index=column,
        subject=subject, subject_type="material", subject_source="table_entity_axis",
        property_name=prop, property_role="property", value_text=value,
        normalized_value_text=value, value_role="measured_result", evidence="<table></table>",
        confidence=0.9, record_status="ready", conditions=list(conditions or []),
    )


def _text_block(text: str, path: list[str]) -> TextBlock:
    return classify_blocks([TextBlock("b1", text, 1, 1, path, path[-1], len(path))])[0]


def test_section_number_stripping_preserves_chemical_locants():
    assert strip_heading_number("3.3.3 3,3-二叠氮甲基氧杂丁环均聚物") == "3,3-二叠氮甲基氧杂丁环均聚物"
    assert strip_heading_number("2.4.1 2,4,6-三硝基甲苯") == "2,4,6-三硝基甲苯"


def test_method_heading_is_not_certified_as_material_entity():
    result = classify_heading_subject("P-R方法")
    assert result.kind == "topic_only"
    assert not result.entity


def test_parenthetical_material_variants_are_preserved():
    assert normalize_subject_name("GAP-PEG(200)").canonical_name == "GAP-PEG(200)"
    assert normalize_subject_name("共聚物(1)").canonical_name == "共聚物(1)"


def test_explicit_product_and_nearest_leaf_heading_precedence():
    product = TableBlock(
        "T1", "html", "<table></table>", 1, 1,
        heading="改性GAP", preceding_text="产物PECH-HTPB-PECH的得率与投料比关系",
        heading_path=["第3章 GAP", "3.3 改性GAP"],
    )
    assert extract_context_metadata(product).subject == "PECH-HTPB-PECH"

    leaf = TableBlock(
        "T2", "html", "<table></table>", 1, 1,
        heading="性能", heading_path=["第3章 叠氮聚丙烯", "3.4 BAMO-AMMO基 ETPE", "3.4.1 性能"],
    )
    assert extract_context_metadata(leaf).subject == "BAMO-AMMO基 ETPE"


def test_bare_sample_code_rebinds_under_hierarchical_context():
    item = _record("D", "实际Mw", "2800", 2)
    item.sample_id = "D"
    item.subject_source = "row_header"
    output, _, _ = refine_record(
        item,
        metadata=ContextMetadata(subject="BAMO-AMMO基 ETPE", subject_source="heading_path:2", subject_confidence=0.76),
    )
    assert output[0].subject == "BAMO-AMMO基 ETPE"
    assert output[0].sample_id == "D"
    assert any(atom.normalized_name == "样品代号" and atom.value_text == "D" for atom in output[0].conditions)


def test_generic_parenthetical_sample_rebinds_but_specific_variant_does_not():
    generic = _record("共聚物(1)", "Mw", "2800", 2)
    generic.subject_source = "row_header"
    output, _, _ = refine_record(
        generic,
        metadata=ContextMetadata(subject="GAP-BAMO嵌段ETPE", subject_source="preceding_text", subject_confidence=0.9),
    )
    assert output[0].subject == "GAP-BAMO嵌段ETPE"
    assert output[0].sample_id == "1"

    specific = _record("GAP-PEG(200)", "DSC结果 / T", "284", 2)
    specific.subject_source = "row_header"
    output, _, _ = refine_record(specific, metadata=ContextMetadata(subject="GAP-PEG共聚物", subject_confidence=0.9))
    assert output[0].subject == "GAP-PEG(200)"


def test_molecular_ratio_and_analytical_leaf_are_preserved():
    ratio = _record("ETPE", "Mw/M", "1.37", 2)
    output, _, _ = refine_record(ratio, metadata=ContextMetadata())
    assert output[0].property_name == "分散度"
    assert output[0].unit == ""
    footnoted = _record("ETPE", "Mn / Mw@", "1.37", 3)
    footnoted_output, _, _ = refine_record(footnoted, metadata=ContextMetadata())
    assert footnoted_output[0].property_name == "分散度"
    assert footnoted_output[0].unit == ""

    assert analytical_property_leaf("DSC结果/Texon/C") == "Texon"
    scores = lexical_scores("DSC结果/Td/C")
    assert scores["property"] == 1.0
    assert scores["group"] == 0.0
    assert _normalize_analytical_property("DTG - TG结果Td/C") == ("DTG-TG分解温度Td", "℃")


def test_explicit_composition_owner_overrides_heading_default():
    block = _text_block(
        "激光点火系统一般由激光器、光导纤维和激光点火器三部分组成。",
        ["第4章 动力源装置", "4.2 组成"],
    )
    anchors, _, _ = resolve_text_subject_anchors_with_audit([block], [])
    assert anchors[0].subject == "激光点火系统"
    assert anchors[0].source == "explicit_local_fact_owner"


def test_toc_page_line_is_not_a_fact_source():
    assert _looks_like_toc_page_line("第5章 含能聚合物的应用：205")
    assert _looks_like_toc_page_line("5.3 含能推进剂........218")
    assert not _looks_like_toc_page_line("含能聚合物的密度为1.20 g/cm³。")



def test_coordinated_caption_does_not_leave_dangling_topic_connector():
    block = TableBlock(
        "T3", "html", "<table></table>", 1, 1,
        heading="BAMO-AMMO基 ETPE",
        preceding_text="表4-7 BAMO-AMMO-BAMO三嵌段ETPE的组成及相对分子质量",
    )
    metadata = extract_context_metadata(block)
    assert metadata.subject == "BAMO-AMMO-BAMO三嵌段ETPE"

def test_causal_axis_repair_keeps_depth_as_condition_when_response_is_bound_atom():
    response = ConditionAtom(
        condition_id="resp", name="熄灭长度", normalized_name="熄灭长度",
        condition_type="measured_response", value_text="54", unit="mm", scope="row",
        priority=95, confidence=0.95, target_row=1, target_column=4,
    )
    records = [
        _record("传爆药", "药量", "8", 1, conditions=[response]),
        _record("传爆药", "药柱直径", "24", 2),
        _record("传爆药", "埋入深度", "表面接触", 3),
    ]
    block = TableBlock("T1", "html", "<table></table>", 1, 1, preceding_text="埋入深度对铜板上熄灭长度的影响")
    repaired, events = _repair_causal_response_records(block, records)
    assert len(repaired) == 1
    assert repaired[0].property_name == "熄灭长度"
    names = {atom.normalized_name for atom in repaired[0].conditions}
    assert {"药量", "药柱直径", "埋入深度"}.issubset(names)
    assert events
