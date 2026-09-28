from book_engine.document.block_segmenter import TextBlock
from book_engine.text.text_condition_extractor import attach_text_conditions
from book_engine.text.text_fact_extractor import TextFactRecord


def test_pressure_condition_is_bound_to_text_measurement():
    block = TextBlock(
        block_id="B1",
        text="在压力为5 MPa条件下，该推进剂的燃速为8.1 mm/s。",
        line_start=10,
        line_end=10,
        heading_path=["燃烧性能"],
        heading_title="燃烧性能",
    )
    record = TextFactRecord(
        record_id="R1",
        block_id="B1",
        subject="推进剂A",
        subject_type="配方/材料体系",
        property_name="燃速",
        value_text="8.1 mm/s",
        unit="mm/s",
        value_num=8.1,
        evidence=block.text,
        confidence=0.9,
        record_status="ready",
    )
    records, audits = attach_text_conditions([record], [block])
    assert len(records[0].conditions) == 1
    assert records[0].conditions[0].normalized_name == "压力"
    assert records[0].conditions[0].value_num == 5.0
    assert records[0].conditions[0].unit == "MPa"
    assert audits


def test_target_property_is_not_reused_as_condition():
    block = TextBlock(
        block_id="B2",
        text="该材料的密度为1.80 g/cm3。",
        line_start=20,
        line_end=20,
        heading_path=["物理性质"],
        heading_title="物理性质",
    )
    record = TextFactRecord(
        record_id="R2",
        block_id="B2",
        subject="RDX",
        subject_type="材料",
        property_name="密度",
        value_text="1.80 g/cm3",
        unit="g/cm3",
        value_num=1.8,
        evidence=block.text,
        confidence=0.9,
        record_status="ready",
    )
    records, _ = attach_text_conditions([record], [block])
    assert records[0].conditions == []


def test_pressure_can_be_inferred_from_unit_with_condition_cue():
    block = TextBlock(
        block_id="B3",
        text="在5 MPa下，该推进剂燃速为8.1 mm/s。",
        line_start=30,
        line_end=30,
        heading_path=["燃烧性能"],
        heading_title="燃烧性能",
    )
    record = TextFactRecord(
        record_id="R3",
        block_id="B3",
        subject="推进剂A",
        subject_type="配方/材料体系",
        property_name="燃速",
        value_text="8.1 mm/s",
        unit="mm/s",
        value_num=8.1,
        evidence=block.text,
        confidence=0.9,
        record_status="ready",
    )
    records, audits = attach_text_conditions([record], [block])
    assert len(records[0].conditions) == 1
    assert records[0].conditions[0].normalized_name == "压力"
    assert records[0].conditions[0].value_num == 5.0
    assert audits
