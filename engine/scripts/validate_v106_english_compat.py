#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any


def _project_root_from_script() -> Path:
    return Path(__file__).resolve().parents[1]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offline validation for v106 English compatibility layer")
    parser.add_argument("--project-root", type=Path, default=_project_root_from_script())
    parser.add_argument("--report", type=Path, default=None)
    return parser.parse_args()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    args = _parse_args()
    root = args.project_root.resolve()
    src = root / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))

    from book_engine.llm_v106.english_aliases import extract_explicit_abbreviation_pairs
    from book_engine.llm_v106.language_compat import detect_language
    from book_engine.llm_v106.process_flow import detect_process_spans
    from book_engine.llm_v106.prompts import direct_extraction_payload, direct_extraction_system
    from book_engine.llm_v106.schema59 import (
        PropertyOntologyIndex,
        SCHEMA59_COLUMNS,
        subject_is_valid,
        write_tsv,
    )

    checks: dict[str, bool] = {}
    details: dict[str, Any] = {}
    config_dir = root / "src" / "config"

    config = json.loads((config_dir / "llm_v106_qwen3_max.json").read_text(encoding="utf-8"))
    compat = config.get("english_compatibility", {})
    _require(config.get("revision") == "v106_english_compat", "revision mismatch")
    _require(compat.get("enabled") is True, "english compatibility disabled")
    _require(compat.get("language_detection_scope") == "per_unit", "language detection must be per_unit")
    _require(compat.get("chinese_regression_policy") == "unchanged", "Chinese regression policy mismatch")
    _require(config.get("output_contract", {}).get("columns") == 59, "schema column count changed")
    _require(compat.get("runtime_environment_variables") == ["OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL"], "runtime environment contract changed")
    checks["config_contract"] = True

    ontology = PropertyOntologyIndex.from_tsv(config_dir / "property_ontology_v2.tsv")
    alias_report = ontology.validation_report()
    _require(alias_report["ok"], f"invalid English property aliases: {alias_report['invalid_english_aliases'][:5]}")
    _require(alias_report["english_alias_count"] >= 20, "English property alias coverage too small")
    _require(ontology.resolve("particle size", language="en").canonical_name == "粒径", "particle size mapping failed")
    _require(not ontology.resolve("unregistered crystal morphology score", language="en").mapped, "unknown English property was auto-created")
    checks["ontology_alias_overlay"] = True
    details["english_alias_count"] = alias_report["english_alias_count"]

    _require(detect_language("推进剂的燃速随压力升高而增加。").language == "zh", "Chinese detection failed")
    _require(detect_language("The burning rate increases with chamber pressure.").language == "en", "English detection failed")
    _require(detect_language("HTPB推进剂 uses ammonium perchlorate as the oxidizer and 铝粉 as fuel.").language == "mixed", "mixed detection failed")
    checks["per_unit_language_detection"] = True

    common = {
        "unit_kind": "text_window",
        "unit_id": "validation:1",
        "book_title": "Validation Book",
        "heading_path": "Chapter 1",
        "source_locator": "L1-L2",
        "evidence": "Ammonium perchlorate has a density of 1.95 g/cm3.",
        "property_names": ["密度", "粒径"],
        "max_facts": 8,
    }
    zh_payload = direct_extraction_payload(**common, language="zh")
    en_payload = direct_extraction_payload(**common, language="en")
    _require("source_language" not in zh_payload, "Chinese prompt gained English-only payload fields")
    _require("english_property_alias_examples" not in zh_payload, "Chinese prompt gained English alias examples")
    _require("English" not in direct_extraction_system(language="zh"), "Chinese system prompt changed to English mode")
    checks["chinese_prompt_regression"] = True
    _require(en_payload.get("source_language") == "en", "English prompt language missing")
    _require(any("保持英文原文" in item for item in en_payload.get("constraints", [])), "English source-preservation constraint missing")
    _require(any("respectively" in item for item in en_payload.get("constraints", [])), "English respectively constraint missing")
    _require("English" in direct_extraction_system(language="en"), "English system prompt adaptation missing")
    checks["english_prompt_constraints"] = True

    pairs = extract_explicit_abbreviation_pairs(
        "Ammonium perchlorate (AP) was mixed with hydroxyl-terminated polybutadiene, HTPB."
    )
    values = {(item.full_name, item.abbreviation) for item in pairs}
    _require(("Ammonium perchlorate", "AP") in values, "full-name abbreviation extraction failed for AP")
    _require(("hydroxyl-terminated polybutadiene", "HTPB") in values, "full-name abbreviation extraction failed for HTPB")
    checks["explicit_abbreviation_binding"] = True

    _require(subject_is_valid("ammonium perchlorate", language="en")[0], "valid English subject rejected")
    _require(not subject_is_valid("it", language="en")[0], "English pronoun subject accepted")
    _require(not subject_is_valid("the propellant depends on", language="en")[0], "English relation fragment accepted")
    checks["english_subject_validation"] = True

    with tempfile.TemporaryDirectory(prefix="v106_english_compat_validate_") as temp_name:
        temp = Path(temp_name)
        process_book = temp / "English Process.md"
        process_book.write_text(
            "# Preparation procedure\n\n"
            "The preparation procedure is as follows:\n"
            "Step 1. Weigh ammonium perchlorate and HTPB.\n"
            "Step 2. Add the ingredients to the mixer.\n"
            "Step 3. Stir for 20 min and cure at 60 °C.\n",
            encoding="utf-8",
        )
        spans = detect_process_spans(process_book)
        _require(len(spans) == 1, f"English process span count mismatch: {len(spans)}")
        _require(spans[0].language == "en", "English process span language mismatch")
        _require(spans[0].expected_labels == (1, 2, 3), "English process step labels incomplete")
        checks["english_process_detection"] = True

        table_book = temp / "English Table.md"
        table_book.write_text(
            "# Test procedure\n\n"
            "| Step | Operation | Temperature | Time |\n"
            "|---|---|---|---|\n"
            "| 1 | Add the sample | 25 °C | 5 min |\n"
            "| 2 | Stir the mixture | 25 °C | 10 min |\n"
            "| 3 | Heat the mixture | 80 °C | 30 min |\n",
            encoding="utf-8",
        )
        table_spans = detect_process_spans(table_book)
        _require(any(item.kind == "process_table" and item.language == "en" for item in table_spans), "English process table not detected")
        checks["english_process_table_detection"] = True

        row = {name: "" for name in SCHEMA59_COLUMNS}
        row["fact_id"] = "fact:validation"
        row["证据文本"] = "Line 1\nLine 2\tcell"
        out = temp / "strict59.tsv"
        write_tsv(out, [row])
        physical = out.read_bytes().splitlines()
        _require(len(SCHEMA59_COLUMNS) == 59, "schema column count is not 59")
        _require(len(physical) == 2, "TSV contains embedded physical line breaks")
        _require(all(line.count(b"\t") == 58 for line in physical), "TSV does not contain exactly 58 delimiters per line")
        checks["schema59_single_line"] = True

    payload = {
        "ok": all(checks.values()),
        "revision": config.get("revision"),
        "network_used": False,
        "api_key_required": False,
        "checks": checks,
        "details": details,
    }
    report_path = (args.report or (root / "v106_english_compat_validation_report.json")).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"[REPORT] {report_path}")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
