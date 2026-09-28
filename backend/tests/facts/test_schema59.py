from app.extraction.native.common import SCHEMA59_COLUMNS as NATIVE_COLUMNS
from app.facts.schema59 import SCHEMA59_COLUMNS
from app.facts.script_schema59 import SCRIPT_SCHEMA59_COLUMNS, platform_to_script_row, script_to_platform_values


def test_schema59_has_exact_utf8_order_and_single_native_contract() -> None:
    assert len(SCHEMA59_COLUMNS) == 59
    assert len(set(SCHEMA59_COLUMNS)) == 59
    assert SCHEMA59_COLUMNS[:4] == ("fact_id", "graph_fact_key", "文档ID", "书名")
    assert SCHEMA59_COLUMNS[-2:] == ("抽取来源", "证据文本")
    assert NATIVE_COLUMNS is SCHEMA59_COLUMNS


def test_script_schema59_matches_the_engine_contract_and_round_trips_business_fields() -> None:
    assert len(SCRIPT_SCHEMA59_COLUMNS) == 59
    assert SCRIPT_SCHEMA59_COLUMNS[:10] == (
        "fact_id", "graph_fact_key", "文档ID", "书名", "章节路径", "来源定位",
        "来源类型", "所属表格ID", "所属表格标题", "主体名称",
    )
    assert SCRIPT_SCHEMA59_COLUMNS[-3:] == ("置信度", "抽取来源", "证据文本")
    internal = {column: "" for column in SCHEMA59_COLUMNS}
    internal.update({"主体": "样品", "属性": "密度", "数值": "1.2", "单位": "g/cm3", "条件": "20 C", "证据文本": "样品密度为 1.2 g/cm3。"})
    script = platform_to_script_row(internal)
    restored = script_to_platform_values(script)
    assert (script["主体名称"], script["attribute_name"], script["数值"]) == ("样品", "密度", "1.2")
    assert (restored["主体"], restored["属性"], restored["数值"]) == ("样品", "密度", "1.2")
