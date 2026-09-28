#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Dict, List, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from book_engine.export.schema59_contract import load_contract, validate_graph_file  # noqa: E402
from book_engine.export.table_evidence_writer import unescape_single_line  # noqa: E402
from book_engine.routing.heading_subject_resolver import classify_heading_subject  # noqa: E402
from book_engine.routing.subject_name_normalizer import normalize_subject_name  # noqa: E402


_IDENTIFIER_ONLY_SUBJECT_RE = re.compile(
    r"^(?:\d+(?:[.．-]\d+)?|[一二三四五六七八九十百]+)(?:号|[#@*※①②③④⑤⑥⑦⑧⑨⑩])?$",
    re.IGNORECASE,
)
_BARE_SAMPLE_CODE_RE = re.compile(r"^[A-Fa-f]$")
_LEADING_ORPHAN_PUNCT_RE = re.compile(r"^[)）\]】}〉》、，,;；:：]+")
_EXPERIMENT_FACTOR_SUBJECTS = {
    "引发剂", "催化剂", "溶剂", "反应温度", "反应时间", "投料比", "单体", "共聚单体", "添加剂",
}
_IDENTITY_PROPERTY_RE = re.compile(
    r"^(?:中文名称|英文名称|中文别称|英文别称|别名|简称|代号|编号|CAS号|CAS登记号|"
    r"(?:假定|经验|理论)?(?:分子式|化学式|结构式))$",
    re.IGNORECASE,
)
_LATEX_NOISE_RE = re.compile(r"\\(?:mathrm|begin|end|prime|cdot)|\^\s*\{|\{\s*\\|\$")
_PROCESS_TITLE_RE = re.compile(r"(?:引发|聚合|反应|合成|制备|处理|测试|测定|研究|体系)")
_REACTION_SENTENCE_SUBJECT_RE = re.compile(r"(?:对.{1,40}进行.{1,30}(?:反应|处理).{0,20}(?:得到|生成|制得)(?:产物)?|经.{1,40}(?:得到|生成|制得)(?:产物)?)")
_SENTENCE_SUBJECT_RE = re.compile(
    r"[。；：!?！？]|(?:可以|能够|采用|通过|影响|导致|提高|降低|增加|减少|研究表明|结果表明|分别为|主要是|由.+构成)"
)
_SPACED_DECIMAL_RE = re.compile(r"(?P<int>[-+]?\d+)\s*[.．]\s*(?P<frac>\d+)")
_SIMPLE_VALUE_RE = re.compile(
    r"^\s*(?:[<>≤≥≈~～]?\s*)?(?P<num>[-+]?\d+(?:\.\d+)?)\s*"
    r"(?:%|‰|℃|°C|K|Pa|kPa|MPa|GPa|bar|mbar|g/cm(?:3|³)|kg/m(?:3|³)|"
    r"g|kg|mg|ug|μg|nm|um|μm|mm|cm|m|ns|us|μs|ms|s|min|h|d|Hz|kHz|MHz|"
    r"rpm|r/min|K/min|℃/min|°C/min|mm/s|cm/s|m/s|km/s|J/g|kJ/kg|kJ/mol|"
    r"mol/L|g/L|mg/L)?\s*$",
    re.IGNORECASE,
)
_NUMERIC_PROPERTY_LEAF_RE = re.compile(r"(?:^|\s/\s)[-+]?\d+(?:\.\d+)?$")
_GENERIC_SAMPLE_SUBJECT_RE = re.compile(r"^(?:共聚物|均聚物|聚合物|预聚物|样品|试样|配方|产物)$", re.IGNORECASE)
_TOC_PAGE_FACT_RE = re.compile(r"(?:^|\n)\s*(?:第?\d+章|\d+(?:[.．]\d+)+).{0,80}?(?:[.．·…]{2,}|[：:]\s*)?\d{1,4}\s*$")
_MOLECULAR_RATIO_RAW_RE = re.compile(r"(?:Mw|Mn|Mz|M)\s*/\s*(?:Mw|Mn|Mz|M)", re.IGNORECASE)
_CAUSAL_INDEPENDENT_ATTR_RE = re.compile(r"^(?:粒度|粒径|药量|装药量|直径|药柱直径|位置|埋深|埋入深度|水分含量|含水量)$")
_CAUSAL_RESPONSE_IN_CONDITION_RE = re.compile(r"(?:发火电压|平均发火电压|熄灭长度|起爆能力|燃速|爆速|爆压|爆热|感度|强度|应力|应变|模量|得率|产率|收率)")
_ANALYTICAL_PARENT_ONLY_RE = re.compile(r"^(?:DSC|DTG|TG|DSC结果|DTG-TG结果)$", re.IGNORECASE)
_DANGLING_TOPIC_CONNECTOR_RE = re.compile(r"(?:的)?(?:组成|配方|性能|性质|结构|参数|指标|结果)(?:和|及|与)$")


def _identifier_only_subject(value: str) -> bool:
    surface = re.sub(r"\s+", "", value or "").strip(" ,，;；:：|[]()（）")
    surface = re.sub(
        r"^(?:编号|序号|批次|样品|试样|配方|聚合物|no\.?|id|#)",
        "",
        surface,
        flags=re.IGNORECASE,
    )
    return bool(surface and _IDENTIFIER_ONLY_SUBJECT_RE.fullmatch(surface))


def _read_rows(path: Path) -> tuple[List[str], List[Dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        return list(reader.fieldnames or []), list(reader)


def _load_evidence_store(path: Path) -> Dict[str, Mapping[str, object]]:
    result: Dict[str, Mapping[str, object]] = {}
    if not path.exists():
        return result
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            table_id = str(payload.get("table_id", "") or "")
            if not table_id:
                raise ValueError(f"Missing table_id at evidence-store line {line_number}")
            result[table_id] = payload
    return result


def _has_numeric_projection(row: Mapping[str, str]) -> bool:
    return any(str(row.get(name, "") or "").strip() for name in ("数值", "范围下限", "范围上限", "normalized_value_num"))


def _float_or_none(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        number = float(text)
    except Exception:
        return None
    return number if math.isfinite(number) else None


def _spaced_decimal_mismatch(row: Mapping[str, str]) -> tuple[bool, str, str]:
    raw = str(row.get("尾实体/取值文本", "") or "").strip()
    if not _SPACED_DECIMAL_RE.search(raw):
        return False, "", ""
    normalized = _SPACED_DECIMAL_RE.sub(lambda m: f"{m.group('int')}.{m.group('frac')}", raw)
    match = _SIMPLE_VALUE_RE.fullmatch(normalized)
    if not match:
        return False, "", ""
    expected = _float_or_none(match.group("num"))
    projected = _float_or_none(row.get("数值") or row.get("normalized_value_num"))
    if expected is None or projected is None:
        return False, "", ""
    mismatch = abs(expected - projected) > max(1e-9, abs(expected) * 1e-9)
    return mismatch, f"{expected:g}", f"{projected:g}"


def _book_title_subject_policy(input_stem: str, rows: List[Dict[str, str]]) -> Dict[str, object]:
    """Classify exact book-title subjects semantically instead of rejecting by string equality.

    A title can itself be a material/entity name (for example a material class or
    a monograph named after one compound).  Such a title is a valid root-level
    default anchor.  Topic-only, method/model, coordinated, and descriptive
    titles are not valid as an unparsed single subject.
    """
    normalized_book = normalize_subject_name(input_stem or "").canonical_name
    matches = [
        {
            "row": index + 2,
            "subject": str(row.get("主体名称", "") or ""),
            "subject_type": str(row.get("主体类型", "") or ""),
            "source_type": str(row.get("来源类型", "") or ""),
        }
        for index, row in enumerate(rows)
        if normalize_subject_name(str(row.get("主体名称", "") or "")).canonical_name == normalized_book
    ]
    if not normalized_book or not matches:
        return {"action": "none", "count": 0, "examples": []}

    semantic = classify_heading_subject(input_stem)
    parsed_entity = normalize_subject_name(semantic.entity or "").canonical_name
    base = {
        "count": len(matches),
        "examples": matches[:20],
        "book_title": input_stem,
        "normalized_book_title": normalized_book,
        "heading_kind": semantic.kind,
        "parsed_entity": parsed_entity,
        "heading_reasons": list(semantic.reasons),
    }

    if semantic.kind in {"pure_entity", "material_class"} and parsed_entity == normalized_book:
        return {"action": "allow_entity_anchor", **base}
    if semantic.kind == "entity_with_topic":
        if parsed_entity == normalized_book:
            return {"action": "allow_entity_anchor", **base}
        return {"action": "reject_unparsed_descriptive_title", **base}
    if semantic.kind == "ambiguous":
        # Fail visible rather than fail closed: an unrecognised monograph title
        # may still be a valid chemical/material proper name.  Upstream subject
        # registry and final production gates have already confirmed these rows.
        return {"action": "allow_ambiguous_root_anchor_with_warning", **base}
    return {"action": "reject_non_entity_title", **base}


def _subject_issue_rows(rows: List[Dict[str, str]]) -> Dict[str, List[Dict[str, object]]]:
    result: Dict[str, List[Dict[str, object]]] = {
        "identifier_only_subject": [],
        "bare_sample_code_subject": [],
        "leading_orphan_punctuation": [],
        "experimental_factor_subject": [],
        "latex_process_title_subject": [],
        "sentence_like_subject": [],
        "reaction_sentence_subject": [],
        "topic_only_subject": [],
        "descriptive_heading_subject": [],
        "coordinated_heading_subject": [],
        "generic_sample_label_subject": [],
        "dangling_topic_connector_subject": [],
    }
    for row_number, row in enumerate(rows, start=2):
        subject = str(row.get("主体名称", "") or "").strip()
        sample = {
            "row": row_number,
            "subject": subject,
            "table_id": str(row.get("所属表格ID", "") or ""),
            "attribute": str(row.get("attribute_name", "") or ""),
        }
        if _identifier_only_subject(subject):
            result["identifier_only_subject"].append(sample)
        if _BARE_SAMPLE_CODE_RE.fullmatch(subject):
            result["bare_sample_code_subject"].append(sample)
        if _GENERIC_SAMPLE_SUBJECT_RE.fullmatch(subject):
            result["generic_sample_label_subject"].append(sample)
        if _DANGLING_TOPIC_CONNECTOR_RE.search(subject):
            result["dangling_topic_connector_subject"].append(sample)
        if _LEADING_ORPHAN_PUNCT_RE.search(subject):
            result["leading_orphan_punctuation"].append(sample)
        if subject in _EXPERIMENT_FACTOR_SUBJECTS:
            result["experimental_factor_subject"].append(sample)
        if len(subject) > 28 and _LATEX_NOISE_RE.search(subject) and _PROCESS_TITLE_RE.search(subject):
            result["latex_process_title_subject"].append(sample)
        if len(subject) > 35 and _SENTENCE_SUBJECT_RE.search(subject):
            result["sentence_like_subject"].append(sample)
        if _REACTION_SENTENCE_SUBJECT_RE.search(subject):
            result["reaction_sentence_subject"].append(sample)
        semantic = classify_heading_subject(subject)
        if semantic.kind == "topic_only":
            result["topic_only_subject"].append(sample)
        elif semantic.kind == "multi_entity":
            sample = dict(sample)
            sample["heading_candidates"] = list(semantic.entity_candidates)
            result["coordinated_heading_subject"].append(sample)
        elif semantic.is_entity and semantic.entity:
            normalized_subject = normalize_subject_name(subject).canonical_name
            if semantic.entity != normalized_subject:
                sample = dict(sample)
                sample["parsed_entity"] = semantic.entity
                sample["heading_kind"] = semantic.kind
                result["descriptive_heading_subject"].append(sample)
    return result


def check(output_dir: Path, contract_path: Path, require_rows: bool, require_process_steps: bool) -> Dict[str, object]:
    graph_path = output_dir / "step_graph_guard" / "graph_import_ready.tsv"
    evidence_store_path = output_dir / "step_table_evidence" / "table_evidence_store.jsonl"
    local_evidence_path = output_dir / "step_table_evidence" / "table_fact_local_evidence.tsv"
    table_report_path = output_dir / "step_table_evidence" / "table_evidence_validation_report.json"
    process_report_path = output_dir / "step_process_semantics" / "process_semantic_validation_report.json"
    run_report_path = output_dir / "book_engine_run_report.json"
    errors: List[Dict[str, object]] = []
    warnings: List[Dict[str, object]] = []

    if not graph_path.exists():
        return {
            "ok": False,
            "stage": "semantic_precision_phase95_1_semantic_book_title_gate_check",
            "validator_revision": "phase95.1_semantic_book_title_gate",
            "errors": [{"reason": "missing_graph_file", "path": str(graph_path)}],
            "warnings": [],
        }

    contract = load_contract(contract_path)
    expected_columns = list(contract["columns"])
    graph_report = validate_graph_file(graph_path, expected_columns, check_rows=True)
    if not graph_report.get("ok"):
        errors.append({"reason": "legacy_schema_contract_failed", "detail": graph_report})

    header, rows = _read_rows(graph_path)
    if require_rows and not rows:
        errors.append({"reason": "graph_has_no_rows"})

    subject_issues = _subject_issue_rows(rows)
    for reason, problem_rows in subject_issues.items():
        if problem_rows:
            errors.append({
                "reason": f"{reason}_reached_final_graph",
                "count": len(problem_rows),
                "examples": problem_rows[:20],
            })

    identity_numeric_rows: List[Dict[str, object]] = []
    decimal_mismatch_rows: List[Dict[str, object]] = []
    numeric_property_leaf_rows: List[Dict[str, object]] = []
    toc_page_fact_rows: List[Dict[str, object]] = []
    molecular_ratio_unit_rows: List[Dict[str, object]] = []
    causal_axis_reversal_rows: List[Dict[str, object]] = []
    analytical_leaf_collapse_rows: List[Dict[str, object]] = []
    for row_number, row in enumerate(rows, start=2):
        property_name = str(row.get("attribute_name", "") or "").strip()
        predicate_raw = str(row.get("predicate_raw", "") or "").strip()
        evidence_text = unescape_single_line(row.get("证据文本", ""))
        condition_text = str(row.get("条件文本", "") or "")
        unit_text = str(row.get("单位", "") or row.get("normalized_unit", "") or "").strip()
        if _TOC_PAGE_FACT_RE.search(evidence_text):
            toc_page_fact_rows.append({"row": row_number, "subject": row.get("主体名称", ""), "evidence": evidence_text[:180]})
        if _MOLECULAR_RATIO_RAW_RE.search(predicate_raw) and unit_text.upper() == "M":
            molecular_ratio_unit_rows.append({"row": row_number, "predicate_raw": predicate_raw, "unit": unit_text})
        if _CAUSAL_INDEPENDENT_ATTR_RE.fullmatch(property_name) and _CAUSAL_RESPONSE_IN_CONDITION_RE.search(condition_text):
            causal_axis_reversal_rows.append({"row": row_number, "attribute": property_name, "condition": condition_text[:180]})
        if _MOLECULAR_RATIO_RAW_RE.search(predicate_raw) and property_name != "分散度":
            analytical_leaf_collapse_rows.append({"row": row_number, "predicate_raw": predicate_raw, "attribute": property_name})
        if re.search(r"(?:DSC|DTG|TG).*(?:Texon|To|Td|Tg|Tp|Tm|T)(?:/|$)", predicate_raw, re.IGNORECASE) and _ANALYTICAL_PARENT_ONLY_RE.fullmatch(property_name):
            analytical_leaf_collapse_rows.append({"row": row_number, "predicate_raw": predicate_raw, "attribute": property_name})
        if _IDENTITY_PROPERTY_RE.fullmatch(property_name) and _has_numeric_projection(row):
            identity_numeric_rows.append({
                "row": row_number,
                "subject": str(row.get("主体名称", "") or ""),
                "attribute": property_name,
                "value": str(row.get("尾实体/取值文本", "") or ""),
                "numeric": str(row.get("数值") or row.get("normalized_value_num") or ""),
            })
        mismatch, expected, actual = _spaced_decimal_mismatch(row)
        if mismatch:
            decimal_mismatch_rows.append({
                "row": row_number,
                "subject": str(row.get("主体名称", "") or ""),
                "attribute": property_name,
                "value": str(row.get("尾实体/取值文本", "") or ""),
                "expected_numeric": expected,
                "actual_numeric": actual,
            })
        if _NUMERIC_PROPERTY_LEAF_RE.search(property_name):
            numeric_property_leaf_rows.append({
                "row": row_number,
                "subject": str(row.get("主体名称", "") or ""),
                "attribute": property_name,
                "table_id": str(row.get("所属表格ID", "") or ""),
            })

    if identity_numeric_rows:
        errors.append({
            "reason": "identity_property_has_numeric_projection",
            "count": len(identity_numeric_rows),
            "examples": identity_numeric_rows[:20],
        })
    if decimal_mismatch_rows:
        errors.append({
            "reason": "ocr_spaced_decimal_projection_mismatch",
            "count": len(decimal_mismatch_rows),
            "examples": decimal_mismatch_rows[:20],
        })
    if numeric_property_leaf_rows:
        warnings.append({
            "reason": "numeric_leaf_remains_in_property_path",
            "count": len(numeric_property_leaf_rows),
            "examples": numeric_property_leaf_rows[:20],
        })
    for reason, problem_rows in (
        ("toc_page_fact_reached_final_graph", toc_page_fact_rows),
        ("molecular_ratio_misread_as_unit", molecular_ratio_unit_rows),
        ("causal_condition_response_axis_reversed", causal_axis_reversal_rows),
        ("analytical_property_leaf_collapsed", analytical_leaf_collapse_rows),
    ):
        if problem_rows:
            errors.append({"reason": reason, "count": len(problem_rows), "examples": problem_rows[:20]})

    book_title_subject_policy: Dict[str, object] = {"action": "none", "count": 0}
    if run_report_path.exists():
        try:
            run_report = json.loads(run_report_path.read_text(encoding="utf-8-sig"))
            if run_report.get("pipeline_revision") != "phase95_generalized_owner_and_causal_hardening":
                errors.append({
                    "reason": "unexpected_pipeline_revision",
                    "actual": run_report.get("pipeline_revision", ""),
                })
            input_stem = Path(str(run_report.get("input", "") or "")).stem.strip()
            if input_stem:
                book_policy = _book_title_subject_policy(input_stem, rows)
                book_title_subject_policy = book_policy
                action = str(book_policy.get("action", "none"))
                if action.startswith("reject_"):
                    errors.append({"reason": "non_entity_book_title_used_as_subject", **book_policy})
                elif action == "allow_entity_anchor":
                    warnings.append({"reason": "entity_book_title_used_as_root_anchor", **book_policy})
                elif action == "allow_ambiguous_root_anchor_with_warning":
                    warnings.append({"reason": "ambiguous_book_title_used_as_root_anchor", **book_policy})
        except Exception as exc:  # noqa: BLE001
            errors.append({"reason": "invalid_book_engine_run_report", "detail": repr(exc)})
    else:
        errors.append({"reason": "missing_book_engine_run_report", "path": str(run_report_path)})

    physical_lines = graph_path.read_text(encoding="utf-8-sig").splitlines()
    if len(physical_lines) != len(rows) + 1:
        errors.append({
            "reason": "graph_not_one_physical_line_per_record",
            "physical_lines": len(physical_lines),
            "logical_rows": len(rows),
        })
    if header != expected_columns:
        errors.append({"reason": "header_name_or_order_changed"})

    store: Dict[str, Mapping[str, object]] = {}
    if not evidence_store_path.exists():
        errors.append({"reason": "missing_table_evidence_store", "path": str(evidence_store_path)})
    else:
        try:
            store = _load_evidence_store(evidence_store_path)
        except Exception as exc:  # noqa: BLE001
            errors.append({"reason": "invalid_table_evidence_store", "detail": repr(exc)})

    if not local_evidence_path.exists():
        errors.append({"reason": "missing_table_local_evidence", "path": str(local_evidence_path)})

    table_rows = 0
    table_evidence_matches = 0
    for row_number, row in enumerate(rows, start=2):
        if str(row.get("来源类型", "")) != "table_conditional_record":
            continue
        table_rows += 1
        table_id = str(row.get("所属表格ID", "") or "")
        payload = store.get(table_id)
        if payload is None:
            errors.append({"reason": "table_id_missing_from_evidence_store", "row": row_number, "table_id": table_id})
            continue
        evidence = unescape_single_line(row.get("证据文本", ""))
        raw = str(payload.get("raw_table_text", "") or "")
        expected_sha = str(payload.get("raw_sha256", "") or "")
        actual_sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if actual_sha != expected_sha:
            errors.append({"reason": "table_store_sha_mismatch", "table_id": table_id})
        if evidence != raw:
            errors.append({"reason": "final_table_evidence_not_full_raw_table", "row": row_number, "table_id": table_id})
        else:
            table_evidence_matches += 1

    process_rows = [row for row in rows if str(row.get("step_id", "") or "")]
    step_by_id = {str(row.get("step_id", "")): row for row in process_rows}
    for row in process_rows:
        step_id = str(row.get("step_id", "") or "")
        process_id = str(row.get("process_id", "") or "")
        action = str(row.get("step_action", "") or "")
        label = unescape_single_line(row.get("step_label", ""))
        evidence = unescape_single_line(row.get("证据文本", ""))
        if not process_id:
            errors.append({"reason": "process_step_missing_process_id", "step_id": step_id})
        if not action:
            errors.append({"reason": "process_step_missing_action", "step_id": step_id})
        if not label or label not in evidence:
            errors.append({"reason": "step_label_not_contiguous_source_span", "step_id": step_id})
        previous_id = str(row.get("previous_step_id", "") or "")
        next_id = str(row.get("next_step_id", "") or "")
        if previous_id:
            previous = step_by_id.get(previous_id)
            if previous is None:
                errors.append({"reason": "previous_step_missing_from_final_graph", "step_id": step_id, "previous_step_id": previous_id})
            elif str(previous.get("next_step_id", "") or "") != step_id:
                errors.append({"reason": "previous_next_not_reciprocal", "step_id": step_id, "previous_step_id": previous_id})
        if next_id:
            following = step_by_id.get(next_id)
            if following is None:
                errors.append({"reason": "next_step_missing_from_final_graph", "step_id": step_id, "next_step_id": next_id})
            elif str(following.get("previous_step_id", "") or "") != step_id:
                errors.append({"reason": "next_previous_not_reciprocal", "step_id": step_id, "next_step_id": next_id})

    if require_process_steps and not process_rows:
        errors.append({"reason": "no_process_steps_in_final_graph"})

    for report_path, reason in (
        (table_report_path, "table_evidence_report_not_ok"),
        (process_report_path, "process_semantic_report_not_ok"),
    ):
        if report_path.exists():
            try:
                payload = json.loads(report_path.read_text(encoding="utf-8-sig"))
                if not payload.get("ok"):
                    errors.append({"reason": reason, "path": str(report_path)})
            except Exception as exc:  # noqa: BLE001
                errors.append({"reason": "invalid_validation_report", "path": str(report_path), "detail": repr(exc)})
        else:
            errors.append({"reason": "missing_validation_report", "path": str(report_path)})

    source_counts: Dict[str, int] = {}
    for row in rows:
        source = str(row.get("来源类型", "") or "")
        source_counts[source] = source_counts.get(source, 0) + 1

    anchor_source_counts: Dict[str, int] = {}
    local_owner_override_count = 0
    ambiguous_local_owner_count = 0
    anchor_path = output_dir / "step_text_semantics" / "text_subject_anchors.tsv"
    decision_path = output_dir / "step_subject_anchor" / "subject_anchor_decisions.tsv"
    if anchor_path.exists():
        _, anchor_rows = _read_rows(anchor_path)
        for anchor_row in anchor_rows:
            source = str(anchor_row.get("source", "") or "")
            anchor_source_counts[source] = anchor_source_counts.get(source, 0) + 1
            if "local_owner_overrides_heading_default" in str(anchor_row.get("reasons", "") or ""):
                local_owner_override_count += 1
    else:
        warnings.append({"reason": "missing_text_subject_anchor_audit", "path": str(anchor_path)})
    if decision_path.exists():
        _, decision_rows = _read_rows(decision_path)
        ambiguous_local_owner_count = sum(
            1 for item in decision_rows
            if str(item.get("deterministic_source", "") or "") in {
                "multiple_local_owners_unresolved", "multiple_subject_mentions_no_llm"
            }
        )
    else:
        warnings.append({"reason": "missing_subject_anchor_decisions", "path": str(decision_path)})

    return {
        "ok": not errors,
        "stage": "semantic_precision_phase95_1_semantic_book_title_gate_check",
        "validator_revision": "phase95.1_semantic_book_title_gate",
        "pipeline_revision_required": "phase95_generalized_owner_and_causal_hardening",
        "expected_column_count": len(expected_columns),
        "actual_column_count": len(header),
        "logical_rows": len(rows),
        "physical_lines": len(physical_lines),
        "source_counts": source_counts,
        "anchor_source_counts": anchor_source_counts,
        "nearest_entity_heading_anchor_count": anchor_source_counts.get("nearest_entity_heading_anchor", 0),
        "explicit_local_fact_owner_count": anchor_source_counts.get("explicit_local_fact_owner", 0),
        "multi_heading_unique_local_mention_count": anchor_source_counts.get("multi_heading_unique_local_mention", 0),
        "local_owner_override_count": local_owner_override_count,
        "ambiguous_local_owner_count": ambiguous_local_owner_count,
        "table_rows": table_rows,
        "table_evidence_matches": table_evidence_matches,
        "process_rows": len(process_rows),
        "identifier_only_subject_rows": len(subject_issues["identifier_only_subject"]),
        "bare_sample_code_subject_rows": len(subject_issues["bare_sample_code_subject"]),
        "leading_orphan_punctuation_rows": len(subject_issues["leading_orphan_punctuation"]),
        "experimental_factor_subject_rows": len(subject_issues["experimental_factor_subject"]),
        "latex_process_title_subject_rows": len(subject_issues["latex_process_title_subject"]),
        "sentence_like_subject_rows": len(subject_issues["sentence_like_subject"]),
        "reaction_sentence_subject_rows": len(subject_issues["reaction_sentence_subject"]),
        "topic_only_subject_rows": len(subject_issues["topic_only_subject"]),
        "descriptive_heading_subject_rows": len(subject_issues["descriptive_heading_subject"]),
        "coordinated_heading_subject_rows": len(subject_issues["coordinated_heading_subject"]),
        "generic_sample_label_subject_rows": len(subject_issues["generic_sample_label_subject"]),
        "dangling_topic_connector_subject_rows": len(subject_issues["dangling_topic_connector_subject"]),
        "toc_page_fact_rows": len(toc_page_fact_rows),
        "molecular_ratio_unit_rows": len(molecular_ratio_unit_rows),
        "causal_axis_reversal_rows": len(causal_axis_reversal_rows),
        "analytical_leaf_collapse_rows": len(analytical_leaf_collapse_rows),
        "identity_numeric_projection_rows": len(identity_numeric_rows),
        "ocr_spaced_decimal_mismatch_rows": len(decimal_mismatch_rows),
        "numeric_property_leaf_warning_rows": len(numeric_property_leaf_rows),
        "book_title_subject_policy": book_title_subject_policy,
        "book_title_subject_rows": int(book_title_subject_policy.get("count", 0) or 0),
        "graph_report": graph_report,
        "warnings": warnings,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--require-rows", action="store_true")
    parser.add_argument("--require-process-steps", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = check(args.output_dir, args.contract, args.require_rows, args.require_process_steps)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n", encoding="utf-8-sig")
    print(text)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
