from book_engine.gates.table_record_gate import RecordGateDecision
from book_engine.gates.text_fact_gate import TextGateDecision
from book_engine.routing.subject_registry_builder import SubjectRegistryEntry
from book_engine.routing.subject_type_harmonizer import harmonize_subject_types


def test_more_specific_subject_type_is_shared_across_sources():
    registry = SubjectRegistryEntry(
        subject_id="s1",
        canonical_name="GAP",
        normalized_key="gap",
        subject_type="高聚物",
        status="confirmed",
        score=0.9,
        type_confidence=0.9,
    )
    table_decision = RecordGateDecision(
        record_id="t1", table_id="T1", accepted=True, action="export",
        canonical_subject="GAP", subject_id="s1", subject_type="高聚物",
        original_status="ready", final_status="ready", confidence=0.9,
    )
    text_decision = TextGateDecision(
        record_id="x1", accepted=True, action="export",
        canonical_subject="GAP", subject_type="材料", confidence=0.8,
    )
    table, text, audits = harmonize_subject_types(
        [(object(), table_decision, object())],
        [(object(), text_decision)],
        [registry],
    )
    assert table[0][1].subject_type == "高聚物"
    assert text[0][1].subject_type == "高聚物"
    assert any(item.changed for item in audits)


def test_source_defined_specific_type_is_not_downgraded_to_material():
    registry = SubjectRegistryEntry(
        subject_id="s2",
        canonical_name="RDX",
        normalized_key="rdx",
        subject_type="单质炸药",
        status="confirmed",
        score=0.9,
        type_confidence=0.9,
    )
    text_decision = TextGateDecision(
        record_id="x2", accepted=True, action="export",
        canonical_subject="RDX", subject_type="单质炸药", confidence=0.8,
    )
    _, text, _ = harmonize_subject_types([], [(object(), text_decision)], [registry])
    assert text[0][1].subject_type == "单质炸药"
