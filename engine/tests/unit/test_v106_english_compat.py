from __future__ import annotations

from pathlib import Path

from book_engine.llm_v106.client import QwenResponse
from book_engine.llm_v106.english_aliases import extract_explicit_abbreviation_pairs
from book_engine.llm_v106.language_compat import detect_language
from book_engine.llm_v106.pipeline import BookInput, process_book
from book_engine.llm_v106.process_flow import ProcessSpan, detect_process_spans
from book_engine.llm_v106.process_graph import compile_process_graphs, validate_process_response
from book_engine.llm_v106.prompts import direct_extraction_payload, direct_extraction_system
from book_engine.llm_v106.schema59 import PropertyOntologyIndex, apply_llm_fact, subject_is_valid, validate_rows


def _ontology() -> PropertyOntologyIndex:
    return PropertyOntologyIndex(
        [
            {
                "canonical_name": "密度",
                "alias": "密度",
                "attribute_category": "物理性质",
                "property_id": "prop:density",
            },
            {
                "canonical_name": "粒径",
                "alias": "粒径",
                "attribute_category": "形态性质",
                "property_id": "prop:particle-size",
            },
            {
                "canonical_name": "别名",
                "alias": "别名",
                "attribute_category": "命名信息",
                "property_id": "prop:alias",
            },
            {
                "canonical_name": "操作步骤",
                "alias": "操作步骤",
                "attribute_category": "方法/工艺",
                "property_id": "prop:process-step",
            },
        ],
        english_alias_rows=[
            {"alias": "density", "canonical_name": "密度"},
            {"alias": "particle size", "canonical_name": "粒径"},
        ],
    )


def test_language_detection_is_per_unit_and_handles_mixed_scientific_text() -> None:
    assert detect_language("推进剂的燃速随压力升高而增加。").language == "zh"
    assert detect_language("The burning rate increases with chamber pressure.").language == "en"
    assert detect_language("HTPB推进剂 uses ammonium perchlorate as the oxidizer and 铝粉 as fuel.").language == "mixed"


def test_explicit_english_full_name_abbreviation_pairs_preserve_source_text() -> None:
    pairs = extract_explicit_abbreviation_pairs(
        "Ammonium perchlorate (AP) was blended with hydroxyl-terminated polybutadiene, HTPB. "
        "RDX (cyclotrimethylenetrinitramine) was also tested."
    )
    values = {(item.full_name, item.abbreviation) for item in pairs}
    assert ("Ammonium perchlorate", "AP") in values
    assert ("hydroxyl-terminated polybutadiene", "HTPB") in values
    assert ("cyclotrimethylenetrinitramine", "RDX") in values


def test_english_property_alias_maps_to_existing_chinese_ontology_only() -> None:
    ontology = _ontology()
    mapped = ontology.resolve("Particle-size", language="en")
    assert mapped.mapped
    assert mapped.canonical_name == "粒径"
    unknown = ontology.resolve("crystal habit score", language="en")
    assert not unknown.mapped
    assert unknown.canonical_name == "crystal habit score"


def test_english_subject_validation_rejects_fragments_without_changing_chinese_rules() -> None:
    assert subject_is_valid("ammonium perchlorate", language="en") == (True, "ok")
    assert subject_is_valid("it", language="en")[0] is False
    assert subject_is_valid("the propellant depends on", language="en")[0] is False
    assert subject_is_valid("this result", language="en")[0] is False
    assert subject_is_valid("复合推进剂", language="zh") == (True, "ok")
    assert subject_is_valid("燃速不", language="zh")[0] is False


def test_english_prompt_adds_language_rules_but_chinese_payload_remains_unchanged() -> None:
    common = dict(
        unit_kind="text_window",
        unit_id="u:test",
        book_title="Test Book",
        heading_path="Chapter 1",
        source_locator="L1-L5",
        evidence="Ammonium perchlorate has a density of 1.95 g/cm3.",
        property_names=["密度", "粒径"],
        max_facts=10,
    )
    zh = direct_extraction_payload(**common, language="zh")
    en = direct_extraction_payload(**common, language="en")
    assert "source_language" not in zh
    assert en["source_language"] == "en"
    assert any("保持英文原文" in item for item in en["constraints"])
    assert any("respectively" in item for item in en["constraints"])
    assert "English" not in direct_extraction_system(language="zh")
    assert "English" in direct_extraction_system(language="en")


def test_apply_llm_fact_releases_mapped_english_property_and_reviews_unmapped_property() -> None:
    ontology = _ontology()
    base = {
        "subject": "ammonium perchlorate",
        "subject_type": "材料",
        "relation_type": "属性",
        "value": "1.95",
        "unit": "g/cm3",
        "condition": "",
        "polarity": "肯定",
        "subject_span": "ammonium perchlorate",
        "value_span": "1.95",
        "confidence": 0.94,
    }
    row, reasons = apply_llm_fact(
        fact={**base, "property": "density"},
        ontology=ontology,
        evidence="ammonium perchlorate has a density of 1.95 g/cm3.",
        book_title="Test Book",
        document_id="doc:test",
        heading_path="Chapter 1",
        source_locator="L1-L1",
        source_type="llm_text_window_direct",
        language="en",
    )
    assert not reasons
    assert row is not None and row["attribute_name"] == "密度"

    review_row, review_reasons = apply_llm_fact(
        fact={**base, "property": "crystal habit score"},
        ontology=ontology,
        evidence="ammonium perchlorate has a crystal habit score of 1.95.",
        book_title="Test Book",
        document_id="doc:test",
        heading_path="Chapter 1",
        source_locator="L2-L2",
        source_type="llm_text_window_direct",
        language="en",
    )
    assert review_row is not None
    assert "english_property_unmapped" in review_reasons
    assert review_row["attribute_name"] == "crystal habit score"


def test_english_process_detector_finds_numbered_procedure_and_rejects_article_outline(tmp_path: Path) -> None:
    source = tmp_path / "English Process Book.md"
    source.write_text(
        "# Chapter 1\n\n"
        "First, this chapter introduces propellant chemistry. Then it discusses performance. Finally it summarizes prior work.\n\n"
        "## Preparation procedure\n\n"
        "The preparation procedure is as follows:\n"
        "Step 1. Weigh ammonium perchlorate and HTPB.\n"
        "Step 2. Add the ingredients to the mixer.\n"
        "Step 3. Stir the mixture for 20 min and then cure it at 60 °C.\n",
        encoding="utf-8",
    )
    spans = detect_process_spans(source)
    assert len(spans) == 1
    assert spans[0].language == "en"
    assert spans[0].expected_labels == (1, 2, 3)
    assert spans[0].process_type_hint == "制备工艺"
    assert "Preparation procedure" in " > ".join(spans[0].heading_path)


def test_english_process_table_is_detected_as_one_complete_span(tmp_path: Path) -> None:
    source = tmp_path / "English Table Book.md"
    source.write_text(
        "# Test procedure\n\n"
        "| Step | Operation | Temperature | Time |\n"
        "|---|---|---|---|\n"
        "| 1 | Add the sample to the vessel | 25 °C | 5 min |\n"
        "| 2 | Stir the mixture | 25 °C | 10 min |\n"
        "| 3 | Heat the mixture | 80 °C | 30 min |\n",
        encoding="utf-8",
    )
    spans = detect_process_spans(source)
    assert len(spans) == 1
    assert spans[0].kind == "process_table"
    assert spans[0].language == "en"
    assert spans[0].expected_labels == (1, 2, 3)
    assert spans[0].process_type_hint == "试验流程"


def test_english_process_response_compiles_to_chinese_ontology_and_english_step_text() -> None:
    evidence = (
        "The preparation procedure is as follows. "
        "Step 1. Weigh ammonium perchlorate. "
        "Step 2. Add it to the mixer. "
        "Step 3. Stir the mixture for 20 min."
    )
    span = ProcessSpan(
        span_id="process_span:english",
        kind="process_text",
        book_title="English Book",
        heading_path=("Chapter 1", "Preparation procedure"),
        line_start=1,
        line_end=4,
        text=evidence,
        expected_labels=(1, 2, 3),
        process_type_hint="制备工艺",
        language="en",
    )
    response = {
        "processes": [
            {
                "process_name": "AP mixing procedure",
                "process_type": "制备工艺",
                "process_object": "AP mixture",
                "process_object_type": "样品/产品",
                "process_evidence_span": "The preparation procedure is as follows",
                "confidence": 0.93,
                "steps": [
                    {
                        "source_step_label": "Step 1",
                        "step_index": 1,
                        "action": "Weigh",
                        "action_span": "Weigh",
                        "object": "ammonium perchlorate",
                        "object_span": "ammonium perchlorate",
                        "materials": ["ammonium perchlorate"],
                        "equipment": [],
                        "condition": "",
                        "result": "",
                        "step_evidence": "Step 1. Weigh ammonium perchlorate.",
                    },
                    {
                        "source_step_label": "Step 2",
                        "step_index": 2,
                        "action": "Add",
                        "action_span": "Add",
                        "object": "it",
                        "object_span": "it",
                        "materials": [],
                        "equipment": ["mixer"],
                        "condition": "",
                        "result": "",
                        "step_evidence": "Step 2. Add it to the mixer.",
                    },
                    {
                        "source_step_label": "Step 3",
                        "step_index": 3,
                        "action": "Stir",
                        "action_span": "Stir",
                        "object": "the mixture",
                        "object_span": "the mixture",
                        "materials": [],
                        "equipment": [],
                        "condition": "for 20 min",
                        "result": "",
                        "step_evidence": "Step 3. Stir the mixture for 20 min.",
                    },
                ],
            }
        ]
    }
    validated = validate_process_response(response, span=span)
    assert not validated.errors
    rows, graphs = compile_process_graphs(
        validated.valid_processes,
        span=span,
        ontology=_ontology(),
        document_id="doc:english",
        heading_path="Chapter 1 > Preparation procedure",
        source_locator="L1-L4",
    )
    assert len(rows) == 3 and len(graphs) == 1
    assert rows[0]["process_type"] == "制备工艺"
    assert rows[0]["step_action"] == "Weigh"
    assert rows[0]["step_object"] == "ammonium perchlorate"
    assert validate_rows(rows)["ok"]


def test_end_to_end_adds_explicit_abbreviation_fact_without_translating_subject(tmp_path: Path) -> None:
    source = tmp_path / "English Book.md"
    source.write_text(
        "# Materials\n\nAmmonium perchlorate (AP) is commonly used as an oxidizer in composite propellants.\n",
        encoding="utf-8",
    )
    v105 = tmp_path / "v105" / "English Book"
    v105.mkdir(parents=True)
    output = tmp_path / "out"

    class MockClient:
        def chat_json(self, *, system, user, max_tokens, request_tag):  # noqa: ANN001
            task = user.get("task") if isinstance(user, dict) else ""
            data = {"processes": []} if task.startswith("complete_process") else {"facts": [], "decisions": []}
            return QwenResponse(data=data, raw_content="{}", request_id="mock", usage={}, cached=False, attempts=1)

    report = process_book(
        BookInput("English Book", source, v105, "doc:english"),
        output_root=output,
        ontology=_ontology(),
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
    final_path = output / "English Book" / "step_graph_guard" / "graph_import_ready.tsv"
    rows = final_path.read_text(encoding="utf-8-sig").splitlines()
    assert any("Ammonium perchlorate" in line and "\tAP\t" in line and "命名关系事实" in line for line in rows[1:])


def test_english_process_validator_rejects_discourse_actions_and_unresolved_previous_step() -> None:
    evidence = "Step 1. Add AP. Step 2. Stir the mixture after the previous step."
    span = ProcessSpan(
        span_id="process_span:bad-english",
        kind="process_text",
        book_title="English Book",
        heading_path=("Procedure",),
        line_start=1,
        line_end=2,
        text=evidence,
        expected_labels=(1, 2),
        process_type_hint="制备工艺",
        language="en",
    )
    response = {
        "processes": [
            {
                "process_name": "AP procedure",
                "process_type": "制备工艺",
                "process_object": "AP mixture",
                "process_object_type": "样品/产品",
                "process_evidence_span": "",
                "confidence": 0.9,
                "steps": [
                    {
                        "source_step_label": "Step 1",
                        "step_index": 1,
                        "action": "Then",
                        "action_span": "Step 1",
                        "object": "AP",
                        "object_span": "AP",
                        "materials": [],
                        "equipment": [],
                        "condition": "",
                        "result": "",
                        "step_evidence": "Step 1. Add AP.",
                    },
                    {
                        "source_step_label": "Step 2",
                        "step_index": 2,
                        "action": "Stir",
                        "action_span": "Stir",
                        "object": "the mixture",
                        "object_span": "the mixture",
                        "materials": [],
                        "equipment": [],
                        "condition": "after the previous step",
                        "result": "",
                        "step_evidence": "Step 2. Stir the mixture after the previous step.",
                    },
                ],
            }
        ]
    }
    result = validate_process_response(response, span=span)
    assert not result.valid_processes
    assert any("invalid_action" in error for error in result.errors)
    assert any("unresolved_relative_reference" in error for error in result.errors)


def test_english_compat_config_is_enabled_without_changing_runtime_contract() -> None:
    import json

    config_path = Path(__file__).resolve().parents[2] / "src" / "config" / "llm_v106_qwen3_max.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    compat = config["english_compatibility"]
    assert config["revision"] == "v106_english_compat"
    assert compat["enabled"] is True
    assert compat["language_detection_scope"] == "per_unit"
    assert compat["preserve_source_language_fields"] is True
    assert compat["ontology_label_language"] == "zh-CN"
    assert compat["unmapped_property_policy"] == "manual_review"
    assert compat["chinese_regression_policy"] == "unchanged"
    assert config["output_contract"]["columns"] == 59


def test_strict_59_column_writer_escapes_embedded_newlines_and_tabs(tmp_path: Path) -> None:
    from book_engine.llm_v106.schema59 import SCHEMA59_COLUMNS, write_tsv

    row = {name: "" for name in SCHEMA59_COLUMNS}
    row["fact_id"] = "fact:test"
    row["证据文本"] = "Line 1\nLine 2\tcell"
    path = tmp_path / "out.tsv"
    write_tsv(path, [row])
    physical = path.read_bytes().splitlines()
    assert len(physical) == 2
    assert physical[1].count(b"\t") == 58
    text = path.read_text(encoding="utf-8-sig")
    assert r"Line 1\nLine 2\tcell" in text


def test_offline_english_compat_validator_passes_without_api_calls(tmp_path: Path) -> None:
    import json
    import subprocess
    import sys

    project_root = Path(__file__).resolve().parents[2]
    script = project_root / "scripts" / "validate_v106_english_compat.py"
    report = tmp_path / "validation.json"
    completed = subprocess.run(
        [sys.executable, str(script), "--project-root", str(project_root), "--report", str(report)],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["network_used"] is False
    assert payload["checks"]["schema59_single_line"] is True
    assert payload["checks"]["chinese_prompt_regression"] is True
