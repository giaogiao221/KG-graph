from book_engine.export.schema59_columns import SCHEMA59_COLUMNS


def test_legacy_schema_is_still_exactly_59_columns():
    assert len(SCHEMA59_COLUMNS) == 59
    assert SCHEMA59_COLUMNS[0] == "fact_id"
    assert SCHEMA59_COLUMNS[-1] == "证据文本"
