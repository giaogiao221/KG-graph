from __future__ import annotations

from book_engine.core.schemas import ConditionalFactRecord, TableBlock
from book_engine.quality.evidence_consistency import assess_row
from book_engine.routing.subject_registry_builder import build_subject_registry
from book_engine.tables.context_metadata_extractor import ContextMetadata, extract_context_metadata
from book_engine.tables.record_refiner import refine_record


def _record(subject: str = "1", source: str = "table_identifier_axis") -> ConditionalFactRecord:
    return ConditionalFactRecord(
        record_id="T1-R1-C2",
        table_id="T1",
        row_index=1,
        column_index=2,
        subject=subject,
        subject_type="formulation_or_sample",
        subject_source=source,
        sample_id=subject,
        property_name="得率",
        property_role="property",
        value_text="35",
        normalized_value_text="35",
        value_num=35.0,
        unit="%",
        value_role="measured_result",
        evidence="<table><tr><td>编号</td><td>得率/%</td></tr><tr><td>1</td><td>35</td></tr></table>",
        confidence=0.88,
        record_status="ready",
    )


def test_caption_subject_trims_condition_phrase():
    block = TableBlock(
        table_id="T1",
        source_type="html",
        raw_text="<table></table>",
        line_start=1,
        line_end=1,
        heading="5.3.3 BAMO基发射药",
        preceding_text="表5-17 TSE-015-019发射药在不同温度下的力学性能",
    )
    metadata = extract_context_metadata(block)
    assert metadata.subject == "TSE-015-019发射药"
    assert metadata.subject_source == "preceding_text"
    assert metadata.subject_confidence >= 0.9


def test_heading_synthesis_subject_beats_reaction_caption():
    block = TableBlock(
        table_id="T2",
        source_type="html",
        raw_text="<table></table>",
        line_start=1,
        line_end=1,
        heading="1）PECH的合成",
        preceding_text="表3-10反应条件对ECH聚合的影响",
    )
    metadata = extract_context_metadata(block)
    assert metadata.subject == "PECH"
    assert metadata.subject_source == "heading"


def test_identifier_only_subject_is_rebound_to_context_and_preserved_as_condition():
    record = _record()
    metadata = ContextMetadata(
        subject="PECH",
        subject_source="heading",
        subject_confidence=0.90,
    )
    output, events, unresolved = refine_record(record, metadata=metadata)
    assert unresolved == []
    assert len(output) == 1
    refined = output[0]
    assert refined.subject == "PECH"
    assert refined.subject_source == "context_identifier_rebind:heading"
    assert refined.sample_id == "1"
    assert refined.record_status == "candidate"
    assert "subject_from_context_requires_registry_confirmation" in refined.unresolved_reasons
    assert any(atom.normalized_name == "样品编号" and atom.value_text == "1" for atom in refined.conditions)
    assert any("rebound_identifier_only_subject_to_context_subject" in event.reasons for event in events)


def test_identifier_only_subject_without_context_fails_closed():
    output, events, unresolved = refine_record(_record(), metadata=ContextMetadata())
    refined = output[0]
    assert refined.subject == "1"
    assert refined.record_status == "unresolved"
    assert "identifier_only_subject_without_context" in refined.unresolved_reasons
    assert unresolved[0]["reason"] == "identifier_only_subject_without_context"


def test_subject_registry_never_confirms_bare_identifier():
    block = TableBlock("T1", "html", "<table></table>", 1, 1, heading="PECH的合成")
    record = _record(subject="6@")
    result = (block, object(), object(), [], [record], [], [], ContextMetadata())
    entries, resolutions, registry = build_subject_registry([result])
    assert len(entries) == 1
    assert entries[0].status == "rejected"
    assert "identifier_only_subject" in entries[0].negative_reasons
    assert resolutions[0].action == "reject"


def test_production_evidence_gate_rejects_numeric_text_subject():
    row = {
        "主体名称": "5",
        "attribute_name": "应用场景",
        "predicate_raw": "应用",
        "尾实体/取值文本": "205",
        "来源类型": "text_labeled_field",
        "证据文本": "5章含能聚合物的应用：205",
        "章节路径": "目录",
        "置信度": "0.90",
        "attribute_category": "应用",
        "_property_alignment_status": "aligned_exact",
    }
    assessment = assess_row(row)
    assert "identifier_only_subject" in assessment.hard_reject_reasons


def test_alphanumeric_material_code_is_not_identifier_only():
    block = TableBlock("T1", "html", "<table></table>", 1, 1, heading="发射药")
    record = _record(subject="JA2", source="table_identifier_axis")
    result = (block, object(), object(), [], [record], [], [], ContextMetadata())
    entries, _, _ = build_subject_registry([result])
    assert "identifier_only_subject" not in entries[0].negative_reasons


def test_identifier_context_rebind_can_confirm_specific_caption_subject():
    block = TableBlock(
        "T1", "html", "<table></table>", 1, 1,
        heading="5.3.3 BAMO基发射药",
        preceding_text="表5-17 TSE-015-019发射药在不同温度下的力学性能",
    )
    refined = refine_record(
        _record(),
        metadata=ContextMetadata(
            subject="TSE-015-019发射药",
            subject_source="preceding_text",
            subject_confidence=0.92,
        ),
    )[0][0]
    result = (block, object(), object(), [], [refined], [], [], ContextMetadata())
    entries, resolutions, _ = build_subject_registry([result])
    assert entries[0].status == "confirmed"
    assert entries[0].score >= 0.70
    assert "explicit_identifier_context_rebind" in entries[0].reasons
    assert resolutions[0].action == "accept"


def test_caption_subject_drops_residual_chemical_property_descriptor():
    block = TableBlock(
        "T1", "html", "<table></table>", 1, 1,
        heading="3)GAP的性质",
        preceding_text="表3-15GAP的理化性质",
    )
    metadata = extract_context_metadata(block)
    assert metadata.subject == "GAP"


def test_collapsed_multirow_identifier_and_integer_are_quarantined():
    record = _record(subject="123")
    record.value_text = "38591166444214"
    record.normalized_value_text = record.value_text
    output, _, unresolved = refine_record(
        record,
        metadata=ContextMetadata(
            subject="THF与BAMO三嵌段共聚物",
            subject_source="heading",
            subject_confidence=0.90,
        ),
    )
    assert output[0].record_status == "unresolved"
    assert "suspected_collapsed_multirow_numeric_series" in output[0].unresolved_reasons
    assert any(item["reason"] == "suspected_collapsed_multirow_numeric_series" for item in unresolved)


def test_paired_property_axis_cell_is_not_emitted_as_value():
    record = _record(subject="GAP", source="context:preceding_text")
    record.property_name = "密度"
    record.value_text = "活化能"
    record.normalized_value_text = "活化能"
    record.value_num = None
    record.value_role = "qualitative_description"
    record.column_header_path = ["项目"]
    output, _, unresolved = refine_record(record, metadata=ContextMetadata())
    assert output[0].record_status == "unresolved"
    assert "property_axis_cell_misused_as_value" in output[0].unresolved_reasons
    assert any(item["reason"] == "property_axis_cell_misused_as_value" for item in unresolved)


def test_identifier_ocr_footnote_zero_is_normalized_from_table_note():
    record = _record(subject="10")
    record.evidence = (
        "<table><tr><td>编号</td></tr><tr><td>10</td></tr>"
        "<tr><td>注：编号1为初始样；编号4为28个月后测定样</td></tr></table>"
    )
    output, events, _ = refine_record(
        record,
        metadata=ContextMetadata(
            subject="TSE-015-019发射药",
            subject_source="preceding_text",
            subject_confidence=0.92,
        ),
    )
    refined = output[0]
    assert refined.sample_id == "1"
    assert any(atom.normalized_name == "样品编号" and atom.value_text == "1" for atom in refined.conditions)
    assert any("normalized_identifier_ocr_footnote_zero_from_table_note" in event.reasons for event in events)


def test_identifier_explicit_footnote_marker_is_stripped():
    record = _record(subject="6@")
    output, _, _ = refine_record(
        record,
        metadata=ContextMetadata(
            subject="TSE-015-019发射药",
            subject_source="preceding_text",
            subject_confidence=0.92,
        ),
    )
    assert output[0].sample_id == "6"
    assert any(atom.normalized_name == "样品编号" and atom.value_text == "6" for atom in output[0].conditions)
