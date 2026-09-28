from __future__ import annotations

from book_engine.document.block_segmenter import TextBlock
from book_engine.quality.generalized_release_gate import apply_generalized_release_gate
from book_engine.routing.text_subject_anchor_resolver import TextSubjectAnchor
from book_engine.text.generalized_relation_compiler import compile_generalized_relations


def _anchor(block_id: str, subject: str = "TATB") -> TextSubjectAnchor:
    return TextSubjectAnchor(
        block_id=block_id,
        subject=subject,
        subject_type="材料",
        source="nearest_entity_heading_anchor",
        confidence=0.95,
        status="confirmed",
        reasons=[],
    )


def test_count_aligned_parallel_numeric_emits_pairwise_candidates():
    block = TextBlock(
        block_id="TB:parallel",
        text="FP-TATB、UF-TATB和PF-TATB的粒径分别为15 μm、5 μm和55 μm。",
        line_start=10,
        line_end=10,
        heading_path=["TATB", "粒度"],
        heading_title="粒度",
        role="property",
    )
    rows, audits = compile_generalized_relations([block], {block.block_id: _anchor(block.block_id)})
    pairs = {(row.subject, row.property_name, row.value_text) for row in rows}
    assert ("FP-TATB", "粒径", "15 μm") in pairs
    assert ("UF-TATB", "粒径", "5 μm") in pairs
    assert ("PF-TATB", "粒径", "55 μm") in pairs
    assert all(row.record_status == "candidate" for row in rows)
    assert sum(item.rule == "count_aligned_parallel_numeric" for item in audits) == 3


def test_generalized_gate_softens_registry_and_confidence_reasons():
    row = {
        "fact_id": "fact:soft",
        "graph_fact_key": "gfk:soft",
        "主体名称": "RDX",
        "主体类型": "材料",
        "attribute_name": "密度",
        "predicate_raw": "密度",
        "尾实体/取值文本": "1.80 g/cm3",
        "证据文本": "RDX的密度为1.80 g/cm3。",
        "来源类型": "text_clause_bound_numeric_property",
        "置信度": "0.68",
        "_gate_reasons": "record_status_candidate;confidence_below_0_70",
        "_gate_accepted": "0",
        "_evidence_self_contained": "1",
        "_origin_stream": "held_text",
    }
    result = apply_generalized_release_gate([row])
    assert len(result.released_rows) == 1
    assert result.decisions[0].action == "release"


def test_generalized_gate_keeps_catastrophic_subject_error_out():
    row = {
        "fact_id": "fact:bad",
        "graph_fact_key": "gfk:bad",
        "主体名称": "μm",
        "主体类型": "其他实体",
        "attribute_name": "粒径",
        "predicate_raw": "粒径",
        "尾实体/取值文本": "5 μm",
        "证据文本": "所得产品粒径为5 μm。",
        "来源类型": "text_clause_bound_numeric_property",
        "置信度": "0.90",
        "_gate_reasons": "unit_or_variable_subject",
        "_gate_accepted": "0",
        "_evidence_self_contained": "1",
        "_origin_stream": "held_text",
    }
    result = apply_generalized_release_gate([row])
    assert not result.released_rows
    assert result.decisions[0].action == "reject"
    assert result.decisions[0].hard_reasons


def test_hanneng_word_does_not_trigger_false_contains_relation():
    block = TextBlock(
        block_id="TB:hanneng-word",
        text="新的含能材料为提高推进剂的能量水平提供了前提。",
        line_start=20,
        line_end=20,
        heading_path=["含能材料"],
        heading_title="含能材料",
        role="material_profile",
    )
    rows, _ = compile_generalized_relations([block], {block.block_id: _anchor(block.block_id, "含能材料")})
    assert not any(row.source_type == "text_generalized_composition" for row in rows)


def test_explicit_contains_and_trait_still_emit_bounded_relations():
    blocks = [
        TextBlock(
            block_id="TB:contains",
            text="ADN含有少量硝酸铵杂质。",
            line_start=30,
            line_end=30,
            heading_path=["ADN"],
            heading_title="ADN",
            role="material_profile",
        ),
        TextBlock(
            block_id="TB:trait",
            text="物理方法的特征是没有化学转变参与。",
            line_start=31,
            line_end=31,
            heading_path=["物理方法"],
            heading_title="物理方法",
            role="method_model",
        ),
    ]
    anchors = {
        blocks[0].block_id: _anchor(blocks[0].block_id, "ADN"),
        blocks[1].block_id: _anchor(blocks[1].block_id, "物理方法"),
    }
    rows, _ = compile_generalized_relations(blocks, anchors)
    assert any(row.subject == "ADN" and row.property_name == "组成描述" for row in rows)
    assert any(row.subject == "物理方法" and row.property_name == "特点" for row in rows)
