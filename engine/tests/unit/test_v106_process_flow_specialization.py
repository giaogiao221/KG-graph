from __future__ import annotations

from pathlib import Path

from book_engine.llm_v106.process_flow import ProcessSpan, detect_process_spans
from book_engine.llm_v106.process_graph import (
    compile_process_graphs,
    validate_compiled_process_rows,
    validate_process_response,
)
from book_engine.llm_v106.schema59 import PropertyOntologyIndex, validate_rows


def span(text: str, labels: tuple[int, ...] = (1, 2, 3)) -> ProcessSpan:
    return ProcessSpan(
        span_id="process_span:test",
        kind="process_text",
        book_title="测试书",
        heading_path=("第1章", "制备方法"),
        line_start=10,
        line_end=20,
        text=text,
        expected_labels=labels,
        process_type_hint="制备工艺",
    )


def good_response() -> dict:
    return {
        "processes": [
            {
                "process_name": "ADN乳液结晶工艺",
                "process_type": "制备工艺",
                "process_object": "球形ADN颗粒",
                "process_object_type": "样品/产品",
                "process_evidence_span": "ADN乳液结晶工艺包括以下步骤",
                "confidence": 0.92,
                "steps": [
                    {
                        "source_step_label": "（1）",
                        "step_index": 1,
                        "action": "制备",
                        "action_span": "制备",
                        "object": "W/O型乳液",
                        "object_span": "W/O型乳液",
                        "materials": ["液态ADN", "石蜡油"],
                        "equipment": [],
                        "condition": "液态ADN为分散相，石蜡油为连续相",
                        "result": "形成ADN乳液",
                        "step_evidence": "（1）制备W/O型乳液，液态ADN为分散相，石蜡油为连续相，形成ADN乳液。",
                    },
                    {
                        "source_step_label": "（2）",
                        "step_index": 2,
                        "action": "冷却结晶",
                        "action_span": "冷却结晶",
                        "object": "ADN微滴",
                        "object_span": "ADN微滴",
                        "materials": [],
                        "equipment": [],
                        "condition": "加入晶种",
                        "result": "形成球形固体颗粒",
                        "step_evidence": "（2）加入晶种使ADN微滴冷却结晶，形成球形固体颗粒。",
                    },
                    {
                        "source_step_label": "（3）",
                        "step_index": 3,
                        "action": "分离洗涤并干燥",
                        "action_span": "分离、洗涤并干燥",
                        "object": "结晶产物",
                        "object_span": "结晶产物",
                        "materials": [],
                        "equipment": [],
                        "condition": "结晶完成后",
                        "result": "获得球形ADN产品",
                        "step_evidence": "（3）结晶完成后，将结晶产物分离、洗涤并干燥，获得球形ADN产品。",
                    },
                ],
            }
        ]
    }


def evidence() -> str:
    return (
        "ADN乳液结晶工艺包括以下步骤：\n"
        "（1）制备W/O型乳液，液态ADN为分散相，石蜡油为连续相，形成ADN乳液。\n"
        "（2）加入晶种使ADN微滴冷却结晶，形成球形固体颗粒。\n"
        "（3）结晶完成后，将结晶产物分离、洗涤并干燥，获得球形ADN产品。"
    )


def test_detector_finds_complete_process_and_ignores_article_outline(tmp_path: Path) -> None:
    source = tmp_path / "测试书.md"
    source.write_text(
        "# 第一章\n\n本章首先介绍材料分类，其次讨论性能，最后总结研究进展。\n\n"
        "## ADN乳液结晶工艺\n\n" + evidence() + "\n",
        encoding="utf-8",
    )
    spans = detect_process_spans(source)
    assert len(spans) == 1
    assert spans[0].expected_labels == (1, 2, 3)
    assert "W/O型乳液" in spans[0].text


def test_missing_numbered_step_is_rejected_for_repair() -> None:
    response = good_response()
    response["processes"][0]["steps"].pop(1)
    response["processes"][0]["steps"][1]["step_index"] = 2
    result = validate_process_response(response, span=span(evidence()))
    assert not result.valid_processes
    assert result.repair_recommended
    assert any("source_step_coverage_mismatch" in error for error in result.errors)


def test_unresolved_previous_step_reference_is_rejected() -> None:
    response = good_response()
    response["processes"][0]["steps"][1]["condition"] = "在上一步完成后"
    result = validate_process_response(response, span=span(evidence()))
    assert not result.valid_processes
    assert any("unresolved_relative_reference" in error for error in result.errors)


def test_valid_process_compiles_to_contiguous_schema59_graph() -> None:
    source_span = span(evidence())
    result = validate_process_response(good_response(), span=source_span)
    assert not result.errors
    ontology = PropertyOntologyIndex([])
    rows, graphs = compile_process_graphs(
        result.valid_processes,
        span=source_span,
        ontology=ontology,
        document_id="doc:test",
        heading_path="第1章 > 制备方法",
        source_locator="L10-L20",
    )
    assert len(rows) == 3
    assert len(graphs) == 1
    assert all(len(row) == 59 for row in rows)
    assert [row["step_index"] for row in rows] == ["1", "2", "3"]
    assert rows[0]["previous_step_id"] == ""
    assert rows[0]["next_step_id"] == rows[1]["step_id"]
    assert rows[1]["previous_step_id"] == rows[0]["step_id"]
    assert rows[2]["next_step_id"] == ""
    assert all(row["抽取来源"] == "qwen3_max_process_flow_specialized_v106p" for row in rows)
    assert validate_compiled_process_rows(rows)["ok"]
    assert validate_rows(rows)["ok"]


def test_process_book_end_to_end_uses_specialized_writer(tmp_path: Path) -> None:
    from book_engine.llm_v106.client import QwenResponse
    from book_engine.llm_v106.pipeline import BookInput, process_book

    source = tmp_path / "测试书.md"
    source.write_text("# 第一章\n\n" + evidence() + "\n", encoding="utf-8")
    v105 = tmp_path / "v105" / "测试书"
    v105.mkdir(parents=True)
    output = tmp_path / "out"

    class MockClient:
        def chat_json(self, *, system, user, max_tokens, request_tag):  # noqa: ANN001
            task = user.get("task") if isinstance(user, dict) else ""
            data = good_response() if task in {"complete_process_flow_extraction", "repair_incomplete_process_flow_once"} else {"facts": []}
            return QwenResponse(data=data, raw_content="{}", request_id="mock", usage={}, cached=False, attempts=1)

    report = process_book(
        BookInput("测试书", source, v105, "doc:test"),
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
    assert report["ok"]
    assert report["accepted_process_graphs"] == 1
    assert report["accepted_process_step_rows"] == 3
    assert report["generic_step_rows_deferred"] == 0
    final_path = output / "测试书" / "step_graph_guard" / "graph_import_ready.tsv"
    process_path = output / "测试书" / "step_graph_guard" / "graph_process_flow_ready.tsv"
    assert final_path.exists() and process_path.exists()
    assert report["process_graph_validation"]["ok"]
