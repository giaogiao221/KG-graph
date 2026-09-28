from pathlib import Path

from book_engine.quality.production_release_gate import apply_production_release_gate


def row(fact_id: str, subject: str = "RDX", value: str = "1.80 g/cm3", confidence: str = "0.82"):
    return {
        "fact_id": fact_id,
        "graph_fact_key": "g" + fact_id,
        "主体名称": subject,
        "主体类型": "单质炸药",
        "attribute_name": "密度",
        "predicate_raw": "密度",
        "attribute_category": "物理属性",
        "尾实体/取值文本": value,
        "normalized_value_num": "1.80",
        "数值": "1.80",
        "单位": "g/cm3",
        "normalized_unit": "g/cm3",
        "value_hidden": "是",
        "条件文本": "",
        "方法名称": "",
        "来源类型": "table_conditional_record",
        "章节路径": "RDX理化性能",
        "所属表格标题": "RDX密度",
        "证据文本": f"RDX | 密度 | {value}",
        "置信度": confidence,
        "_property_alignment_status": "aligned_exact",
    }


def test_release_and_reject_without_llm(monkeypatch):
    monkeypatch.delenv("KGCHOUQU_PRODUCTION_LLM_ENABLED", raising=False)
    result = apply_production_release_gate([row("1"), row("2", subject="优等品")])
    assert len(result.released_rows) == 1
    assert len(result.rejected_rows) == 1
    assert result.decisions[0].action == "release"
    assert result.decisions[1].action == "reject"


def test_unmapped_property_fails_closed_to_candidate(monkeypatch):
    monkeypatch.delenv("KGCHOUQU_PRODUCTION_LLM_ENABLED", raising=False)
    item = row("1", confidence="0.90")
    item["attribute_category"] = "其他属性"
    item["_property_alignment_status"] = "unmapped"
    result = apply_production_release_gate([item])
    assert len(result.released_rows) == 0
    assert len(result.candidate_rows) == 1
    assert result.decisions[0].action == "candidate"
