from __future__ import annotations

from book_engine.document.block_segmenter import TextBlock
from book_engine.quality.semantic_release_consistency import apply_semantic_release_guard
from book_engine.routing.text_subject_anchor_resolver import TextSubjectAnchor
from book_engine.text.narrative_relation_compiler import compile_narrative_relations


def _block(text: str, *, role: str = "comparison", title: str = "GN聚合") -> TextBlock:
    return TextBlock(
        block_id="B1031", text=text, line_start=1, line_end=max(1, text.count("\n") + 1),
        heading_path=[title], heading_title=title, heading_level=2, role=role, role_confidence=0.9,
    )


def _anchor(block: TextBlock, subject: str = "GN") -> TextSubjectAnchor:
    return TextSubjectAnchor(
        block_id=block.block_id, subject=subject, subject_type="材料",
        source="nearest_entity_heading_anchor", confidence=0.92, status="confirmed",
    )


def test_generic_experimental_factor_is_not_promoted_as_effect_owner():
    block = _block("单体增加的速率必须小于单体质子化的速率。")
    records, _ = compile_narrative_relations([block], {block.block_id: _anchor(block)})
    assert not any(record.subject == "单体" for record in records)


def test_plain_numbered_toc_block_without_dot_leaders_emits_no_relation():
    text = "\n".join([
        "4.10.1抑制剂的特性 233",
        "4.10.2抑制剂的测试 233",
        "4.10.3 抑制推进剂的弹道评估 233",
        "4.10.4 抑制材料 234",
        "4.10.5 抑制技术 235",
        "4.10.6 双基推进剂的抑制 238",
    ])
    block = _block(text, role="unknown", title="双基推进剂")
    records, _ = compile_narrative_relations([block], {block.block_id: _anchor(block, "双基推进剂")})
    assert records == []


def test_release_guard_quarantines_experimental_factor_subject():
    row = {
        "fact_id": "f:factor", "graph_fact_key": "g:factor", "主体名称": "单体",
        "主体类型": "材料", "attribute_name": "作用影响", "predicate_raw": "作用影响",
        "尾实体/取值文本": "增加的速率必须小于质子化速率",
        "来源类型": "text_narrative_effect", "证据文本": "单体增加的速率必须小于单体质子化的速率。",
    }
    result = apply_semantic_release_guard([row])
    assert len(result.releasable_rows) == 0
    assert "experimental_factor_subject" in result.candidate_rows[0]["_phase100_release_guard_reasons"]


def test_release_guard_quarantines_toc_page_evidence():
    row = {
        "fact_id": "f:toc", "graph_fact_key": "g:toc", "主体名称": "双基推进剂",
        "主体类型": "材料", "attribute_name": "作用影响", "predicate_raw": "作用影响",
        "尾实体/取值文本": "抑制238", "来源类型": "text_narrative_effect",
        "证据文本": "4.10.1抑制剂的特性 233\n4.10.6 双基推进剂的抑制 238",
    }
    result = apply_semantic_release_guard([row])
    assert len(result.releasable_rows) == 0
    assert "table_of_contents_or_page_index_evidence" in result.candidate_rows[0]["_phase100_release_guard_reasons"]
