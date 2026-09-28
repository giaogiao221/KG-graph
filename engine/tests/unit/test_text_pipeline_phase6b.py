from pathlib import Path

from book_engine.document.markdown_loader import MarkdownDocument
from book_engine.document.heading_rebuilder import rebuild_headings
from book_engine.document.block_segmenter import segment_text_blocks
from book_engine.routing.block_role_classifier import classify_blocks
from book_engine.routing.text_subject_anchor_resolver import resolve_text_subject_anchors
from book_engine.text.text_fact_extractor import extract_text_facts
from book_engine.gates.text_fact_gate import gate_text_facts


def _doc(text: str) -> MarkdownDocument:
    return MarkdownDocument(path=Path("sample.md"), text=text, lines=text.splitlines())


def test_material_heading_and_property_section_inheritance():
    document = _doc(
        "# 第1章 材料\n"
        "# 1.1 三苯基铋\n"
        "中文名称：三苯基铋\n"
        "分子式：C18H15Bi\n\n"
        "# 1.1.1 物理性质\n"
        "密度为1.58 g/cm3。\n"
    )
    headings = rebuild_headings(document)
    blocks = classify_blocks(segment_text_blocks(document, headings, []))
    anchors, anchor_map = resolve_text_subject_anchors(blocks, [])
    records = extract_text_facts(blocks, anchor_map)
    accepted, decisions = gate_text_facts(records, anchor_map)

    assert any(anchor.subject == "三苯基铋" and anchor.status == "confirmed" for anchor in anchors)
    assert any(record.property_name == "分子式" and record.value_text == "C18H15Bi" for record in records)
    assert any(record.property_name == "密度" and record.value_num == 1.58 for record, _ in accepted)


def test_theory_heading_does_not_become_material_subject():
    document = _doc("# 第1章 炸药一般特征\n# 1.1 爆炸基本条件\n爆炸具有高速、放热和放气特征。\n")
    headings = rebuild_headings(document)
    blocks = classify_blocks(segment_text_blocks(document, headings, []))
    anchors, anchor_map = resolve_text_subject_anchors(blocks, [])
    records = extract_text_facts(blocks, anchor_map)
    assert not records
    assert all(anchor.subject == "" for anchor in anchors)


def test_labeled_non_numeric_value_remains_text():
    document = _doc("# 1.1 RDX\n中文名称：黑索今\n用途：雷管输出装药\n")
    blocks = classify_blocks(segment_text_blocks(document, rebuild_headings(document), []))
    anchors, anchor_map = resolve_text_subject_anchors(blocks, [])
    records = extract_text_facts(blocks, anchor_map)
    usage = next(record for record in records if record.property_name == "用途")
    assert usage.value_num is None
    assert usage.value_text == "雷管输出装药"
