from book_engine.core.schemas import ConditionalFactRecord
from book_engine.tables.record_refiner import refine_record


def _record(**kwargs):
    data = dict(
        record_id="r1", table_id="T1", row_index=1, column_index=1,
        subject="HMX", subject_type="材料", subject_source="row_header",
        property_name="爆速", property_role="property", value_text="9000",
        normalized_value_text="9000", value_num=9000.0, unit="m/s",
        confidence=0.92, record_status="ready",
    )
    data.update(kwargs)
    return ConditionalFactRecord(**data)


def test_property_unit_is_separated_and_path_collapsed():
    records, events, unresolved = refine_record(_record(property_name="性能 / 爆速/(m/s)", unit=""))
    assert len(records) == 1
    assert records[0].property_name == "爆速"
    assert records[0].unit == "m/s"
    assert not unresolved


def test_clear_axis_inversion_is_repaired():
    records, events, unresolved = refine_record(_record(
        subject="密度/(g/cm3)", property_name="HMX", value_text="1.91", unit="",
        value_num=1.91,
    ))
    item = records[0]
    assert item.subject == "HMX"
    assert item.property_name == "密度"
    assert item.unit == "g/cm3"
    assert item.subject_source == "axis_inversion_repair"


def test_explicit_multi_subject_enumeration_is_split_but_slash_system_is_not():
    records, _, _ = refine_record(_record(subject="RDX、HMX"))
    assert [item.subject for item in records] == ["RDX", "HMX"]
    records2, _, _ = refine_record(_record(subject="NG/BTTN/PEG"))
    assert len(records2) == 1
    assert records2[0].subject == "NG/BTTN/PEG"


def test_concatenated_numeric_series_is_held():
    records, _, unresolved = refine_record(_record(value_text="1.651.811.96", unit="g/cm3"))
    assert records[0].record_status == "unresolved"
    assert "suspected_concatenated_numeric_series" in records[0].unresolved_reasons
    assert unresolved


def test_subject_property_collision_recovers_from_header_path():
    records, _, unresolved = refine_record(_record(
        subject="TPB", property_name="TPB", value_text=">=97.5", unit="",
        row_header_path=["纯度/%"], column_header_path=["TPB"],
    ))
    assert records[0].property_name == "纯度"
    assert records[0].unit == "%"
    assert not unresolved


def test_subject_named_percentage_becomes_content_attribute():
    records, _, unresolved = refine_record(_record(
        subject="TPB", property_name="TPB", value_text=">=97.5", unit="%",
        row_header_path=["TPB/%"], column_header_path=["理化指标"],
    ))
    assert records[0].property_name == "含量"
    assert records[0].unit == "%"
    assert not unresolved


def test_grade_header_is_rebound_to_context_material():
    from book_engine.tables.context_metadata_extractor import ContextMetadata

    metadata = ContextMetadata(subject="过氧化二苯甲酰", subject_source="heading", subject_confidence=0.8)
    records, _, unresolved = refine_record(_record(
        subject="一级品", property_name="熔点/C", value_text="102~106", unit="",
        column_header_path=["理化指标", "一级品"],
    ), metadata=metadata)
    item = records[0]
    assert item.subject == "过氧化二苯甲酰"
    assert item.sample_id == "一级品"
    assert item.property_name == "熔点"
    assert item.unit == "℃"
    assert any(condition.normalized_name == "样品等级" for condition in item.conditions)
    assert not unresolved


def test_latex_table_caption_subject_is_recovered():
    from book_engine.core.schemas import TableBlock
    from book_engine.tables.context_metadata_extractor import extract_context_metadata

    block = TableBlock(
        table_id="T9", source_type="html", raw_text="", line_start=1, line_end=1,
        heading="3.理化指标和检验方法",
        preceding_text="表2-4 $1 , 1 ^ { \\prime }$ -二乙基二茂铁理化指标和检验方法",
    )
    metadata = extract_context_metadata(block)
    assert metadata.subject == "1,1′-二乙基二茂铁"
    assert metadata.subject_confidence >= 0.85
