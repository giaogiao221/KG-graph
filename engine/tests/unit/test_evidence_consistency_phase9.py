from book_engine.quality.evidence_consistency import assess_row


def base_row():
    return {
        "fact_id": "f1",
        "graph_fact_key": "g1",
        "主体名称": "RDX",
        "attribute_name": "密度",
        "predicate_raw": "密度",
        "attribute_category": "物理属性",
        "尾实体/取值文本": "1.80 g/cm3",
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
        "证据文本": "RDX | 密度/(g/cm3) | 1.80",
        "置信度": "0.82",
        "_property_alignment_status": "aligned_exact",
    }


def test_grounded_fact_scores_high():
    result = assess_row(base_row())
    assert not result.hard_reject_reasons
    assert result.score >= 0.8


def test_grade_only_subject_is_hard_reject():
    row = base_row()
    row["主体名称"] = "优等品"
    result = assess_row(row)
    assert "grade_only_subject" in result.hard_reject_reasons


def test_glued_numeric_is_hard_reject():
    row = base_row()
    row["尾实体/取值文本"] = "1.651.811.96"
    row["证据文本"] = "密度 1.651.811.96"
    result = assess_row(row)
    assert "suspected_glued_numeric_value" in result.hard_reject_reasons


def test_text_visibility_policy_is_checked():
    row = base_row()
    row["尾实体/取值文本"] = "白色粉末"
    row["数值"] = ""
    row["normalized_value_num"] = ""
    row["value_hidden"] = "是"
    row["证据文本"] = "RDX 外观 白色粉末"
    result = assess_row(row)
    assert "text_visibility_policy_mismatch" in result.hard_reject_reasons
