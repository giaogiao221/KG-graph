from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from book_engine.llm_v106.markdown_units import parse_markdown_units
from book_engine.llm_v106.prompts import direct_extraction_payload
from book_engine.llm_v106.schema59 import (
    PropertyOntologyIndex,
    SCHEMA59_COLUMNS,
    apply_llm_fact,
    deduplicate,
    read_tsv,
    span_supported,
    subject_is_valid,
    validate_rows,
    write_tsv,
)


def test_subject_fragments_are_rejected() -> None:
    for value in ("只", "法", "凝聚炸药的DDT不仅", "燃速不", "目前已有各种实验手段证明上述机理图"):
        ok, _ = subject_is_valid(value)
        assert not ok
    assert subject_is_valid("凝聚炸药的DDT")[0]
    assert subject_is_valid("圆筒试验")[0]


def test_span_support_tolerates_whitespace() -> None:
    evidence = "CL-20 的密度可达 2.04 g·cm⁻³。"
    assert span_supported("CL-20 的密度", evidence)
    assert span_supported("2.04 g·cm⁻³", evidence)
    assert not span_supported("RDX", evidence)


def test_markdown_parser_builds_text_and_table_units(tmp_path: Path) -> None:
    source = tmp_path / "测试书.md"
    source.write_text(
        "# 第一章\n\nTNT 的密度为 1.65 g/cm3，爆速为 6900 m/s。\n\n"
        "<table><tr><td>材料</td><td>密度</td></tr><tr><td>RDX</td><td>1.81</td></tr></table>\n",
        encoding="utf-8",
    )
    windows, tables = parse_markdown_units(source, min_paragraph_chars=10)
    assert windows and "TNT" in windows[0].text
    assert tables and "RDX" in tables[0].text
    assert windows[0].line_start > 0


def test_single_line_html_tables_are_not_merged_with_the_next_table(tmp_path: Path) -> None:
    source = tmp_path / "火工品技术.md"
    source.write_text(
        "# 感度影响因素\n\n"
        "表7-1桥丝直径与发火电压的关系\n\n"
        "<table><tr><td>直径</td><td>电压</td></tr><tr><td>5</td><td>10</td></tr></table>\n\n"
        "两张表之间的正文不应进入任一表格证据。\n\n"
        "表7-2桥丝长度与发火电压的关系\n\n"
        "<table><tr><td>长度</td><td>电压</td></tr><tr><td>0.35</td><td>365</td></tr></table>\n",
        encoding="utf-8",
    )

    _, tables = parse_markdown_units(source, min_paragraph_chars=10)

    assert len(tables) == 2
    assert tables[0].line_start == tables[0].line_end == 5
    assert tables[1].line_start == tables[1].line_end == 11
    assert "0.35" not in tables[0].text
    assert "5</td><td>10" not in tables[1].text


def test_table_units_have_stable_ids_and_explicit_titles(tmp_path: Path) -> None:
    source = tmp_path / "火工品技术.md"
    source.write_text(
        "# 感度影响因素\n\n"
        "表7-1桥丝直径与发火电压的关系\n\n"
        "<table><tr><td>直径</td></tr><tr><td>5</td></tr></table>\n\n"
        "表7-2桥丝长度与发火电压的关系\n\n"
        "<table><tr><td>长度</td></tr><tr><td>0.35</td></tr></table>\n",
        encoding="utf-8",
    )

    _, tables = parse_markdown_units(source)

    assert [table.table_id for table in tables] == ["T00001", "T00002"]
    assert [table.table_title for table in tables] == [
        "表7-1桥丝直径与发火电压的关系",
        "表7-2桥丝长度与发火电压的关系",
    ]


def test_table_without_caption_keeps_context_out_of_title(tmp_path: Path) -> None:
    source = tmp_path / "测试书.md"
    source.write_text(
        "# 复合推进剂\n\n"
        "<table><tr><td>材料</td></tr><tr><td>AP</td></tr></table>\n",
        encoding="utf-8",
    )

    _, tables = parse_markdown_units(source)

    assert tables[0].table_id == "T00001"
    assert tables[0].table_title == ""
    assert tables[0].table_title_source == "context_only"
    assert tables[0].table_context == "复合推进剂"
    assert tables[0].caption_line_start == 0


def test_table_with_explicit_caption_has_source_caption_provenance(tmp_path: Path) -> None:
    source = tmp_path / "测试书.md"
    source.write_text(
        "# 第一章\n\n表 2-1 配方组成\n"
        "| 成分 | 含量 |\n| --- | --- |\n| AP | 70 |\n\n"
        "Table 3 Thermal Properties\n"
        "| Item | Value |\n| --- | --- |\n| RDX | 1.8 |\n",
        encoding="utf-8",
    )

    _, tables = parse_markdown_units(source)

    assert [(table.table_title, table.table_title_source, table.caption_line_start) for table in tables] == [
        ("表 2-1 配方组成", "source_caption", 3),
        ("Table 3 Thermal Properties", "source_caption", 8),
    ]


def test_direct_table_prompt_receives_table_identity_and_title() -> None:
    payload = direct_extraction_payload(
        unit_kind="table",
        unit_id="u:test",
        table_id="T00001",
        table_title="表7-1桥丝直径与发火电压的关系",
        book_title="火工品技术",
        heading_path="感度影响因素",
        source_locator="L5-L5",
        evidence="<table><tr><td>5μm</td><td>21.4Ω</td></tr></table>",
        property_names=[],
        max_facts=80,
    )

    assert payload["table_id"] == "T00001"
    assert payload["table_title"] == "表7-1桥丝直径与发火电压的关系"


def test_direct_text_prompt_does_not_change_for_empty_table_metadata() -> None:
    payload = direct_extraction_payload(
        unit_kind="text_window",
        unit_id="u:text",
        book_title="火工品技术",
        heading_path="感度影响因素",
        source_locator="L1-L4",
        evidence="桥丝直径影响产品感度。",
        property_names=[],
        max_facts=24,
    )

    assert "table_id" not in payload
    assert "table_title" not in payload


def test_apply_llm_fact_builds_exact_59_columns() -> None:
    ontology = PropertyOntologyIndex([
        {"alias": "密度", "canonical_name": "密度", "attribute_category": "物理属性", "property_id": "prop:d"}
    ])
    evidence = "CL-20 的密度可达 2.04 g·cm⁻³。"
    row, reasons = apply_llm_fact(
        fact={
            "subject": "CL-20",
            "subject_type": "材料",
            "property": "密度",
            "relation_type": "属性",
            "value": "2.04",
            "unit": "g·cm⁻³",
            "condition": "",
            "polarity": "肯定",
            "subject_span": "CL-20",
            "value_span": "2.04",
            "confidence": 0.91,
        },
        ontology=ontology,
        evidence=evidence,
        book_title="含能材料",
        document_id="doc:test",
        heading_path="第1章",
        source_locator="L1-L1",
        source_type="llm_text_window_direct",
    )
    assert not reasons
    assert row is not None
    assert len(row) == 59
    assert row["主体名称"] == "CL-20"
    assert row["attribute_name"] == "密度"
    assert row["normalized_unit"] == "g·cm⁻³"
    assert validate_rows([row])["ok"]


def test_apply_llm_table_fact_writes_table_metadata() -> None:
    ontology = PropertyOntologyIndex([])
    evidence = "<table><tr><td>桥丝直径</td><td>5μm</td></tr></table>"

    row, reasons = apply_llm_fact(
        fact={
            "subject": "桥丝直径",
            "subject_type": "性能参数",
            "property": "取值",
            "relation_type": "属性",
            "value": "5μm",
            "unit": "μm",
            "condition": "",
            "polarity": "肯定",
            "subject_span": "桥丝直径",
            "value_span": "5μm",
            "confidence": 0.95,
        },
        ontology=ontology,
        evidence=evidence,
        book_title="火工品技术",
        document_id="doc:test",
        heading_path="感度影响因素",
        source_locator="L5-L5",
        source_type="llm_table_direct",
        table_id="T00001",
        table_title="表7-1桥丝直径与发火电压的关系",
    )

    assert not reasons
    assert row is not None
    assert row["所属表格ID"] == "T00001"
    assert row["所属表格标题"] == "表7-1桥丝直径与发火电压的关系"
    assert validate_rows([row])["ok"]


def test_validation_requires_direct_table_id_but_allows_empty_title() -> None:
    ontology = PropertyOntologyIndex([])
    row, reasons = apply_llm_fact(
        fact={
            "subject": "桥丝直径",
            "subject_type": "性能参数",
            "property": "取值",
            "relation_type": "属性",
            "value": "5μm",
            "unit": "μm",
            "condition": "",
            "polarity": "肯定",
            "subject_span": "桥丝直径",
            "value_span": "5μm",
            "confidence": 0.95,
        },
        ontology=ontology,
        evidence="桥丝直径为5μm。",
        book_title="火工品技术",
        document_id="doc:test",
        heading_path="感度影响因素",
        source_locator="L5-L5",
        source_type="llm_text_window_direct",
    )
    assert not reasons and row is not None
    row["来源类型"] = "llm_table_direct"

    report = validate_rows([row])

    assert not report["ok"]
    assert {error["reason"] for error in report["errors"]} >= {"empty_所属表格ID"}

    row["所属表格ID"] = "T00001"
    report = validate_rows([row])

    assert report["ok"]


def test_process_book_writes_table_title_provenance_catalog(tmp_path: Path) -> None:
    from book_engine.llm_v106.client import QwenResponse
    from book_engine.llm_v106.pipeline import BookInput, process_book

    source = tmp_path / "火工品技术.md"
    source.write_text(
        "# 感度影响因素\n\n"
        "<table><tr><td>桥丝直径</td><td>产品最大电阻</td></tr>"
        "<tr><td>5μm</td><td>21.4Ω</td></tr></table>\n",
        encoding="utf-8",
    )
    v105 = tmp_path / "v105" / "火工品技术"
    v105.mkdir(parents=True)
    output = tmp_path / "out"

    class MockClient:
        def chat_json(self, *, system, user, max_tokens, request_tag):  # noqa: ANN001
            if user.get("task") == "direct_scientific_fact_extraction" and user.get("unit_kind") == "table":
                data = {
                    "facts": [
                        {
                            "subject": "5μm桥丝",
                            "subject_type": "材料",
                            "property": "产品最大电阻",
                            "relation_type": "属性",
                            "value": "21.4Ω",
                            "unit": "Ω",
                            "condition": "",
                            "polarity": "肯定",
                            "subject_span": "5μm",
                            "value_span": "21.4Ω",
                            "confidence": 0.95,
                        }
                    ]
                }
            elif str(user.get("task", "")).startswith("complete_process"):
                data = {"processes": []}
            else:
                data = {"facts": [], "decisions": []}
            return QwenResponse(data=data, raw_content="{}", request_id="mock", usage={}, cached=False, attempts=1)

    report = process_book(
        BookInput("火工品技术", source, v105, "doc:test"),
        output_root=output,
        ontology=PropertyOntologyIndex([]),
        client=MockClient(),  # type: ignore[arg-type]
        candidate_batch_size=12,
        max_window_chars=6500,
        max_table_chars=30000,
        max_text_windows=0,
        max_tables=0,
        final_threshold=0.68,
        direct_max_facts_text=24,
        direct_max_facts_table=80,
        max_process_chars=12000,
        max_process_spans=0,
        process_threshold=0.72,
        process_max_steps=40,
        process_max_processes_per_span=3,
        plan_only=False,
    )

    rows = read_tsv(output / "火工品技术" / "step_graph_guard" / "graph_import_ready.tsv")
    assert report["status"] == "complete"
    assert len(rows) == 1
    assert rows[0]["来源定位"] == "L3-L3"
    assert rows[0]["所属表格ID"] == "T00001"
    assert rows[0]["所属表格标题"] == ""
    catalog = json.loads((output / "火工品技术" / "table_provenance_catalog.json").read_text(encoding="utf-8-sig"))
    assert catalog["tables"] == [
        {
            "table_id": "T00001",
            "table_title": "",
            "table_title_source": "context_only",
            "table_context": "感度影响因素",
            "caption_line_start": 0,
            "caption_line_end": 0,
            "line_start": 3,
            "line_end": 3,
        }
    ]


def test_acceptance_checker_rejects_direct_table_rows_without_id(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    book_dir = root / "火工品技术"
    guard = book_dir / "step_graph_guard"
    guard.mkdir(parents=True)
    row = {column: "" for column in SCHEMA59_COLUMNS}
    row.update(
        {
            "fact_id": "fact:test",
            "graph_fact_key": "gfk:test",
            "书名": "火工品技术",
            "来源类型": "llm_table_direct",
            "主体名称": "5μm桥丝",
            "attribute_name": "产品最大电阻",
            "尾实体/取值文本": "21.4Ω",
            "证据文本": "<table><tr><td>5μm</td><td>21.4Ω</td></tr></table>",
            "抽取来源": "qwen3_max_table_direct_v106",
        }
    )
    write_tsv(guard / "graph_import_ready.tsv", [row])
    write_tsv(guard / "graph_process_flow_ready.tsv", [])
    (book_dir / "v106_qwen3_max_book_report.json").write_text(
        json.dumps(
            {
                "book_title": "火工品技术",
                "final_rows": 1,
                "accepted_process_step_rows": 0,
                "llm_error_jobs": 0,
                "ok": True,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    checker = Path(__file__).resolve().parents[2] / "scripts" / "check_v106_qwen3_max_outputs.py"

    completed = subprocess.run(
        [sys.executable, str(checker), str(root), "--expected-books", "1"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    report = json.loads((root / "v106_qwen3_max_acceptance_report.json").read_text(encoding="utf-8-sig"))
    assert completed.returncode == 2
    assert any(error["reason"] == "direct_table_id_missing" for error in report["errors"])


def test_acceptance_checker_accepts_direct_table_rows_without_caption_title(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    book_dir = root / "火工品技术"
    guard = book_dir / "step_graph_guard"
    guard.mkdir(parents=True)
    row = {column: "" for column in SCHEMA59_COLUMNS}
    row.update(
        {
            "fact_id": "fact:test",
            "graph_fact_key": "gfk:test",
            "书名": "火工品技术",
            "来源类型": "llm_table_direct",
            "所属表格ID": "T00001",
            "所属表格标题": "",
            "主体名称": "5μm桥丝",
            "attribute_name": "产品最大电阻",
            "尾实体/取值文本": "21.4Ω",
            "证据文本": "<table><tr><td>5μm</td><td>21.4Ω</td></tr></table>",
            "抽取来源": "qwen3_max_table_direct_v106",
        }
    )
    write_tsv(guard / "graph_import_ready.tsv", [row])
    write_tsv(guard / "graph_process_flow_ready.tsv", [])
    (book_dir / "v106_qwen3_max_book_report.json").write_text(
        json.dumps(
            {
                "book_title": "火工品技术",
                "final_rows": 1,
                "accepted_process_step_rows": 0,
                "llm_error_jobs": 0,
                "ok": True,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    checker = Path(__file__).resolve().parents[2] / "scripts" / "check_v106_qwen3_max_outputs.py"

    completed = subprocess.run(
        [sys.executable, str(checker), str(root), "--expected-books", "1"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    assert completed.returncode == 0


def test_invalid_span_does_not_release() -> None:
    ontology = PropertyOntologyIndex([])
    row, reasons = apply_llm_fact(
        fact={
            "subject": "RDX",
            "subject_type": "材料",
            "property": "密度",
            "relation_type": "属性",
            "value": "1.81",
            "unit": "g/cm3",
            "condition": "",
            "polarity": "肯定",
            "subject_span": "RDX",
            "value_span": "1.81",
            "confidence": 0.9,
        },
        ontology=ontology,
        evidence="TNT 的密度为 1.65 g/cm3。",
        book_title="测试",
        document_id="doc:test",
        heading_path="",
        source_locator="L1-L1",
        source_type="llm_text_window_direct",
    )
    assert row is None
    assert "subject_span_not_supported" in reasons


def test_deduplicate_prefers_qwen_corrected_row() -> None:
    ontology = PropertyOntologyIndex([])
    evidence = "TNT 的密度为 1.65 g/cm3。"
    common = dict(
        ontology=ontology,
        evidence=evidence,
        book_title="测试",
        document_id="doc:test",
        heading_path="",
        source_locator="L1-L1",
        source_type="llm_text_window_direct",
    )
    fact = {
        "subject": "TNT", "subject_type": "材料", "property": "密度", "relation_type": "属性",
        "value": "1.65", "unit": "g/cm3", "condition": "", "polarity": "肯定",
        "subject_span": "TNT", "value_span": "1.65", "confidence": 0.9,
    }
    qwen, _ = apply_llm_fact(fact=fact, extraction_source="qwen3_max_text_direct_v106", **common)
    old, _ = apply_llm_fact(fact={**fact, "confidence": 0.8}, extraction_source="old_rule", **common)
    rows = deduplicate([old, qwen])
    assert len(rows) == 1
    assert "qwen3" in rows[0]["抽取来源"]
