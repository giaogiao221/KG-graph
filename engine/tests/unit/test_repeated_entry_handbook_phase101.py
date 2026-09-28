from __future__ import annotations

import csv
from pathlib import Path

from book_engine.document.markdown_loader import MarkdownDocument
from book_engine.handbook.adapter import build_repeated_entry_text_layer
from book_engine.handbook.entry_parser import parse_entries
from book_engine.handbook.profile_detector import detect_document_profile


def _handbook_text(count: int = 35) -> str:
    chunks = []
    for idx in range(1, count + 1):
        code = f"{100000 + idx:06d}"
        chunks.append(
            f"{code}材料{idx} material {idx}\n"
            f"化学式：C{idx}H{idx + 1}\n"
            f"相对分子质量：{10 + idx}\n"
            f"CAS号：100-{idx:02d}-0\n"
            f"[理化性质] 熔点为{20 + idx}℃。\n"
        )
    return "\n".join(chunks)


def test_profile_detects_repeated_entry_handbook():
    text = _handbook_text()
    doc = MarkdownDocument(path=Path("synthetic.md"), text=text, lines=text.splitlines())
    decision = detect_document_profile(doc)
    assert decision.profile == "repeated_entry_handbook"
    assert decision.entry_count == 35
    assert decision.unique_entry_codes == 35


def test_profile_keeps_narrative_document_on_monograph_route():
    text = "# 推进剂\n\n该推进剂由氧化剂和粘合剂组成。\n\n## 性能\n其密度为1.7 g/cm3。"
    doc = MarkdownDocument(path=Path("narrative.md"), text=text, lines=text.splitlines())
    decision = detect_document_profile(doc)
    assert decision.profile == "narrative_monograph"


def test_parser_splits_inline_glued_entry_boundary():
    text = (
        "100001银 silver\n化学式：Ag\n[用途]用于导电。"
        "100002溴化银 silver bromide\n化学式：AgBr\n"
    )
    entries = parse_entries(text)
    assert [entry.entry_id for entry in entries] == ["100001", "100002"]
    assert "100002" not in entries[0].block_text
    assert entries[1].canonical_subject_name == "溴化银"


def test_parser_supports_locant_title_immediately_after_code():
    text = (
        "100001银 silver\n化学式：Ag\n"
        "2000091-硝基-2-氨基乙烷 1-nitro-2-aminoethane\n化学式：C2H6N2O2\n"
    )
    entries = parse_entries(text)
    assert [entry.entry_id for entry in entries] == ["100001", "200009"]
    assert entries[1].canonical_subject_name.startswith("1-硝基-2-氨基乙烷")


def test_parser_ignores_front_index_code_plus_page():
    text = "100001 2\n100002 3\n\n100001银 silver\n化学式：Ag\n100002溴化银 silver bromide\n化学式：AgBr\n"
    entries = parse_entries(text)
    assert [entry.entry_id for entry in entries] == ["100001", "100002"]


def test_parser_ignores_six_digit_number_inside_property_text():
    text = (
        "100001银 silver\n化学式：Ag\n"
        "[理化性质] 压力达到123456 Pa时发生变化。\n"
        "100002溴化银 silver bromide\n化学式：AgBr\n"
    )
    entries = parse_entries(text)
    assert [entry.entry_id for entry in entries] == ["100001", "100002"]
    assert "123456 Pa" in entries[0].block_text


def test_adapter_records_are_hard_scoped_to_entry(tmp_path: Path):
    text = _handbook_text(35)
    doc = MarkdownDocument(path=Path("synthetic.md"), text=text, lines=text.splitlines())
    blocks, anchors, anchor_by_block, records, report = build_repeated_entry_text_layer(
        doc,
        output_dir=tmp_path,
        book_id="doc:test",
        book_title="synthetic",
    )
    assert report["entries_detected"] == 35
    assert len(blocks) == len(anchors) == len(anchor_by_block) == 35
    assert records
    block_by_id = {block.block_id: block for block in blocks}
    for record in records:
        block = block_by_id[record.block_id]
        assert record.line_start == block.line_start
        assert record.line_end == block.line_end
        assert record.source_type.startswith("handbook_entry_")
        assert record.owner_source == "handbook_entry_scope"
        assert block.heading_path[-1].startswith(block.block_id.split(":")[1])
    registry = tmp_path / "step_handbook_entries" / "handbook_entry_registry.tsv"
    assert registry.exists()
    with registry.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    assert len(rows) == 35


def test_duplicate_entry_code_is_candidate_not_silent_merge(tmp_path: Path):
    text = (
        "100001银 silver\n化学式：Ag\n"
        "100002溴化银 silver bromide\n化学式：AgBr\n"
        "100002氯化银 silver chloride\n化学式：AgCl\n"
    )
    doc = MarkdownDocument(path=Path("duplicate.md"), text=text, lines=text.splitlines())
    _, anchors, _, records, report = build_repeated_entry_text_layer(
        doc,
        output_dir=tmp_path,
        book_id="doc:test",
        book_title="duplicate",
    )
    assert report["duplicate_entry_codes"] == 1
    duplicated_anchors = [anchor for anchor in anchors if anchor.block_id.startswith("HB:100002:")]
    assert duplicated_anchors and all(anchor.status == "candidate" for anchor in duplicated_anchors)
    duplicated_records = [record for record in records if record.block_id.startswith("HB:100002:")]
    assert duplicated_records and all(record.record_status == "candidate" for record in duplicated_records)


def test_handbook_adapter_preserves_decimal_visible_in_value(tmp_path: Path):
    text = "100001示例材料 example\n沸点：323.4G\n化学式：C2H4\n相对分子质量：28.05\nCAS号：1-2-3\n"
    doc = MarkdownDocument(path=Path("decimal.md"), text=text, lines=text.splitlines())
    _, _, _, records, _ = build_repeated_entry_text_layer(
        doc,
        output_dir=tmp_path,
        book_id="doc:test",
        book_title="decimal",
    )
    boiling = [record for record in records if record.property_name == "沸点"]
    assert boiling
    assert boiling[0].value_num == 323.4


def test_handbook_identity_fields_never_receive_numeric_projection(tmp_path: Path):
    text = "100001示例材料 example\n化学式：C2H4\nCAS号：7785-23-1\n相对分子质量：28.05\n"
    doc = MarkdownDocument(path=Path("identity.md"), text=text, lines=text.splitlines())
    _, _, _, records, _ = build_repeated_entry_text_layer(
        doc,
        output_dir=tmp_path,
        book_id="doc:test",
        book_title="identity",
    )
    identity = [record for record in records if record.property_name in {"化学式", "CAS登记号", "编号"}]
    assert identity
    assert all(record.value_num is None for record in identity)
