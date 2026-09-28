from __future__ import annotations

from pathlib import Path
import os
import sys
import json
import time

import pytest

from app.extraction.legacy import output_parser
from app.extraction.legacy.output_parser import (
    LegacyParseError,
    parse_candidate_tsv,
    parse_v105_report,
    parse_v106_report,
    resolve_legacy_output,
)
from app.extraction.legacy.v105_rule import V105RuleAdapter
from app.extraction.legacy.v106_hybrid import V106HybridAdapter
from app.extraction.legacy.capabilities import CapabilityError
from app.extraction.contracts import AdapterContext, StageCleanupError
from app.extraction.process_runner import ProcessIsolationError, ProcessRunner
from app.extraction.registry import (
    ADAPTERS,
    LEGACY_ADAPTERS,
    build_legacy_adapters,
    execution_adapter_for,
    legacy_adapters_from_environment,
)


FIXTURES = Path(__file__).parent / "fixtures"


def test_v105_parser_reports_success() -> None:
    parsed = parse_v105_report(FIXTURES / "v105_report.json")

    assert parsed.status == "completed"
    assert parsed.retryable_errors == 0
    assert parsed.metrics["rows"] == 12


def test_v106_parser_reports_partial_success() -> None:
    parsed = parse_v106_report(FIXTURES / "v106_report.json")

    assert parsed.status == "partial_success"
    assert parsed.retryable_errors == 1
    assert parsed.metrics["final_rows"] == 8


def test_tsv_parser_preserves_headers_and_rows() -> None:
    expected = ("book_title", "status", "final_rows", "llm_error_jobs")
    parsed = parse_candidate_tsv(FIXTURES / "v106_manifest.tsv", expected_headers=expected)

    assert parsed.headers == ("book_title", "status", "final_rows", "llm_error_jobs")
    assert parsed.rows[0]["book_title"] == "fixture book"
    assert parsed.rows[0]["final_rows"] == "8"


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        '{"ok": NaN}',
        '{"ok": Infinity}',
        '{"ok": "false"}',
        '{"ok": true, "llm_error_jobs": -1}',
        '{"ok": true, "llm_error_jobs": 9223372036854775808}',
        '{"ok": true, "final_rows": 1.25}',
    ],
)
def test_report_parser_rejects_malformed_and_invalid_scalars(
    tmp_path: Path, payload: str
) -> None:
    report = tmp_path / "secret-report-name.json"
    report.write_text(payload, encoding="utf-8")

    with pytest.raises(LegacyParseError) as caught:
        parse_v106_report(report)

    assert "secret-report-name" not in str(caught.value)
    assert payload not in str(caught.value)


def test_report_parser_rejects_missing_and_oversized_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(output_parser, "MAX_REPORT_BYTES", 32)
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * 33)

    for path in (tmp_path / "missing-secret.json", oversized):
        with pytest.raises(LegacyParseError) as caught:
            parse_v105_report(path)
        assert str(path) not in str(caught.value)


def test_report_parser_limits_json_depth_and_node_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = tmp_path / "deep.json"
    nested: object = 0
    for _ in range(output_parser.MAX_JSON_DEPTH + 1):
        nested = [nested]
    report.write_text(json.dumps({"ok": True, "nested": nested}), encoding="utf-8")
    with pytest.raises(LegacyParseError):
        parse_v105_report(report)

    monkeypatch.setattr(output_parser, "MAX_JSON_NODES", 8)
    report.write_text(json.dumps({"ok": True, "nodes": list(range(10))}), encoding="utf-8")
    with pytest.raises(LegacyParseError):
        parse_v105_report(report)


def test_report_parser_sanitizes_memory_and_decoder_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = tmp_path / "private.json"
    report.write_text('{"ok": true}', encoding="utf-8")
    monkeypatch.setattr(output_parser.json, "loads", lambda *args, **kwargs: (_ for _ in ()).throw(MemoryError("secret")))

    with pytest.raises(LegacyParseError) as caught:
        parse_v105_report(report)
    assert "secret" not in str(caught.value)


def test_report_parser_sanitizes_result_materialization_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = tmp_path / "report.json"
    report.write_text('{"ok": true}', encoding="utf-8")
    monkeypatch.setattr(
        output_parser,
        "ParsedLegacyReport",
        lambda *args, **kwargs: (_ for _ in ()).throw(MemoryError("private")),
    )

    with pytest.raises(LegacyParseError) as caught:
        parse_v105_report(report)
    assert "private" not in str(caught.value)


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO semantics")
def test_parser_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "input.fifo"
    os.mkfifo(fifo)
    started = time.monotonic()
    with pytest.raises(LegacyParseError):
        parse_v105_report(fifo)
    assert time.monotonic() - started < 1


@pytest.mark.parametrize(
    "unsafe",
    ["../escape.tsv", "/absolute.tsv", r"C:\escape.tsv", "safe/../../escape.tsv"],
)
def test_v106_parser_rejects_unsafe_output_paths(tmp_path: Path, unsafe: str) -> None:
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps({"ok": True, "status": "complete", "outputs": {"final": unsafe}}),
        encoding="utf-8",
    )

    with pytest.raises(LegacyParseError, match="invalid"):
        parse_v106_report(report)


def test_resolve_legacy_output_is_relative_to_workdir(tmp_path: Path) -> None:
    resolved = resolve_legacy_output(tmp_path, "legacy_v106/final.tsv")
    assert resolved == (tmp_path / "legacy_v106" / "final.tsv").resolve()
    with pytest.raises(LegacyParseError):
        resolve_legacy_output(tmp_path, "../outside.tsv")


@pytest.mark.parametrize(
    "content",
    [
        "a\ta\n1\t2\n",
        "a\tb\n1\n",
        "a\tb\n1\t2\t3\n",
        "a\tc\n1\t2\n",
    ],
)
def test_tsv_parser_rejects_duplicate_missing_extra_and_unknown_columns(
    tmp_path: Path, content: str
) -> None:
    source = tmp_path / "hostile.tsv"
    source.write_text(content, encoding="utf-8")

    with pytest.raises(LegacyParseError):
        parse_candidate_tsv(source, expected_headers=("a", "b"))

def test_tsv_parser_enforces_byte_row_and_field_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "limited.tsv"
    monkeypatch.setattr(output_parser, "MAX_TSV_BYTES", 16)
    source.write_text("a\tb\n123456789\t2\n", encoding="utf-8")
    with pytest.raises(LegacyParseError):
        parse_candidate_tsv(source, expected_headers=("a", "b"))

    monkeypatch.setattr(output_parser, "MAX_TSV_BYTES", 1024)
    monkeypatch.setattr(output_parser, "MAX_ROWS", 1)
    source.write_text("a\tb\n1\t2\n3\t4\n", encoding="utf-8")
    with pytest.raises(LegacyParseError):
        parse_candidate_tsv(source, expected_headers=("a", "b"))

    monkeypatch.setattr(output_parser, "MAX_ROWS", 10)
    monkeypatch.setattr(output_parser, "MAX_FIELD_LENGTH", 3)
    source.write_text("a\tb\nlong\t2\n", encoding="utf-8")
    with pytest.raises(LegacyParseError):
        parse_candidate_tsv(source, expected_headers=("a", "b"))


def test_tsv_parser_limits_columns_line_bytes_and_retained_cells(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "wide.tsv"
    headers = [f"c{i}" for i in range(output_parser.MAX_COLUMNS + 1)]
    source.write_text("\t".join(headers) + "\n", encoding="utf-8")
    with pytest.raises(LegacyParseError):
        parse_candidate_tsv(source)

    monkeypatch.setattr(output_parser, "MAX_TSV_LINE_BYTES", 16)
    source.write_text("a\tb\n" + "x" * 17 + "\t2\n", encoding="utf-8")
    with pytest.raises(LegacyParseError):
        parse_candidate_tsv(source, expected_headers=("a", "b"))

    monkeypatch.setattr(output_parser, "MAX_TSV_LINE_BYTES", 1024)
    monkeypatch.setattr(output_parser, "MAX_RETAINED_CELL_BYTES", 5)
    source.write_text("a\tb\n123\t456\n", encoding="utf-8")
    with pytest.raises(LegacyParseError):
        parse_candidate_tsv(source, expected_headers=("a", "b"))


def test_tsv_parser_supports_multiline_quoted_fields_and_large_valid_field(
    tmp_path: Path,
) -> None:
    multiline = tmp_path / "multiline.tsv"
    multiline.write_text('a\tb\n"line one\nline two"\tvalue\n', encoding="utf-8")

    parsed = parse_candidate_tsv(multiline, expected_headers=("a", "b"))
    assert parsed.rows[0]["a"].splitlines() == ["line one", "line two"]

    large = tmp_path / "large.tsv"
    field = "x" * (150 * 1024)
    large.write_text(f"a\tb\n{field}\tvalue\n", encoding="utf-8")
    parsed_large = parse_candidate_tsv(large, expected_headers=("a", "b"))
    assert parsed_large.rows[0]["a"] == field


def test_v105_declares_combined_rule_capability_only(tmp_path: Path) -> None:
    adapter = V105RuleAdapter(source_root=tmp_path)

    assert adapter.capability.mode == "combined_rule_text_table"
    adapter.require_routes({"rule_text", "rule_table"})
    with pytest.raises(CapabilityError) as caught:
        adapter.require_routes({"rule_text"})

    assert caught.value.engine == "legacy_v105_rule"
    assert caught.value.requested_routes == ("rule_text",)
    assert caught.value.supported_route_sets == (("rule_table", "rule_text"),)


def test_v106_declares_hybrid_compatibility_capability(tmp_path: Path) -> None:
    adapter = V106HybridAdapter(source_root=tmp_path)

    assert adapter.capability.mode == "combined_hybrid_compatibility"
    adapter.require_routes({"rule_text", "rule_table", "llm_text", "llm_table"})
    with pytest.raises(CapabilityError):
        adapter.require_routes({"llm_text"})


def test_legacy_registry_keys_do_not_replace_four_current_routes() -> None:
    assert set(ADAPTERS) == {"rule_text", "llm_text", "rule_table", "llm_table"}
    assert LEGACY_ADAPTERS == {}
    configured = build_legacy_adapters(
        engine_root=Path("configured-engine"),
        input_roots=(Path("configured-input"),),
    )
    assert set(configured) == {"legacy_v105_rule", "legacy_v106_hybrid"}
    with pytest.raises(Exception, match="not configured"):
        execution_adapter_for("legacy_v105_rule")


def test_legacy_environment_configuration_uses_json_not_pathsep() -> None:
    configured = legacy_adapters_from_environment(
        {
            "LEGACY_ENGINE_SOURCE_ROOT": r"C:\engine",
            "LEGACY_READONLY_INPUT_ROOTS_JSON": json.dumps(
                [r"D:\input-one", r"E:\input-two"]
            ),
        }
    )
    assert len(configured["legacy_v105_rule"].input_source_roots) == 2
    with pytest.raises(Exception, match="not configured"):
        legacy_adapters_from_environment(
            {
                "LEGACY_ENGINE_SOURCE_ROOT": r"C:\engine",
                "LEGACY_READONLY_INPUT_ROOTS_JSON": r"D:\one;E:\two",
            }
        )


def test_v105_adapter_runs_only_staged_inputs_with_fake_engine(tmp_path: Path) -> None:
    source_root = tmp_path / "fake-v105"
    entry = source_root / "model" / "src" / "book_engine" / "cli.py"
    entry.parent.mkdir(parents=True)
    (entry.parent / "__init__.py").write_text("", encoding="utf-8")
    entry.write_text(
        "import argparse,json,os,pathlib\n"
        "p=argparse.ArgumentParser(); p.add_argument('--input-md'); p.add_argument('--output-dir'); "
        "p.add_argument('--config'); p.add_argument('--mode'); a=p.parse_args()\n"
        "root=pathlib.Path.cwd().resolve()\n"
        "assert all(pathlib.Path(v).resolve().is_relative_to(root) for v in (a.input_md,a.config,a.output_dir,os.environ['PYTHONPATH']))\n"
        "assert any(p.name == 'staged-engine' for p in pathlib.Path(__file__).resolve().parents)\n"
        "assert any(p.name.startswith('.stage-') for p in pathlib.Path(__file__).resolve().parents)\n"
        "out=pathlib.Path(a.output_dir); out.mkdir(parents=True,exist_ok=True)\n"
        "(out/'book_engine_run_report.json').write_text(json.dumps({'ok':True,'schema59_merged_export':{'rows':3}}))\n",
        encoding="utf-8",
    )
    inputs = tmp_path / "readonly-inputs"
    document_dir = inputs / "document"
    config_dir = inputs / "config"
    document_dir.mkdir(parents=True)
    config_dir.mkdir()
    document = document_dir / "same-name.dat"
    config = config_dir / "same-name.dat"
    document.write_text("fixture", encoding="utf-8")
    config.write_text("{}", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    adapter = V105RuleAdapter(source_root=source_root, input_source_roots=(inputs,))

    result = adapter.run(
        AdapterContext(
            work_dir=work,
            profile_snapshot={
                "routes": ["rule_text", "rule_table"],
                "source_document": str(document),
                "config": str(config),
                "python_executable": sys.executable,
            },
        ),
        lambda event: None,
    )

    assert result.manifest_path == work / "legacy_v105" / "book_engine_run_report.json"
    assert result.metrics["rows"] == 3
    stage_root = next(work.glob(".stage-*"))
    assert (stage_root / "inputs" / "document" / "same-name.dat").exists()
    assert (stage_root / "inputs" / "config" / "same-name.dat").exists()
    assert (stage_root / "staged-engine" / "v105" / "book_engine" / "cli.py").exists()


def test_v105_adapter_discards_stage_when_sealing_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "fake-v105"
    entry = source_root / "model" / "src" / "book_engine" / "cli.py"
    entry.parent.mkdir(parents=True)
    entry.write_text("pass\n", encoding="utf-8")
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    document = inputs / "document.md"
    config = inputs / "config.json"
    document.write_text("fixture", encoding="utf-8")
    config.write_text("{}", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()

    def fail_seal(self: ProcessRunner) -> None:
        raise ProcessIsolationError("stage integrity verification failed")

    monkeypatch.setattr(ProcessRunner, "seal_staging", fail_seal)
    adapter = V105RuleAdapter(source_root=source_root, input_source_roots=(inputs,))

    with pytest.raises(ProcessIsolationError, match="stage integrity"):
        adapter.run(
            AdapterContext(
                work_dir=work,
                profile_snapshot={
                    "routes": ["rule_text", "rule_table"],
                    "source_document": str(document),
                    "config": str(config),
                },
            ),
            lambda event: None,
        )

    assert not list(work.glob(".stage-*"))


def test_v105_adapter_reports_unconfirmed_stage_cleanup_as_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "fake-v105"
    entry = source_root / "model" / "src" / "book_engine" / "cli.py"
    entry.parent.mkdir(parents=True)
    entry.write_text("pass\n", encoding="utf-8")
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    document = inputs / "document.md"
    config = inputs / "config.json"
    document.write_text("fixture", encoding="utf-8")
    config.write_text("{}", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    real_discard = ProcessRunner.discard_stage

    def fail_seal(self: ProcessRunner) -> None:
        raise ProcessIsolationError("sensitive source path")

    def discard_but_report_failure(self: ProcessRunner) -> bool:
        real_discard(self)
        return False

    monkeypatch.setattr(ProcessRunner, "seal_staging", fail_seal)
    monkeypatch.setattr(ProcessRunner, "discard_stage", discard_but_report_failure)
    adapter = V105RuleAdapter(source_root=source_root, input_source_roots=(inputs,))

    with pytest.raises(StageCleanupError) as caught:
        adapter.run(
            AdapterContext(
                work_dir=work,
                profile_snapshot={
                    "routes": ["rule_text", "rule_table"],
                    "source_document": str(document),
                    "config": str(config),
                },
            ),
            lambda event: None,
        )

    assert str(caught.value) == "stage cleanup could not be confirmed"
    assert caught.value.failure_kind == "retryable"
    assert caught.value.__cause__ is None
    assert "sensitive" not in str(caught.value)
    assert not list(work.glob(".stage-*"))


def test_v106_adapter_runs_fake_hybrid_engine_and_parses_batch_report(tmp_path: Path) -> None:
    source_root = tmp_path / "fake-v106"
    entry = source_root / "model" / "src" / "book_engine" / "llm_v106" / "pipeline.py"
    entry.parent.mkdir(parents=True)
    (entry.parent.parent / "__init__.py").write_text("", encoding="utf-8")
    (entry.parent / "__init__.py").write_text("", encoding="utf-8")
    entry.write_text(
        "import argparse,json,os,pathlib\n"
        "p=argparse.ArgumentParser(); "
        "[p.add_argument(x) for x in ('--books-dir','--v105-root','--output-root','--property-ontology','--cache-dir')]\n"
        "p.add_argument('--plan-only',action='store_true'); a=p.parse_args(); out=pathlib.Path(a.output_root); out.mkdir(parents=True,exist_ok=True)\n"
        "root=pathlib.Path.cwd().resolve()\n"
        "assert all(pathlib.Path(v).resolve().is_relative_to(root) for v in (a.books_dir,a.v105_root,a.output_root,a.property_ontology,a.cache_dir,os.environ['PYTHONPATH']))\n"
        "assert any(p.name == 'staged-engine' for p in pathlib.Path(__file__).resolve().parents)\n"
        "assert any(p.name.startswith('.stage-') for p in pathlib.Path(__file__).resolve().parents)\n"
        "(out/'v106_qwen3_max_batch_report.json').write_text(json.dumps({'ok':True,'status':'planned','final_rows':0,'llm_error_jobs':0}))\n",
        encoding="utf-8",
    )
    inputs = tmp_path / "readonly-inputs"
    books = inputs / "books"
    v105 = inputs / "v105"
    books.mkdir(parents=True)
    v105.mkdir()
    ontology = inputs / "ontology.tsv"
    ontology.write_text("property\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    adapter = V106HybridAdapter(source_root=source_root, input_source_roots=(inputs,))

    result = adapter.run(
        AdapterContext(
            work_dir=work,
            profile_snapshot={
                "routes": ["rule_text", "rule_table", "llm_text", "llm_table"],
                "books_dir": str(books),
                "v105_root": str(v105),
                "property_ontology": str(ontology),
                "python_executable": sys.executable,
                "plan_only": True,
            },
        ),
        lambda event: None,
    )

    assert result.manifest_path == work / "legacy_v106" / "v106_qwen3_max_batch_report.json"
    assert result.metrics["llm_error_jobs"] == 0
    stage_root = next(work.glob(".stage-*"))
    assert (stage_root / "inputs" / "ontology" / "ontology.tsv").exists()
    assert (stage_root / "inputs" / "v105-root").is_dir()
    assert (stage_root / "staged-engine" / "v106" / "book_engine" / "llm_v106" / "pipeline.py").exists()


def _script_tsv(tmp_path: Path, rows: list[dict[str, str]]) -> Path:
    from app.facts.script_schema59 import SCRIPT_SCHEMA59_COLUMNS

    path = tmp_path / "shared.tsv"
    lines = ["\t".join(SCRIPT_SCHEMA59_COLUMNS)]
    for row in rows:
        lines.append("\t".join(row.get(column, "") for column in SCRIPT_SCHEMA59_COLUMNS))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_v105_route_scope_filters_combined_output_by_source_kind(tmp_path: Path) -> None:
    from app.extraction.contracts import AdapterContext

    source = _script_tsv(tmp_path, [
        {"fact_id": "F1", "graph_fact_key": "a:mass", "主体名称": "梯恩梯", "来源类型": "文本",
         "attribute_name": "密度", "数值": "1.65", "证据文本": "梯恩梯密度为1.65"},
        {"fact_id": "F2", "graph_fact_key": "b:mp", "主体名称": "梯恩梯", "来源类型": "表格",
         "所属表格ID": "table-1", "attribute_name": "熔点", "数值": "80.5", "证据文本": "熔点 80.5"},
    ])
    work = tmp_path / "work"
    work.mkdir()
    base = V105RuleAdapter(source_root=tmp_path)

    text_adapter = base.for_route("rule_text")
    table_adapter = base.for_route("rule_table")
    text_result = text_adapter.run(
        AdapterContext(work_dir=work, shared_source_tsv=source), lambda event: None
    )
    table_result = table_adapter.run(
        AdapterContext(work_dir=work / "t2", shared_source_tsv=source), lambda event: None
    )

    assert text_result.metrics["records"] == 1
    assert text_result.metrics["reused_shared_output"] == 1
    assert table_result.metrics["records"] == 1
    manifest = json.loads((work / "rule_text" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["route"] == "rule_text"
    assert "shared_source_tsv" not in manifest


def test_v105_for_route_rejects_unknown_route(tmp_path: Path) -> None:
    from app.extraction.contracts import PermanentAdapterError

    adapter = V105RuleAdapter(source_root=tmp_path)
    with pytest.raises(PermanentAdapterError):
        adapter.for_route("llm_text")


def test_v105_executor_route_includes_script_rows_metric(tmp_path: Path) -> None:
    # for_route without shared TSV still runs the engine; covered by existing
    # fake-engine tests.  Here only the shared-filter metric contract is checked.
    source = _script_tsv(tmp_path, [
        {"fact_id": "F1", "graph_fact_key": "a:mass", "主体名称": "A", "来源类型": "表格",
         "所属表格ID": "t1", "attribute_name": "p", "数值": "1", "证据文本": "A p 1"},
    ])
    work = tmp_path / "w"
    work.mkdir()
    result = V105RuleAdapter(source_root=tmp_path, route="rule_table").run(
        AdapterContext(work_dir=work, shared_source_tsv=source), lambda event: None
    )
    assert result.metrics == {"records": 1, "source_rows": 1, "reused_shared_output": 1}
