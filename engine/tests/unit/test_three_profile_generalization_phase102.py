from __future__ import annotations

from pathlib import Path

from book_engine.core.schemas import TableBlock
from book_engine.document.markdown_loader import MarkdownDocument
from book_engine.handbook.entry_parser import ChemEntry
from book_engine.handbook.extractor import extract_facts_from_entry
from book_engine.handbook.profile_detector import detect_document_profile
from book_engine.quality.semantic_release_consistency import apply_semantic_release_guard
from book_engine.specification.adapter import build_specification_table_layer
from book_engine.specification.parser import SpecificationEntry, parse_specification_entries
from book_engine.tables.grid_rebuilder import rebuild_html_grid


def _specification_text(count: int = 20) -> str:
    parts: list[str] = []
    for index in range(1, count + 1):
        parts.append(
            f"# 1.{index} 材料{index}\n"
            f"中文名称：材料{index}\n英文名称：Material {index}\n化学式：C{index}H{index + 2}\n"
            "## 3.理化指标和检验方法\n理化指标和检验方法\n"
        )
    return "\n".join(parts)


def test_profile_detects_specification_handbook():
    text = _specification_text()
    document = MarkdownDocument(path=Path("spec.md"), text=text, lines=text.splitlines())
    decision = detect_document_profile(document)
    assert decision.profile == "specification_handbook"
    assert decision.specification_entry_heading_hits == 20
    assert decision.specification_table_hits >= 20


def test_specification_parser_accepts_math_wrapped_numbered_heading():
    text = (
        "# $1 . 5$ 高氯酸铵\n"
        "中文名称：高氯酸铵\n英文名称：Ammonium perchlorate\n化学式：NH4ClO4\n"
        "## 1.物理性质\n白色晶体。\n"
    )
    document = MarkdownDocument(path=Path("spec.md"), text=text, lines=text.splitlines())
    entries = parse_specification_entries(document)
    assert len(entries) == 1
    assert entries[0].section_id == "1.5"
    assert entries[0].canonical_subject == "高氯酸铵"


def test_specification_matrix_uses_material_subject_and_preserves_grade_and_method(tmp_path: Path):
    raw = (
        "<table><tr><td rowspan=2>项目</td><td colspan=2>理化指标</td><td rowspan=2>检验方法</td></tr>"
        "<tr><td>99.90</td><td>99.00</td></tr>"
        "<tr><td>外观</td><td colspan=2>白色粉末</td><td>目视法</td></tr>"
        "<tr><td>As/%</td><td>≤0.0040</td><td>二</td><td>分光光度法</td></tr></table>"
    )
    block = TableBlock(
        table_id="T-SPEC", source_type="html", raw_text=raw, line_start=10, line_end=10,
        heading="3.理化指标和检验方法",
    )
    grid = rebuild_html_grid(block)
    entry = SpecificationEntry(
        section_id="1.1", canonical_subject="三氧化二锑", english_name_raw="Antimony trioxide",
        title_raw="三氧化二锑", start_line=1, end_line=20, block_text="",
    )
    accepted, decisions, report = build_specification_table_layer(
        [(block, grid, object())], [entry], output_dir=tmp_path,
    )
    assert report["tables_matched"] == 1
    assert len(decisions) == 4  # appearance projected across two grade columns + two As cells
    arsenic = [item for item in accepted if item[0].property_name == "砷含量"]
    assert len(arsenic) == 1
    record = arsenic[0][0]
    assert record.subject == "三氧化二锑"
    assert record.method == "分光光度法"
    assert record.conditions[0].normalized_name == "规格等级"
    assert record.conditions[0].value_text == "99.90"
    held = [decision for decision in decisions if not decision.accepted]
    assert any("specification_value_not_safely_projectable" in decision.reasons for decision in held)


def test_final_guard_blocks_header_unit_role_and_formula_fragment_subjects():
    subjects = ["外观", "um", "以开始", "CH2", "lnr"]
    rows = []
    for index, subject in enumerate(subjects):
        rows.append({
            "fact_id": f"f{index}", "graph_fact_key": f"g{index}",
            "来源类型": "text_clause_bound_numeric_property", "主体名称": subject,
            "attribute_name": "粒度", "predicate_raw": "粒度", "尾实体/取值文本": "0.2 μm",
            "normalized_value_num": "0.2", "单位": "μm", "normalized_unit": "μm",
            "证据文本": "RDX产品粒度为0.2 μm，结晶由此开始。",
        })
    result = apply_semantic_release_guard(rows)
    assert not result.releasable_rows
    assert len(result.candidate_rows) == len(subjects)


def test_repeated_handbook_multivalue_measurements_preserve_state_and_source_conditions():
    entry = ChemEntry(
        entry_id="100001", canonical_subject_name="示例材料", english_name="Example",
        entry_type="材料", start_line=1, end_line=4, title_raw="示例材料 Example",
        title_normalized="示例材料 Example",
        block_lines=[
            "100001示例材料 Example",
            "标准生成热：(s) -23.97 kJ/mol [2]；(l) -18.50 kJ/mol [3]",
            "化学式：C2H4",
        ],
    )
    facts = extract_facts_from_entry(entry, book_id="doc:test", book_title="test")
    generated = [fact for fact in facts if fact.get("关系/属性名称") == "标准生成热"]
    assert len(generated) == 2
    assert {fact.get("条件文本") for fact in generated} == {
        "物态=固态；来源=[2]", "物态=液态；来源=[3]",
    }
