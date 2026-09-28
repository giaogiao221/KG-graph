from book_engine.core.schemas import ConditionalFactRecord, TableBlock, TableSemanticPlan
from book_engine.gates.table_record_gate import gate_table_records
from book_engine.routing.subject_name_normalizer import normalize_subject_name
from book_engine.routing.subject_registry_builder import build_subject_registry
from book_engine.routing.subject_type_inferer import infer_subject_type


def _result(records, heading="材料性能"):
    block = TableBlock(
        table_id="T00001", source_type="html", raw_text="", line_start=1, line_end=3,
        heading=heading, preceding_text="HMX的性能参数如下。", following_text="",
    )
    plan = TableSemanticPlan(table_id="T00001", topology="entity_by_property", confidence=0.9)
    return (block, None, plan, [], records, [], [], None)


def test_subject_normalization_collapses_duplicate_header_path():
    normalized = normalize_subject_name("爆发点/℃ / 爆发点/℃")
    assert normalized.canonical_name == "爆发点/°C"


def test_subject_type_inference():
    assert infer_subject_type("半导体桥雷管")[0] == "火工品器件"
    assert infer_subject_type("NG/BTTN/GAP体系")[0] == "配方/材料体系"
    assert infer_subject_type("GAP预聚物")[0] == "高聚物"


def test_registry_confirms_specific_repeated_material_and_rejects_property_axis():
    records = [
        ConditionalFactRecord(
            record_id=f"r{i}", table_id="T00001", subject="HMX", subject_type="material",
            subject_source="column_header", property_name="密度", value_text="1.91", value_num=1.91,
            confidence=0.9, record_status="ready",
        ) for i in range(3)
    ]
    records.append(ConditionalFactRecord(
        record_id="bad", table_id="T00001", subject="爆发点/℃ / 爆发点/℃", subject_type="material",
        subject_source="table_entity_axis", property_name="雷汞", value_text="170", value_num=170,
        confidence=0.9, record_status="ready",
    ))
    results = [_result(records)]
    entries, resolutions, registry = build_subject_registry(results)
    by_name = {entry.canonical_name: entry for entry in entries}
    assert by_name["HMX"].status == "confirmed"
    assert by_name["爆发点/°C"].status == "rejected"

    accepted, decisions = gate_table_records(results, registry)
    assert any(record.record_id == "r0" for record, *_ in accepted)
    bad = next(item for item in decisions if item.record_id == "bad")
    assert not bad.accepted
