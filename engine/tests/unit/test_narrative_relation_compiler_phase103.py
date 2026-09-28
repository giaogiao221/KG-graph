from __future__ import annotations

from pathlib import Path

from book_engine.document.block_segmenter import TextBlock
from book_engine.gates.text_fact_gate import gate_text_facts
from book_engine.ontology.property_alignment_engine import PropertyAlignmentEngine
from book_engine.routing.text_subject_anchor_resolver import TextSubjectAnchor
from book_engine.text.narrative_relation_compiler import compile_narrative_relations


def _block(text: str, *, role: str, title: str = "RDX") -> TextBlock:
    return TextBlock(
        block_id=f"B:{abs(hash((text, role, title)))}",
        text=text,
        line_start=1,
        line_end=1,
        heading_path=[title],
        heading_title=title,
        heading_level=2,
        role=role,
        role_confidence=0.9,
    )


def _anchor(block: TextBlock, subject: str = "RDX") -> TextSubjectAnchor:
    return TextSubjectAnchor(
        block_id=block.block_id,
        subject=subject,
        subject_type="材料",
        source="nearest_entity_heading_anchor",
        confidence=0.92,
        status="confirmed",
    )


def test_ownerless_application_uses_confirmed_local_anchor():
    block = _block("可用于高能浇注炸药和固体推进剂。", role="application_safety", title="RDX")
    records, _ = compile_narrative_relations([block], {block.block_id: _anchor(block)})
    row = next(record for record in records if record.property_name == "用途")
    assert row.subject == "RDX"
    assert "高能浇注炸药" in row.value_text
    accepted, decisions = gate_text_facts(records, {block.block_id: _anchor(block)})
    assert any(record.record_id == row.record_id for record, _ in accepted)


def test_device_part_function_and_connection_are_clause_local():
    block = _block(
        "电点火具包括桥丝、引火药和壳体。桥丝与电极连接。桥丝的作用是将电能转换为热能。",
        role="device_system",
        title="电点火具",
    )
    records, _ = compile_narrative_relations([block], {block.block_id: _anchor(block, "电点火具")})
    triples = {(r.subject, r.property_name, r.value_text) for r in records}
    assert any(s == "电点火具" and p == "组成描述" and "桥丝" in v for s, p, v in triples)
    assert any(s == "桥丝" and p == "连接关系" and "电极" in v for s, p, v in triples)
    assert any(s == "桥丝" and p == "功能" and "电能转换为热能" in v for s, p, v in triples)


def test_classification_definition_and_comparison_are_preserved():
    block = _block(
        "火炸药可分为火药、炸药和烟火药。线性燃烧是指燃面逐层推进的燃烧现象。RDX的爆速比TNT高。",
        role="theory",
        title="火炸药",
    )
    records, _ = compile_narrative_relations([block], {block.block_id: _anchor(block, "火炸药")})
    assert any(r.subject == "火炸药" and r.property_name == "分类" for r in records)
    assert any(r.subject == "线性燃烧" and r.property_name == "定义" for r in records)
    assert any(r.subject == "RDX" and r.property_name == "比较结论" for r in records)


def test_safety_clause_is_bound_to_material_anchor():
    block = _block("储存时应保持阴凉、干燥，并严禁与强氧化剂混放。", role="application_safety", title="硝化甘油")
    records, _ = compile_narrative_relations([block], {block.block_id: _anchor(block, "硝化甘油")})
    row = next(record for record in records if record.property_name == "储存要求")
    assert row.subject == "硝化甘油"
    assert "严禁" in row.value_text


def test_numbered_method_blocks_become_linked_method_steps():
    blocks = [
        _block("（1）建立体系自由能与产物焓、熵的关系。", role="method_model", title="燃烧产物平衡组成计算方法"),
        _block("（2）假定一组燃烧产物并列出自由能函数。", role="method_model", title="燃烧产物平衡组成计算方法"),
        _block("（3）求解线性方程组并检验结果。", role="method_model", title="燃烧产物平衡组成计算方法"),
    ]
    # Same heading path is the grouping key; each block still keeps a unique id.
    for i, block in enumerate(blocks, start=1):
        block.block_id = f"M{i}"
        block.line_start = i
        block.line_end = i
    anchors = {block.block_id: _anchor(block, "固体推进剂") for block in blocks}
    records, _ = compile_narrative_relations(blocks, anchors)
    steps = [record for record in records if record.relation_kind == "method_step"]
    assert len(steps) == 3
    assert steps[0].next_step_id == steps[1].step_id
    assert steps[1].previous_step_id == steps[0].step_id
    assert steps[1].next_step_id == steps[2].step_id
    accepted, _ = gate_text_facts(steps, anchors)
    assert len(accepted) == 3


def test_new_relation_properties_receive_synthetic_alignment():
    engine = PropertyAlignmentEngine.from_default_config()
    for property_name, expected_category in (
        ("因果关系", "功能/作用"),
        ("连接关系", "结构/连接"),
        ("安全要求", "安全/储运"),
        ("比较结论", "比较"),
    ):
        decision = engine.align_row(
            {
                "fact_id": f"f:{property_name}",
                "predicate_raw": property_name,
                "attribute_name": property_name,
                "来源类型": "text_narrative_causal",
                "事实类型": "属性事实",
                "证据文本": property_name,
            }
        )
        assert decision.accepted
        assert decision.attribute_category == expected_category


def test_numeric_change_with_you_is_not_part_whole():
    block = _block("ADN中的水含量由0.1%增至0.3%。", role="property", title="ADN")
    records, _ = compile_narrative_relations([block], {block.block_id: _anchor(block, "ADN")})
    assert not any(record.relation_kind == "composition" for record in records)


def test_plain_yongyu_modifier_is_not_ownerless_application():
    block = _block("用于实验的原料RDX是由工业RDX筛选得到的。", role="experiment", title="RDX")
    records, _ = compile_narrative_relations([block], {block.block_id: _anchor(block, "RDX")})
    assert not any(record.relation_kind == "application" for record in records)


def test_toc_page_line_does_not_emit_effect_relation():
    block = _block("4.10.7复合推进剂的抑制……241", role="unknown", title="复合推进剂")
    records, _ = compile_narrative_relations([block], {block.block_id: _anchor(block, "复合推进剂")})
    assert records == []


def test_numbered_method_advantages_are_not_compiled_as_steps():
    blocks = [
        _block("（1）可灵活调用rule-based方法。", role="method_model", title="知识表示方法"),
        _block("（2）无论对how query还是why query都便于解释。", role="method_model", title="知识表示方法"),
        _block("（3）适合加入各种基本操作原语。", role="method_model", title="知识表示方法"),
    ]
    for index, block in enumerate(blocks, start=1):
        block.block_id = f"ADV{index}"
        block.line_start = index
        block.line_end = index
    anchors = {block.block_id: _anchor(block, "专家系统") for block in blocks}
    records, _ = compile_narrative_relations(blocks, anchors)
    assert not any(record.relation_kind == "method_step" for record in records)


def test_collapsed_multi_item_toc_block_emits_no_relations():
    block = _block(
        "6.1复合火药的危险性（125） 6.2几种常用的感度实验方法（129） 6.3生产技安措施（135）",
        role="application_safety",
        title="复合火药",
    )
    records, _ = compile_narrative_relations([block], {block.block_id: _anchor(block, "复合火药")})
    assert records == []


def test_source_intro_and_surface_use_prefix_are_removed_from_owner():
    blocks = [
        _block("最近资料还介绍一类癸硼烷聚合物作为高能燃料粘合剂。", role="material_profile", title="高能燃料粘合剂"),
        _block("内表面用EPDM橡胶作为绝热层。", role="material_profile", title="发动机壳体"),
    ]
    for index, block in enumerate(blocks, start=1):
        block.block_id = f"OWN{index}"
    anchors = {block.block_id: _anchor(block, block.heading_title) for block in blocks}
    records, _ = compile_narrative_relations(blocks, anchors)
    assert any(r.subject == "癸硼烷聚合物" and r.value_text == "高能燃料粘合剂" for r in records)
    assert any(r.subject == "EPDM橡胶" and r.value_text == "绝热层" for r in records)
    assert not any(r.subject.startswith("最近资料") or r.subject.startswith("内表面用") for r in records)


def test_generic_locative_property_phrase_is_not_promoted_as_effect_owner():
    block = _block("燃烧物中固体物质增加9%。", role="comparison", title="铝粉含量影响")
    records, _ = compile_narrative_relations([block], {block.block_id: _anchor(block, "铝粉")})
    assert not any(r.relation_kind == "effect" for r in records)


def test_explicit_effect_strips_addition_action_from_material_owner():
    block = _block("加入铝粉可以提高复合火药的燃烧速度。", role="comparison", title="铝粉")
    records, _ = compile_narrative_relations([block], {block.block_id: _anchor(block, "铝粉")})
    row = next(r for r in records if r.relation_kind == "effect")
    assert row.subject == "铝粉"


def test_ownerless_device_function_requires_deictic_subject_prefix():
    block = _block("完成战斗任务的部分称为战斗部。", role="device_system", title="导弹火工品")
    records, _ = compile_narrative_relations([block], {block.block_id: _anchor(block, "导弹火工品")})
    assert not any(r.relation_kind == "function" and r.subject == "导弹火工品" for r in records)


def test_as_role_with_enclosing_system_splits_role_and_application_target():
    block = _block(
        "HMX和含能四唑化合物作为氧化剂的端羟基聚丁二烯复合气体发生剂。",
        role="formulation",
        title="复合气体发生剂",
    )
    records, _ = compile_narrative_relations([block], {block.block_id: _anchor(block, "复合气体发生剂")})
    rows = [r for r in records if r.subject == "HMX和含能四唑化合物"]
    assert any(r.value_text == "氧化剂" for r in rows)
    assert any("端羟基聚丁二烯复合气体发生剂" in r.value_text for r in rows)


def test_generic_unspecified_material_is_not_promoted_as_owner():
    block = _block("一种物质能减慢爆炸物的燃烧速度。", role="comparison", title="燃烧抑制剂")
    records, _ = compile_narrative_relations([block], {block.block_id: _anchor(block, "燃烧抑制剂")})
    assert not any(r.subject == "一种物质" for r in records)


def test_research_intro_is_removed_and_generic_residue_rejected():
    block = _block("实验证实一些化学物质可抑制燃烧。", role="comparison", title="燃烧抑制剂")
    records, _ = compile_narrative_relations([block], {block.block_id: _anchor(block, "燃烧抑制剂")})
    assert not any(r.relation_kind == "effect" for r in records)


def test_as_structural_material_is_application_not_classification():
    block = _block("某些聚合物可作为构成火箭的结构材料。", role="application_safety", title="聚合物")
    records, _ = compile_narrative_relations([block], {block.block_id: _anchor(block, "聚合物")})
    assert any(r.relation_kind == "application" and r.value_text == "构成火箭的结构材料" for r in records)
    assert not any(r.relation_kind == "classification" for r in records)
