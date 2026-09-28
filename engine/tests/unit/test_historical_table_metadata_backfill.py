from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

from book_engine.llm_v106.historical_table_metadata_backfill import (
    read_fact_rows,
    repair_historical_rows,
    resolve_source_book,
    run_batch,
)
from book_engine.llm_v106.table_metadata_backfill import TableCatalog


def test_read_fact_rows_supports_utf8_tsv_and_gb18030_csv(tmp_path: Path) -> None:
    tsv = tmp_path / "facts.tsv"
    tsv.write_text("fact_id\t书名\nF1\t测试书\n", encoding="utf-8-sig")
    csv = tmp_path / "facts.csv"
    csv.write_bytes("fact_id,书名\nF2,中文书\n".encode("gb18030"))

    tsv_fields, tsv_rows = read_fact_rows(tsv)
    csv_fields, csv_rows = read_fact_rows(csv)

    assert tsv_fields == ["fact_id", "书名"]
    assert tsv_rows == [{"fact_id": "F1", "书名": "测试书"}]
    assert csv_fields == ["fact_id", "书名"]
    assert csv_rows == [{"fact_id": "F2", "书名": "中文书"}]


def test_resolve_source_book_uses_explicit_alias_for_input_007(tmp_path: Path) -> None:
    markdown = tmp_path / "Assessment of Joint Improvised Explosive Device Defeat Organization (JIEDDO) Training Activity.md"
    markdown.write_text("# source", encoding="utf-8")

    resolved = resolve_source_book(
        "input_007",
        tmp_path / "Assessment of Joint Improvised Explosive Device Defeat Organization.tsv",
        [tmp_path],
        {"input_007": markdown.stem},
    )

    assert resolved == markdown


def test_resolve_source_book_rejects_missing_book(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing original Markdown"):
        resolve_source_book("missing", tmp_path / "missing.tsv", [tmp_path], {})


def test_repair_historical_rows_fills_captioned_table_and_clears_context_title(tmp_path: Path) -> None:
    markdown = tmp_path / "测试书.md"
    markdown.write_text(
        "表1 材料密度\n\n"
        "<table><tr><td>材料</td><td>密度</td></tr><tr><td>RDX</td><td>1.81</td></tr></table>\n\n"
        "# 无题注表\n\n"
        "<table><tr><td>材料</td><td>密度</td></tr><tr><td>HMX</td><td>1.90</td></tr></table>\n",
        encoding="utf-8",
    )
    rows = [
        {
            "fact_id": "F1", "书名": "测试书", "来源类型": "llm_table_direct", "来源定位": "L3-L3",
            "所属表格ID": "", "所属表格标题": "", "主体名称": "RDX", "尾实体/取值文本": "1.81",
        },
        {
            "fact_id": "F2", "书名": "测试书", "来源类型": "llm_table_direct", "来源定位": "L7-L7",
            "所属表格ID": "", "所属表格标题": "无题注表", "主体名称": "HMX", "尾实体/取值文本": "1.90",
        },
    ]

    repaired, audit, unresolved = repair_historical_rows(rows, TableCatalog.from_markdown(markdown))

    assert repaired[0]["所属表格ID"] == "T00001"
    assert repaired[0]["所属表格标题"] == "表1 材料密度"
    assert repaired[1]["所属表格ID"] == "T00002"
    assert repaired[1]["所属表格标题"] == ""
    assert audit["filled_single_candidate"] == 2
    assert unresolved == []


def test_repair_historical_rows_replaces_legacy_table_id_when_locator_is_unique(tmp_path: Path) -> None:
    markdown = tmp_path / "测试书.md"
    markdown.write_text(
        "表1 材料密度\n\n<table><tr><td>材料</td><td>密度</td></tr><tr><td>RDX</td><td>1.81</td></tr></table>\n",
        encoding="utf-8",
    )
    rows = [{
        "fact_id": "F1", "书名": "测试书", "来源类型": "表格", "来源定位": "L3-L3",
        "所属表格ID": "table_00001", "所属表格标题": "旧标题", "主体名称": "RDX", "尾实体/取值文本": "1.81",
    }]

    repaired, audit, unresolved = repair_historical_rows(rows, TableCatalog.from_markdown(markdown))

    assert repaired[0]["所属表格ID"] == "T00001"
    assert repaired[0]["所属表格标题"] == "表1 材料密度"
    assert audit["normalized_legacy_table_id"] == 1
    assert unresolved == []


def test_repair_historical_rows_uses_validated_external_resolution(tmp_path: Path) -> None:
    markdown = tmp_path / "测试书.md"
    markdown.write_text(
        "表1 RDX\n\n<table><tr><td>材料</td><td>数值</td></tr><tr><td>RDX</td><td>1</td></tr></table>\n\n"
        "表2 HMX\n\n<table><tr><td>材料</td><td>数值</td></tr><tr><td>HMX</td><td>2</td></tr></table>\n",
        encoding="utf-8",
    )
    row = {
        "fact_id": "F1", "书名": "测试书", "来源类型": "llm_table_direct", "来源定位": "L3-L7",
        "所属表格ID": "", "所属表格标题": "", "主体名称": "材料", "尾实体/取值文本": "数值",
    }

    repaired, audit, unresolved = repair_historical_rows(
        [row], TableCatalog.from_markdown(markdown), resolutions={"F1": "T00002"}
    )

    assert repaired[0]["所属表格ID"] == "T00002"
    assert repaired[0]["所属表格标题"] == "表2 HMX"
    assert audit["filled_external_resolution"] == 1
    assert unresolved == []


def test_run_batch_writes_repaired_tsv_and_audit_without_mutating_input(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    input_root.mkdir()
    source_root = tmp_path / "sources"
    source_root.mkdir()
    (source_root / "测试书.md").write_text(
        "表1 材料密度\n\n<table><tr><td>材料</td><td>密度</td></tr><tr><td>RDX</td><td>1.81</td></tr></table>\n",
        encoding="utf-8",
    )
    input_tsv = input_root / "测试书.tsv"
    input_tsv.write_text(
        "fact_id\t书名\t来源类型\t来源定位\t所属表格ID\t所属表格标题\t主体名称\t尾实体/取值文本\n"
        "F1\t测试书\tllm_table_direct\tL3-L3\t\t\tRDX\t1.81\n",
        encoding="utf-8-sig",
    )
    output_root = tmp_path / "output"

    report = run_batch(input_root, [source_root], output_root, {})

    fields, rows = read_fact_rows(output_root / "测试书.tsv")
    assert fields[0] == "fact_id"
    assert rows[0]["所属表格ID"] == "T00001"
    assert rows[0]["所属表格标题"] == "表1 材料密度"
    assert input_tsv.read_text(encoding="utf-8-sig").splitlines()[1].endswith("\t\tRDX\t1.81")
    assert report["resolved_rows"] == 1
    assert (output_root / "table_metadata_backfill_report.json").exists()
    assert (output_root / "source_book_mapping.tsv").exists()
    assert (output_root / "table_provenance_catalogs" / "测试书" / "table_provenance_catalog.json").exists()


def test_run_batch_uses_an_explicit_manifest_to_exclude_other_schema_files(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    input_root.mkdir()
    source_root = tmp_path / "sources"
    source_root.mkdir()
    (source_root / "测试书.md").write_text("# source", encoding="utf-8")
    header = "fact_id\t书名\t来源类型\t来源定位\t所属表格ID\t所属表格标题\n"
    (input_root / "keep.tsv").write_text(header + "F1\t测试书\tllm_text_window_direct\tL1-L1\t\t\n", encoding="utf-8-sig")
    (input_root / "deleted.tsv").write_text(header + "F2\t测试书\tllm_text_window_direct\tL1-L1\t\t\n", encoding="utf-8-sig")
    output_root = tmp_path / "output"

    report = run_batch(input_root, [source_root], output_root, {}, included_files=[Path("keep.tsv")])

    assert report["processed_files"] == 1
    assert (output_root / "keep.tsv").exists()
    assert not (output_root / "deleted.tsv").exists()


def test_run_batch_uses_majority_title_when_legacy_placeholder_shares_one_document(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    input_root.mkdir()
    source_root = tmp_path / "sources"
    source_root.mkdir()
    source = source_root / "真实书名.md"
    source.write_text("# source", encoding="utf-8")
    (input_root / "mixed.tsv").write_text(
        "fact_id\t文档ID\t书名\t来源类型\t来源定位\t所属表格ID\t所属表格标题\n"
        "F1\tD1\tinput_007\tllm_text_window_direct\tL1-L1\t\t\n"
        "F2\tD1\t真实书名\tllm_text_window_direct\tL1-L1\t\t\n",
        encoding="utf-8-sig",
    )

    report = run_batch(input_root, [source_root], tmp_path / "output", {"input_007": source.stem})

    assert report["processed_files"] == 1


def test_historical_repair_cli_refuses_output_equal_to_input(tmp_path: Path) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    script = Path(__file__).resolve().parents[2] / "scripts" / "repair_historical_table_metadata_backfill.py"

    completed = subprocess.run(
        [
            sys.executable, str(script), "--input-root", str(tmp_path), "--markdown-root", str(source_root),
            "--output-root", str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "input-root and output-root must differ" in completed.stderr
