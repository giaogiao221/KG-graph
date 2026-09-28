from book_engine.core.schemas import ConditionAtom, ConditionalFactRecord
from book_engine.export.schema59_exporter import _condition_attributes, _condition_text


def test_semantically_duplicate_decimal_comma_conditions_are_projected_once():
    record = ConditionalFactRecord(
        subject="GAP/AN复合火药",
        subject_type="材料",
        property_name="燃速",
        value_text="2.80mm/s",
        conditions=[
            ConditionAtom(
                name="压力", normalized_name="压力", condition_type="environment",
                value_text="6,86kPa", value_num=6.86, unit="kPa", priority=400, confidence=0.78,
            ),
            ConditionAtom(
                name="压力", normalized_name="压力", condition_type="environment",
                value_text="6.86kPa", value_num=6.86, unit="kPa", priority=420, confidence=0.88,
            ),
        ],
    )
    assert _condition_text(record) == "压力=6.86kPa"
    assert _condition_attributes(record) == '{"压力": ["6.86kPa"]}'
