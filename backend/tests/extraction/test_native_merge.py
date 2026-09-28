from __future__ import annotations

import csv
import json
from itertools import combinations
from pathlib import Path
from uuid import uuid4

import pytest

from app.extraction.contracts import AdapterContext, AdapterResult, PermanentAdapterError
from app.extraction.native.common import FIELD, SCHEMA59_COLUMNS, write_route_output
from app.extraction.native.merge import MergeAdapter, merge_route_outputs


ROUTES = ("rule_text", "rule_table", "llm_text", "llm_table")


def _result(tmp_path: Path, route: str, rows: list[dict[str, str]]) -> AdapterResult:
    context = AdapterContext(work_dir=tmp_path, execution_idempotency_key="merge-key")
    return write_route_output(context, route, rows, lambda _event: None)


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def test_all_15_non_empty_route_combinations_merge(tmp_path: Path):
    results = {
        route: _result(tmp_path / route, route, [{"graph_fact_key": f"key-{route}", "subject": route, "property": "p", "value": "v", "evidence_text": f"evidence {route}"}])
        for route in ROUTES
    }
    seen = 0
    for size in range(1, 5):
        for selected in combinations(ROUTES, size):
            summary = merge_route_outputs([results[name] for name in selected], tmp_path / f"merged-{seen}.tsv")
            assert summary.input_routes == size
            assert summary.output_rows == size
            seen += 1
    assert seen == 15


def test_merge_deduplicates_graph_key_then_canonical_evidence_hash(tmp_path: Path):
    first = _result(tmp_path / "a", "rule_text", [{"graph_fact_key": "same", "subject": "A", "property": "p", "value": "1", "evidence_text": "  Evidence A  "}])
    second = _result(tmp_path / "b", "llm_text", [
        {"graph_fact_key": "same", "subject": "A", "property": "p", "value": "1", "evidence_text": "Evidence A"},
        {"graph_fact_key": "", "subject": "A", "property": "p", "value": "1", "evidence_text": "evidence   a"},
    ])
    output = tmp_path / "merged.tsv"
    summary = merge_route_outputs([first, second], output)
    rows = _read(output)
    assert summary.output_rows == 1
    assert rows[0][FIELD["route"]] == "llm_text,rule_text"
    assert rows[0][FIELD["evidence_text"]] == "Evidence A"


def test_conflicting_values_are_both_preserved_for_candidate_review(tmp_path: Path):
    one = _result(tmp_path / "one", "rule_text", [{"graph_fact_key": "A:p", "subject": "A", "property": "p", "value": "1", "evidence_text": "one"}])
    two = _result(tmp_path / "two", "llm_table", [{"graph_fact_key": "A:p", "subject": "A", "property": "p", "value": "2", "evidence_text": "two"}])
    output = tmp_path / "merged.tsv"
    merge_route_outputs([one, two], output)
    rows = _read(output)
    assert [row[FIELD["value"]] for row in rows] == ["1", "2"]
    assert {row[FIELD["review_status"]] for row in rows} == {"candidate_review"}
    assert len({row[FIELD["conflict_group"]] for row in rows}) == 1


def test_graph_dedup_preserves_distinct_evidence_from_all_routes(tmp_path: Path):
    one = _result(tmp_path / "one-evidence", "rule_text", [{"graph_fact_key": "A:p", "subject": "A", "property": "p", "value": "1", "evidence_text": "first source passage"}])
    two = _result(tmp_path / "two-evidence", "llm_text", [{"graph_fact_key": "A:p", "subject": "A", "property": "p", "value": "1", "evidence_text": "second source passage"}])
    output = tmp_path / "evidence.tsv"

    merge_route_outputs([one, two], output)

    row = _read(output)[0]
    assert row[FIELD["route"]] == "llm_text,rule_text"
    provenance = __import__("json").loads(row[FIELD["provenance"]])
    assert {item["evidence"] for item in provenance} == {"first source passage", "second source passage"}


def test_merge_is_deterministic_across_input_order_and_repeated_runs(tmp_path: Path):
    results = [_result(tmp_path / route, route, [{"graph_fact_key": route, "subject": route, "property": "p", "value": "1", "evidence_text": route}]) for route in ROUTES]
    one, two = tmp_path / "one.tsv", tmp_path / "two.tsv"
    merge_route_outputs(results, one)
    merge_route_outputs(list(reversed(results)), two)
    assert one.read_bytes() == two.read_bytes()


@pytest.mark.parametrize("case", ["empty", "bad-header", "traversal", "oversized"])
def test_merge_rejects_empty_and_hostile_inputs_with_fixed_error(tmp_path: Path, case: str):
    if case == "empty":
        results = []
    else:
        source = tmp_path / case
        source.mkdir()
        manifest = source / "manifest.json"
        if case == "bad-header":
            candidate = source / "bad.tsv"
            candidate.write_text("wrong\nvalue\n", encoding="utf-8")
            manifest.write_text('{"route":"rule_text","candidate_tsv":"bad.tsv"}', encoding="utf-8")
        elif case == "traversal":
            manifest.write_text('{"route":"rule_text","candidate_tsv":"../outside.tsv"}', encoding="utf-8")
        else:
            candidate = source / "large.tsv"
            candidate.write_text("x" * 2_100_000, encoding="utf-8")
            manifest.write_text('{"route":"rule_text","candidate_tsv":"large.tsv"}', encoding="utf-8")
        results = [AdapterResult(manifest_path=manifest, metrics={"records": 1})]
    with pytest.raises(PermanentAdapterError, match="native merge input is invalid") as raised:
        merge_route_outputs(results, tmp_path / "out.tsv")
    assert raised.value.__cause__ is None


def test_merge_adapter_remains_registry_compatible(tmp_path: Path):
    work = tmp_path / "merge"
    route = _result(work / "rebuilt", "rule_text", [{"graph_fact_key": "k", "subject": "A", "property": "p", "value": "v", "evidence_text": "e"}])
    context = AdapterContext(work_dir=work, route_result_manifests=(route.manifest_path,))
    result = MergeAdapter().run(context, lambda _event: None)
    assert result.manifest_path == context.work_dir / "merge" / "manifest.json"


def test_merge_adapter_rewrites_platform_identity_and_preserves_generator_provenance(tmp_path: Path):
    document_id = uuid4()
    generator_step = uuid4()
    source_context = AdapterContext(
        work_dir=tmp_path / "merge-work" / "rebuilt",
        step_id=generator_step,
        document_version_id=document_id,
        execution_idempotency_key="generator-execution",
    )
    route = write_route_output(source_context, "rule_text", [{"graph_fact_key":"a:p","subject":"A","property":"p","value":"1","evidence_text":"source evidence"}], lambda _event: None)
    merge_step = uuid4()
    context = AdapterContext(
        work_dir=tmp_path / "merge-work",
        step_id=merge_step,
        document_version_id=document_id,
        execution_idempotency_key="merge-execution",
        route_result_manifests=(route.manifest_path,),
    )

    result = MergeAdapter().run(context, lambda _event: None)
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    row = _read(result.manifest_path.parent / manifest["candidate_tsv"])[0]

    assert row[FIELD["route"]] == "merge"
    assert row[FIELD["extraction_source"]] == "merge"
    assert row[FIELD["step_id"]] == str(merge_step)
    assert row[FIELD["document_id"]] == str(document_id)
    assert row[FIELD["execution_idempotency_key"]] == "merge-execution"
    assert row["fact_id"] == __import__("app.extraction.native.common", fromlist=["candidate_fact_id"]).candidate_fact_id(row)
    provenance = json.loads(row[FIELD["provenance"]])
    assert provenance[0]["route"] == "rule_text"
    assert provenance[0]["step_id"] == str(generator_step)
    assert provenance[0]["execution_key"] == "generator-execution"


@pytest.mark.parametrize("mutation", ["extra-manifest", "record-count", "bad-provenance", "control-character"])
def test_merge_rejects_non_exact_payloads_and_untrusted_fields(tmp_path: Path, mutation: str):
    result = _result(tmp_path / mutation, "rule_text", [{"subject": "A", "property": "p", "value": "1", "evidence_text": "evidence"}])
    manifest_path = result.manifest_path
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    candidate = manifest_path.parent / manifest["candidate_tsv"]
    if mutation == "extra-manifest":
        manifest["unexpected"] = "payload"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif mutation == "record-count":
        manifest["records"] = 2
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    else:
        rows = _read(candidate)
        if mutation == "bad-provenance":
            rows[0][FIELD["provenance"]] = '[{"route":"llm_text"}]'
        else:
            rows[0][FIELD["subject"]] = "A\u0000hidden"
        with candidate.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=SCHEMA59_COLUMNS, delimiter="\t", lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
    with pytest.raises(PermanentAdapterError, match="native merge input is invalid"):
        merge_route_outputs([result], tmp_path / "rejected.tsv")


def test_merge_rejects_candidate_symlink(tmp_path: Path):
    result = _result(tmp_path / "linked", "rule_text", [{"subject": "A", "property": "p", "value": "1", "evidence_text": "evidence"}])
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    candidate = result.manifest_path.parent / manifest["candidate_tsv"]
    outside = tmp_path / "outside.tsv"
    candidate.replace(outside)
    try:
        candidate.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(PermanentAdapterError, match="native merge input is invalid"):
        merge_route_outputs([result], tmp_path / "rejected.tsv")


def test_merge_rejects_row_transplanted_from_another_step(tmp_path: Path):
    context = AdapterContext(
        work_dir=tmp_path / "source",
        step_id=__import__("uuid").uuid4(),
        document_version_id=__import__("uuid").uuid4(),
        execution_idempotency_key="expected-execution",
    )
    result = write_route_output(context, "rule_text", [{"subject":"A","property":"p","value":"1","evidence_text":"e"}], lambda _event: None)
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    candidate = result.manifest_path.parent / manifest["candidate_tsv"]
    rows = _read(candidate)
    transplanted_step = str(__import__("uuid").uuid4())
    rows[0][FIELD["step_id"]] = transplanted_step
    with candidate.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SCHEMA59_COLUMNS, delimiter="\t", lineterminator="\n")
        writer.writeheader(); writer.writerows(rows)
    manifest["step_id"] = transplanted_step
    result.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(PermanentAdapterError, match="native merge input is invalid"):
        merge_route_outputs([result], tmp_path / "rejected.tsv")
