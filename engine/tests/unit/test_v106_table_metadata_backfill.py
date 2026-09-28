from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

from book_engine.llm_v106.table_metadata_backfill import (
    TableCatalog,
    build_resolution_payload,
    discover_book_jobs,
    repair_book_directory,
    repair_rows,
    repair_tsv,
    validate_resolution_response,
)


def _source(tmp_path: Path) -> Path:
    path = tmp_path / "测试书.md"
    path.write_text(
        "# 第一章\n\n"
        "表1 RDX密度\n\n"
        "<table><tr><td>材料</td><td>密度</td></tr><tr><td>RDX</td><td>1.81</td></tr></table>\n\n"
        "表2 HMX密度\n\n"
        "<table><tr><td>材料</td><td>密度</td></tr><tr><td>HMX</td><td>1.90</td></tr></table>\n",
        encoding="utf-8",
    )
    return path


def _uncaptioned_source(tmp_path: Path) -> Path:
    path = tmp_path / "无题注测试书.md"
    path.write_text(
        "# 复合推进剂\n\n"
        "<table><tr><td>材料</td><td>密度</td></tr><tr><td>RDX</td><td>1.81</td></tr></table>\n",
        encoding="utf-8",
    )
    return path


def _row(**updates: str) -> dict[str, str]:
    row = {
        "fact_id": "fact:1",
        "书名": "测试书",
        "章节路径": "第一章",
        "来源定位": "L5-L9",
        "来源类型": "llm_table_direct",
        "所属表格ID": "",
        "所属表格标题": "",
        "主体名称": "RDX",
        "predicate_raw": "密度",
        "attribute_name": "密度",
        "尾实体/取值文本": "1.81",
        "数值": "1.81",
        "证据文本": "旧证据必须保持不变",
    }
    row.update(updates)
    return row


def test_single_candidate_backfill_changes_only_table_metadata(tmp_path: Path) -> None:
    catalog = TableCatalog.from_markdown(_source(tmp_path))
    original = _row(来源定位="L5-L5")

    repaired, audit, unresolved = repair_rows([original], catalog)

    assert repaired[0]["所属表格ID"] == "T00001"
    assert repaired[0]["所属表格标题"] == "表1 RDX密度"
    assert {k: v for k, v in repaired[0].items() if k not in {"所属表格ID", "所属表格标题"}} == {
        k: v for k, v in original.items() if k not in {"所属表格ID", "所属表格标题"}
    }
    assert audit["filled_single_candidate"] == 1
    assert unresolved == []


def test_content_match_selects_the_correct_table_from_merged_locator(tmp_path: Path) -> None:
    catalog = TableCatalog.from_markdown(_source(tmp_path))
    row = _row(
        来源定位="L5-L9",
        主体名称="HMX",
        尾实体_取值文本="unused",
        **{"尾实体/取值文本": "1.90", "数值": "1.90"},
    )

    repaired, audit, unresolved = repair_rows([row], catalog)

    assert repaired[0]["所属表格ID"] == "T00002"
    assert repaired[0]["所属表格标题"] == "表2 HMX密度"
    assert audit["filled_content_match"] == 1
    assert unresolved == []


def test_ambiguous_row_is_not_force_filled_without_resolution(tmp_path: Path) -> None:
    catalog = TableCatalog.from_markdown(_source(tmp_path))
    row = _row(来源定位="L5-L9", 主体名称="材料", **{"尾实体/取值文本": "密度", "数值": ""})

    repaired, audit, unresolved = repair_rows([row], catalog)

    assert repaired[0]["所属表格ID"] == ""
    assert repaired[0]["所属表格标题"] == ""
    assert audit["unresolved"] == 1
    assert unresolved[0]["fact_id"] == "fact:1"
    assert unresolved[0]["candidate_table_ids"] == "T00001|T00002"


def test_explicit_resolution_fills_an_ambiguous_row(tmp_path: Path) -> None:
    catalog = TableCatalog.from_markdown(_source(tmp_path))
    row = _row(来源定位="L5-L9", 主体名称="材料", **{"尾实体/取值文本": "密度", "数值": ""})

    repaired, audit, unresolved = repair_rows([row], catalog, resolutions={"fact:1": "T00002"})

    assert repaired[0]["所属表格ID"] == "T00002"
    assert repaired[0]["所属表格标题"] == "表2 HMX密度"
    assert audit["filled_external_resolution"] == 1
    assert unresolved == []


def test_existing_section_fallback_title_is_canonicalized_to_caption(tmp_path: Path) -> None:
    catalog = TableCatalog.from_markdown(_source(tmp_path))
    row = _row(来源定位="L5-L5", 所属表格ID="T00001", 所属表格标题="第一章")

    repaired, audit, unresolved = repair_rows([row], catalog)

    assert repaired[0]["所属表格ID"] == "T00001"
    assert repaired[0]["所属表格标题"] == "表1 RDX密度"
    assert audit["canonicalized_existing_title"] == 1
    assert unresolved == []


def test_repair_rows_clears_existing_context_title_for_uncaptioned_table(tmp_path: Path) -> None:
    catalog = TableCatalog.from_markdown(_uncaptioned_source(tmp_path))

    repaired, audit, unresolved = repair_rows([
        _row(来源定位="L3-L3", 所属表格ID="T00001", 所属表格标题="复合推进剂")
    ], catalog)

    assert catalog.by_id["T00001"].title_source == "context_only"
    assert not unresolved
    assert repaired[0]["所属表格ID"] == "T00001"
    assert repaired[0]["所属表格标题"] == ""
    assert audit["cleared_non_caption_title"] == 1


def test_repair_rows_fills_id_without_title_for_uncaptioned_table(tmp_path: Path) -> None:
    catalog = TableCatalog.from_markdown(_uncaptioned_source(tmp_path))

    repaired, audit, unresolved = repair_rows([_row(来源定位="L3-L3")], catalog)

    assert catalog.by_id["T00001"].title_source == "context_only"
    assert not unresolved
    assert repaired[0]["所属表格ID"] == "T00001"
    assert repaired[0]["所属表格标题"] == ""
    assert audit["filled_single_candidate"] == 1


def test_repair_tsv_preserves_header_order_row_count_and_non_target_values(tmp_path: Path) -> None:
    catalog = TableCatalog.from_markdown(_source(tmp_path))
    source_tsv = tmp_path / "input.tsv"
    output_tsv = tmp_path / "output.tsv"
    fields = list(_row().keys())
    rows = [_row(来源定位="L5-L5"), _row(fact_id="fact:2", 来源类型="llm_text_window_direct")]
    with source_tsv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)

    report = repair_tsv(source_tsv, output_tsv, catalog)

    with output_tsv.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        output_rows = list(reader)
        assert reader.fieldnames == fields
    assert len(output_rows) == 2
    assert output_rows[0]["所属表格ID"] == "T00001"
    assert output_rows[1] == rows[1]
    assert report["rows"] == 2


def test_resolution_payload_contains_only_overlapping_candidate_tables(tmp_path: Path) -> None:
    catalog = TableCatalog.from_markdown(_source(tmp_path))
    unresolved = [{
        "fact_id": "fact:1",
        "书名": "测试书",
        "来源定位": "L5-L9",
        "主体名称": "材料",
        "predicate_raw": "密度",
        "尾实体/取值文本": "1.90",
        "数值": "1.90",
        "条件文本": "常温",
        "candidate_table_ids": "T00001|T00002",
    }]

    payload = build_resolution_payload("测试书", "L5-L9", unresolved, catalog)

    assert [table["table_id"] for table in payload["candidate_tables"]] == ["T00001", "T00002"]
    assert payload["facts"][0]["fact_id"] == "fact:1"
    assert payload["facts"][0]["value"] == "1.90"


def test_resolution_response_rejects_unknown_duplicate_and_missing_assignments() -> None:
    unresolved = [
        {"fact_id": "fact:1", "candidate_table_ids": "T00001|T00002"},
        {"fact_id": "fact:2", "candidate_table_ids": "T00003|T00004"},
    ]
    response = {
        "assignments": [
            {"fact_id": "fact:1", "table_id": "T99999"},
            {"fact_id": "fact:1", "table_id": "T00002"},
        ]
    }

    resolutions, rejected = validate_resolution_response(response, unresolved)

    assert resolutions == {}
    assert rejected == {
        "fact:1": "duplicate_assignment",
        "fact:2": "missing_assignment",
    }


def test_resolution_response_accepts_one_valid_candidate_per_fact() -> None:
    unresolved = [
        {"fact_id": "fact:1", "candidate_table_ids": "T00001|T00002"},
        {"fact_id": "fact:2", "candidate_table_ids": "T00003|T00004"},
    ]
    response = {
        "assignments": [
            {"fact_id": "fact:1", "table_id": "T00002"},
            {"fact_id": "fact:2", "table_id": "T00003"},
        ]
    }

    resolutions, rejected = validate_resolution_response(response, unresolved)

    assert resolutions == {"fact:1": "T00002", "fact:2": "T00003"}
    assert rejected == {}


def test_repair_book_directory_copies_sidecars_and_repairs_every_schema_tsv(tmp_path: Path) -> None:
    markdown = _source(tmp_path)
    source_book = tmp_path / "source_book"
    guard = source_book / "step_graph_guard"
    guard.mkdir(parents=True)
    fields = list(_row().keys())
    for name in ("graph_import_ready.tsv", "graph_candidate_review_59.tsv"):
        with (guard / name).open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t")
            writer.writeheader()
            writer.writerow(_row(来源定位="L5-L5"))
    (source_book / "v106_qwen3_max_book_report.json").write_text('{"ok": true}', encoding="utf-8")
    output_book = tmp_path / "output_book"

    report = repair_book_directory(source_book, output_book, markdown)

    assert (output_book / "v106_qwen3_max_book_report.json").read_text(encoding="utf-8") == '{"ok": true}'
    for name in ("graph_import_ready.tsv", "graph_candidate_review_59.tsv"):
        with (output_book / "step_graph_guard" / name).open("r", encoding="utf-8-sig", newline="") as handle:
            repaired = next(csv.DictReader(handle, delimiter="\t"))
        assert repaired["所属表格ID"] == "T00001"
        assert repaired["所属表格标题"] == "表1 RDX密度"
    catalog = json.loads((output_book / "table_provenance_catalog.json").read_text(encoding="utf-8-sig"))
    assert catalog["tables"][0]["table_title_source"] == "source_caption"
    assert catalog["tables"][0]["caption_line_start"] == 3
    assert report["tsv_files_repaired"] == 2
    assert report["filled_single_candidate"] == 2


def test_discover_book_jobs_matches_originals_and_excludes_v105(tmp_path: Path) -> None:
    root = tmp_path / "batch"
    originals = root / "甲原书汇总"
    originals.mkdir(parents=True)
    (originals / "测试书.md").write_text("# 测试", encoding="utf-8")
    v106_book = root / "甲" / "v106_qwen3_max_all_books" / "测试书"
    v106_book.mkdir(parents=True)
    (v106_book / "v106_qwen3_max_book_report.json").write_text("{}", encoding="utf-8")
    v105_book = root / "甲" / "v105_generalized_all_books" / "测试书"
    v105_book.mkdir(parents=True)
    (v105_book / "v106_qwen3_max_book_report.json").write_text("{}", encoding="utf-8")

    jobs = discover_book_jobs(root)

    assert len(jobs) == 1
    assert jobs[0].book_title == "测试书"
    assert jobs[0].source_markdown == originals / "测试书.md"
    assert jobs[0].relative_output_dir == Path("甲/v106_qwen3_max_all_books/测试书")


def test_discover_book_jobs_supports_separate_roots_and_explicit_title_map(tmp_path: Path) -> None:
    originals = tmp_path / "唐伟均原属汇总"
    originals.mkdir()
    source = originals / "冲压发动机的調節問題.md"
    source.write_text("# 测试", encoding="utf-8")
    results = tmp_path / "v106_qwen3_max_cn12"
    book = results / "冲压发动机的调节问题"
    book.mkdir(parents=True)
    (book / "v106_qwen3_max_book_report.json").write_text("{}", encoding="utf-8")

    jobs = discover_book_jobs(
        tmp_path,
        source_books_root=originals,
        results_root=results,
        title_map={"冲压发动机的调节问题": "冲压发动机的調節問題"},
    )

    assert len(jobs) == 1
    assert jobs[0].book_title == "冲压发动机的调节问题"
    assert jobs[0].source_markdown == source
    assert jobs[0].relative_output_dir == Path("冲压发动机的调节问题")


def test_discover_book_jobs_normalizes_spacing_punctuation_and_width(tmp_path: Path) -> None:
    originals = tmp_path / "originals"
    originals.mkdir()
    source = originals / "冲压发动机技术(上册).md"
    source.write_text("# 测试", encoding="utf-8")
    results = tmp_path / "results"
    book = results / "冲压发动机技术（上册）"
    book.mkdir(parents=True)
    (book / "v106_qwen3_max_book_report.json").write_text("{}", encoding="utf-8")

    jobs = discover_book_jobs(
        tmp_path,
        source_books_root=originals,
        results_root=results,
    )

    assert len(jobs) == 1
    assert jobs[0].source_markdown == source


def test_repair_runner_plan_only_audits_without_creating_output(tmp_path: Path) -> None:
    root = tmp_path / "batch"
    originals = root / "甲原书汇总"
    originals.mkdir(parents=True)
    markdown = originals / "测试书.md"
    markdown.write_text(_source(tmp_path).read_text(encoding="utf-8"), encoding="utf-8")
    book = root / "甲" / "v106_qwen3_max_all_books" / "测试书"
    guard = book / "step_graph_guard"
    guard.mkdir(parents=True)
    (book / "v106_qwen3_max_book_report.json").write_text("{}", encoding="utf-8")
    fields = list(_row().keys())
    with (guard / "graph_import_ready.tsv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        writer.writerow(_row(来源定位="L5-L5"))
    output = tmp_path / "repaired"
    report = tmp_path / "plan.json"
    script = Path(__file__).resolve().parents[2] / "scripts" / "repair_v106_table_metadata_backfill.py"

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--input-root",
            str(root),
            "--output-root",
            str(output),
            "--plan-only",
            "--report-path",
            str(report),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    plan = json.loads(report.read_text(encoding="utf-8"))
    assert plan["book_count"] == 1
    assert plan["missing_table_metadata_rows"] == 1
    assert plan["deterministically_resolved_rows"] == 1
    assert not output.exists()


def test_repair_runner_plan_only_accepts_separate_roots_and_title_map(tmp_path: Path) -> None:
    originals = tmp_path / "唐伟均原属汇总"
    originals.mkdir()
    markdown = originals / "测试原题.md"
    markdown.write_text(_source(tmp_path).read_text(encoding="utf-8"), encoding="utf-8")
    results = tmp_path / "v106_results"
    book = results / "测试结果题"
    guard = book / "step_graph_guard"
    guard.mkdir(parents=True)
    (book / "v106_qwen3_max_book_report.json").write_text("{}", encoding="utf-8")
    fields = list(_row(书名="测试结果题").keys())
    with (guard / "graph_import_ready.tsv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        writer.writerow(_row(书名="测试结果题", 来源定位="L5-L5"))
    title_map = tmp_path / "title_map.json"
    title_map.write_text(json.dumps({"测试结果题": "测试原题"}, ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "repaired"
    report = tmp_path / "plan.json"
    script = Path(__file__).resolve().parents[2] / "scripts" / "repair_v106_table_metadata_backfill.py"

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--source-books-root",
            str(originals),
            "--results-root",
            str(results),
            "--title-map",
            str(title_map),
            "--output-root",
            str(output),
            "--plan-only",
            "--report-path",
            str(report),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    plan = json.loads(report.read_text(encoding="utf-8"))
    assert plan["book_count"] == 1
    assert plan["source_books_root"] == str(originals.resolve())
    assert plan["results_root"] == str(results.resolve())
    assert plan["missing_table_metadata_rows"] == 1
    assert not output.exists()
