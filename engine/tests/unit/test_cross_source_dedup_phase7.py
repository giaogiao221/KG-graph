from book_engine.export.cross_source_deduplicator import deduplicate_rows, semantic_key


def _row(fact_id, source, value_text, value_num="1.8", unit="g/cm3"):
    return {
        "fact_id": fact_id,
        "graph_fact_key": "g-" + fact_id,
        "主体名称": "RDX",
        "attribute_name": "密度",
        "尾实体/取值文本": value_text,
        "normalized_value_text": value_text,
        "normalized_value_num": value_num,
        "数值": value_num,
        "单位": unit,
        "normalized_unit": unit,
        "条件文本": "",
        "structured_condition_json": "[]",
        "来源类型": source,
        "置信度": "0.9",
        "证据文本": "evidence",
    }


def test_table_wins_over_equivalent_text_row():
    table = _row("T1", "table_conditional_record", "1.80")
    text = _row("X1", "text_explicit_numeric_property", "1.8 g/cm3")
    kept, audits = deduplicate_rows([text, table])
    assert len(kept) == 1
    assert kept[0]["fact_id"] == "T1"
    assert len(audits) == 1


def test_different_conditions_are_not_merged():
    a = _row("A", "table_conditional_record", "1.8")
    b = _row("B", "table_conditional_record", "1.8")
    b["条件文本"] = "温度=20℃"
    b["structured_condition_json"] = '[{"normalized_name":"温度","value_num":20,"unit":"℃"}]'
    kept, _ = deduplicate_rows([a, b])
    assert len(kept) == 2
    assert semantic_key(a) != semantic_key(b)
