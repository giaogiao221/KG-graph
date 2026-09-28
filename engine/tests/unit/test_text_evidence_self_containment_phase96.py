from __future__ import annotations

from pathlib import Path

from book_engine.document.block_segmenter import TextBlock
from book_engine.export.text_audit_writer import write_text_outputs
from book_engine.gates.text_fact_gate import gate_text_facts
from book_engine.routing.block_role_classifier import classify_blocks
from book_engine.routing.text_subject_anchor_resolver import TextSubjectAnchor, resolve_text_subject_anchors_with_audit
from book_engine.text.evidence_self_containment import assess_text_fact, compact_ocr_numeric_text
from book_engine.text.text_fact_extractor import TextFactRecord, extract_text_facts


def _block(text: str, heading: str = "复合火药") -> TextBlock:
    return TextBlock("b1", text, 1, 1, [heading], heading, 1, role="property", role_confidence=0.9)


def _anchor(subject: str = "复合火药", source: str = "nearest_entity_heading_anchor") -> TextSubjectAnchor:
    return TextSubjectAnchor("b1", subject, "材料", source, 0.92, "confirmed", [])


def _records(text: str, subject: str = "复合火药"):
    block = _block(text)
    anchor = _anchor(subject)
    return extract_text_facts([block], {block.block_id: anchor}), block, anchor


def test_incomplete_visual_or_list_composition_is_held_for_audit():
    records, block, anchor = _records("图6表示点火系统。由下列元件组成：")
    compositions = [r for r in records if r.relation_kind == "composition"]
    assert len(compositions) == 1
    accepted, decisions = gate_text_facts(compositions, {block.block_id: anchor})
    assert not accepted
    assert "external_visual_or_list_required" in decisions[0].reasons


def test_false_composition_trigger_from_figure_is_held_for_audit():
    records, block, anchor = _records("由图4-2、4-3可看出：聚氨酯复合火药的比冲随组成的变化而变化。")
    compositions = [r for r in records if r.relation_kind == "composition"]
    assert len(compositions) == 1
    accepted, decisions = gate_text_facts(compositions, {block.block_id: anchor})
    assert not accepted
    assert "false_composition_trigger" in decisions[0].reasons


def test_named_composition_is_self_contained_and_released():
    records, block, anchor = _records("复合火药由氧化剂、粘合剂和金属燃料组成。")
    compositions = [r for r in records if r.relation_kind == "composition"]
    assert len(compositions) == 1
    assert compositions[0].subject == "复合火药"
    assert compositions[0].value_text == "由氧化剂、粘合剂和金属燃料组成"
    accepted, decisions = gate_text_facts(compositions, {block.block_id: anchor})
    assert len(accepted) == 1
    assert decisions[0].evidence_self_contained


def test_ownerless_modifier_uses_safe_heading_anchor_not_word_mainly():
    records, _, _ = _records("主要是由高分子粘合剂、无机氧化剂和金属粉燃料所组成的。")
    compositions = [r for r in records if r.relation_kind == "composition"]
    assert len(compositions) == 1
    assert compositions[0].subject == "复合火药"
    assert compositions[0].subject != "主要"


def test_explicit_nepe_owner_is_cleaned():
    records, _, _ = _records("许多资料报道，NEPE推进剂是由PEG、辅助粘合剂和异氰酸酯交联剂组成的粘合剂体系。")
    compositions = [r for r in records if r.relation_kind == "composition"]
    assert len(compositions) == 1
    assert compositions[0].subject == "NEPE推进剂"


def test_deictic_composition_owner_does_not_override_entity_heading():
    block = classify_blocks([
        TextBlock("b1", "该系统由连续送料机、混合机和除气机组成。", 1, 1,
                  ["第6章 连续混合系统"], "连续混合系统", 1)
    ])[0]
    anchors, by_block, _ = resolve_text_subject_anchors_with_audit([block], [])
    assert anchors[0].subject == "连续混合系统"
    records = extract_text_facts([block], by_block)
    compositions = [r for r in records if r.relation_kind == "composition"]
    assert len(compositions) == 1
    assert compositions[0].subject == "连续混合系统"


def test_equipment_owner_is_recognized_for_following_ownerless_clause():
    block = classify_blocks([
        TextBlock("b1", "振能磨的结构如图4-9所示。由一个环形研磨室构成。", 1, 1,
                  ["第4章 粉碎设备"], "粉碎设备", 1)
    ])[0]
    anchors, by_block, _ = resolve_text_subject_anchors_with_audit([block], [])
    assert anchors[0].subject == "振能磨"
    records = extract_text_facts([block], by_block)
    compositions = [r for r in records if r.relation_kind == "composition"]
    assert len(compositions) == 1
    assert compositions[0].subject == "振能磨"


def test_numeric_extractor_binds_multiple_local_owners():
    records, _, _ = _records("复合火药的密度为1.70 g/cm³，双基火药的密度为1.58 g/cm³。")
    density = [r for r in records if r.property_name == "密度"]
    assert {(r.subject, r.value_num) for r in density} == {
        ("复合火药", 1.70), ("双基火药", 1.58),
    }


def test_composition_extraction_is_clause_local():
    records, _, _ = _records("该体系含有EO和PO；研究者指出其性能随组成变化。")
    assert not [r for r in records if r.relation_kind == "composition"]


def test_count_only_composition_is_held():
    records, block, anchor = _records("航天飞机由四个主要部份组成(图1)。", subject="航天飞机")
    compositions = [r for r in records if r.relation_kind == "composition"]
    assert len(compositions) == 1
    accepted, decisions = gate_text_facts(compositions, {block.block_id: anchor})
    assert not accepted
    assert {"external_visual_or_list_required", "incomplete_composition_description"} & set(decisions[0].reasons)


def test_ocr_spaced_number_compaction():
    # OCR-spaced scalars are compacted when a numeric field has already been
    # identified.  The general sentence extractor stays conservative and does
    # not broaden its numeric regex merely to recover every spaced sequence.
    assert compact_ocr_numeric_text("$6 5 8 . 9 \\mathrm { C }$") == "$658.9 \\mathrm { C }$"


def test_visual_reference_does_not_block_explicit_scalar_fact():
    record = TextFactRecord(
        record_id="r1", block_id="b1", subject="AP", subject_type="材料",
        property_name="压力指数", value_text="0.2", value_num=0.2,
        normalized_value_text="0.2", source_type="text_explicit_numeric_property",
        evidence="由图3-15可知，平台区压力指数为0.2。", fact_clause="由图3-15可知，平台区压力指数为0.2。",
        confidence=0.9, record_status="ready",
    )
    assessment = assess_text_fact(record)
    assert assessment.self_contained
    assert assessment.visual_reference


def test_visual_dependency_audit_is_written(tmp_path: Path):
    block = _block("图6表示点火系统。由下列元件组成：")
    anchor = _anchor()
    record = TextFactRecord(
        record_id="r1", block_id="b1", subject="复合火药", subject_type="材料",
        property_name="组成描述", value_text="由下列元件组成", relation_kind="composition",
        source_type="text_composition_description", evidence=block.text, fact_clause="由下列元件组成",
        confidence=0.8, record_status="ready",
    )
    _, decisions = gate_text_facts([record], {"b1": anchor})
    report = write_text_outputs(tmp_path, [block], [anchor], [record], decisions)
    audit = tmp_path / "step_visual_dependency" / "visual_dependency_audit.tsv"
    assert audit.exists()
    assert report["visual_or_list_dependency_held"] == 1
