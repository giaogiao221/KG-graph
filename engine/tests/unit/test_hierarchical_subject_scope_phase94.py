from __future__ import annotations

from book_engine.core.schemas import ConditionalFactRecord, TableBlock
from book_engine.document.block_segmenter import TextBlock
from book_engine.document.semantic_block_splitter import split_multisubject_blocks
from book_engine.quality.evidence_consistency import assess_row
from book_engine.routing.block_role_classifier import classify_blocks
from book_engine.routing.heading_subject_resolver import classify_heading_subject
from book_engine.routing.subject_registry_builder import build_subject_registry
from book_engine.routing.text_subject_anchor_resolver import resolve_text_subject_anchors_with_audit
from book_engine.tables.context_metadata_extractor import extract_context_metadata


def _block(text: str, path: list[str], title: str, block_id: str = "b") -> TextBlock:
    block = TextBlock(block_id, text, 1, 1, path, title, len(path))
    return classify_blocks([block])[0]


def test_nearest_entity_parent_heading_anchors_topic_child():
    block = _block("密度为1.80 g/cm³。", ["第3章 RDX", "3.1 物理性质"], "3.1 物理性质")
    anchors, _, _ = resolve_text_subject_anchors_with_audit([block], [])
    assert anchors[0].subject == "RDX"
    assert anchors[0].source == "nearest_entity_heading_anchor"
    assert anchors[0].status == "confirmed"


def test_explicit_local_owner_overrides_entity_heading_default():
    block = _block("TNT的密度为1.65 g/cm³。", ["第3章 RDX", "3.1 物理性质"], "3.1 物理性质")
    anchors, _, _ = resolve_text_subject_anchors_with_audit([block], [])
    assert anchors[0].subject == "TNT"
    assert anchors[0].source == "explicit_local_fact_owner"


def test_comparison_reference_does_not_override_heading_default():
    block = _block("与TNT相比，该材料的密度更高。", ["第3章 RDX", "3.1 物理性质"], "3.1 物理性质")
    anchors, _, _ = resolve_text_subject_anchors_with_audit([block], [])
    assert anchors[0].subject == "RDX"
    assert anchors[0].source == "nearest_entity_heading_anchor"


def test_two_explicit_fact_owners_are_split_and_anchored_independently():
    block = _block(
        "RDX的密度为1.80 g/cm³；TNT的密度为1.65 g/cm³。",
        ["第3章 RDX", "3.1 物理性质"],
        "3.1 物理性质",
    )
    children, audits = split_multisubject_blocks([block], [])
    assert len(children) == 2
    anchors, _, _ = resolve_text_subject_anchors_with_audit(children, [])
    assert [item.subject for item in anchors] == ["RDX", "TNT"]
    assert all(item.source == "explicit_local_fact_owner" for item in anchors)
    assert sum(item.action == "split" for item in audits) == 2


def test_entity_plus_topic_heading_returns_entity_span_only():
    result = classify_heading_subject("5.2 HTPB推进剂的力学性能")
    assert result.kind == "entity_with_topic"
    assert result.entity == "HTPB推进剂"


def test_theory_book_style_heading_is_not_an_entity_anchor():
    result = classify_heading_subject("第1章 炸药一般特征")
    assert result.kind == "topic_only"
    assert not result.entity


def test_root_broad_class_needs_local_confirmation():
    implicit = _block("密度通常较高。", ["含能聚合物"], "含能聚合物")
    explicit = _block("含能聚合物的密度通常较高。", ["含能聚合物"], "含能聚合物", "b2")
    anchors, _, _ = resolve_text_subject_anchors_with_audit([implicit, explicit], [])
    assert anchors[0].subject == "含能聚合物"
    assert anchors[0].status == "candidate"
    assert anchors[1].subject == "含能聚合物"
    assert anchors[1].status == "confirmed"
    assert anchors[1].source == "explicit_local_fact_owner"


def test_table_uses_parent_entity_heading_when_leaf_is_topic():
    block = TableBlock(
        "T1", "html", "<table></table>", 1, 1,
        heading="力学性能",
        heading_path=["第3章 HTPB推进剂", "3.2 力学性能"],
    )
    metadata = extract_context_metadata(block)
    assert metadata.subject == "HTPB推进剂"
    assert metadata.subject_source.startswith("heading_path:")


def test_table_local_caption_overrides_parent_heading_entity():
    block = TableBlock(
        "T1", "html", "<table></table>", 1, 1,
        heading="物理性质",
        preceding_text="表3-2 TNT的密度和爆速",
        heading_path=["第3章 RDX", "3.1 物理性质"],
    )
    metadata = extract_context_metadata(block)
    assert metadata.subject == "TNT"
    assert metadata.subject_source == "preceding_text"


def test_topic_word_subject_is_rejected_by_registry_generically():
    block = TableBlock("T1", "html", "<table></table>", 1, 1, heading="原材料")
    record = ConditionalFactRecord(
        record_id="r1", table_id="T1", row_index=1, column_index=1,
        subject="原材料", subject_type="材料", subject_source="row_header",
        property_name="密度", property_role="property", value_text="1.2",
        normalized_value_text="1.2", value_num=1.2, value_role="measured_result",
        evidence="<table></table>", confidence=0.9, record_status="ready",
    )
    entries, _, _ = build_subject_registry([(block, None, None, None, [record])])
    assert entries[0].status == "rejected"
    assert "generic_or_topic_subject" in entries[0].negative_reasons


def test_full_descriptive_heading_cannot_reach_final_graph():
    assessment = assess_row({
        "主体名称": "RDX的热分解性能",
        "attribute_name": "分解温度",
        "predicate_raw": "分解温度",
        "尾实体/取值文本": "210 ℃",
        "证据文本": "RDX的热分解性能：分解温度为210 ℃。",
        "来源类型": "text_explicit_numeric_property",
        "置信度": "0.90",
        "attribute_category": "热性能",
        "_property_alignment_status": "aligned_exact",
    })
    assert "descriptive_heading_used_as_subject" in assessment.hard_reject_reasons


def test_topic_morphology_is_not_certified_by_material_word_inside_title():
    for title in (
        "环张力、取代基和溶剂效应对环醚聚合",
        "炸药释能与化学组成及反应速度",
        "炸药分类-释能方式",
        "基于 Hess 定律计算标准",
        "药剂方面",
    ):
        result = classify_heading_subject(title)
        assert result.kind == "topic_only", title
        assert not result.entity


def test_device_entity_is_recovered_from_descriptive_heading():
    expectations = {
        "火帽的检验": "火帽",
        "电点火具的作用过程": "电点火具",
        "LD-1火花式电雷管的构造": "LD-1火花式电雷管",
        "典型导电药电雷管构造的举例": "导电药电雷管",
        "影响导电药式电雷管性能的因素": "导电药式电雷管",
    }
    for title, entity in expectations.items():
        result = classify_heading_subject(title)
        assert result.kind == "entity_with_topic"
        assert result.entity == entity


def test_middle_scope_heading_recovers_material_entity():
    result = classify_heading_subject("粉状炸药中 LVD 的传播")
    assert result.entity == "粉状炸药"
    assert result.kind == "entity_with_topic"


def test_definition_heading_recovers_only_entity_span():
    result = classify_heading_subject("火炸药是比能材料")
    assert result.entity == "火炸药"
    assert result.kind == "entity_with_topic"


def test_registry_cannot_certify_a_topic_phrase():
    class Entry:
        status = "confirmed"
        canonical_name = "环张力、取代基和溶剂效应对环醚聚合"
        aliases = []

    result = classify_heading_subject(Entry.canonical_name, [Entry()])
    assert result.kind == "topic_only"
    assert not result.entity


def test_heading_citation_is_removed_without_losing_entity():
    result = classify_heading_subject("冲击片雷管[17]")
    assert result.entity == "冲击片雷管"
    assert result.kind == "pure_entity"


def test_condition_tail_is_not_carried_into_local_owner():
    block = _block(
        "起爆混合炸药质量分数硝酸铵/梯恩梯为90/10的密度为1.10 g/cm³。",
        ["第2章 起爆混合炸药", "2.1 性能"],
        "2.1 性能",
    )
    anchors, _, _ = resolve_text_subject_anchors_with_audit([block], [])
    assert anchors[0].subject == "起爆混合炸药"
    assert anchors[0].source == "explicit_local_fact_owner"


def test_explicit_owner_beats_heading_while_reagent_does_not():
    explicit = _block(
        "TNT的密度为1.65 g/cm³。",
        ["第3章 RDX", "3.1 物理性质"],
        "3.1 物理性质",
        "explicit",
    )
    reagent = _block(
        "向RDX中加入TNT作为添加剂后，该材料的密度略有变化。",
        ["第3章 RDX", "3.1 物理性质"],
        "3.1 物理性质",
        "reagent",
    )
    anchors, _, _ = resolve_text_subject_anchors_with_audit([explicit, reagent], [])
    assert anchors[0].subject == "TNT"
    assert anchors[0].source == "explicit_local_fact_owner"
    assert anchors[1].subject == "RDX"
    assert anchors[1].source == "nearest_entity_heading_anchor"


def test_topic_prefix_plus_broad_class_is_not_an_entity():
    result = classify_heading_subject("生成焓分类的单质炸药")
    assert result.kind == "topic_only"
    assert not result.entity


def test_coordinated_entity_heading_requires_local_resolution():
    result = classify_heading_subject("延期药和延期元件")
    assert result.kind == "multi_entity"
    assert not result.entity


def test_short_cjk_ocr_spacing_is_normalized():
    from book_engine.routing.subject_name_normalizer import normalize_subject_name

    assert normalize_subject_name("电 雷 管").canonical_name == "电雷管"
    assert normalize_subject_name("点火 具").canonical_name == "点火具"
    assert normalize_subject_name("斯蒂芬酸铅 二硝基重氮酚").canonical_name == "斯蒂芬酸铅 二硝基重氮酚"


def test_multi_entity_heading_is_a_closed_scope_not_a_default():
    selected = _block("TNT在20 ℃下保持稳定。", ["第4章 RDX和TNT", "4.1 稳定性"], "4.1 稳定性", "m1")
    unresolved = _block("两者在20 ℃下均保持稳定。", ["第4章 RDX和TNT", "4.1 稳定性"], "4.1 稳定性", "m2")
    anchors, _, _ = resolve_text_subject_anchors_with_audit([selected], [])
    assert anchors[0].subject == "TNT"
    assert anchors[0].source == "multi_heading_unique_local_mention"
    unresolved_anchors, _, _ = resolve_text_subject_anchors_with_audit([unresolved], [])
    assert unresolved_anchors[0].status == "unresolved"
